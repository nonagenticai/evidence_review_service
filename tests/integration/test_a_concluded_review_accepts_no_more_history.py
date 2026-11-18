"""A closed review kept accepting work, and the live ledger then contradicted its own export.

Measured on `b3f4af1`, driving the real API. After `POST /reviews/{id}:close`:

    ACCEPTED  201  import records
    ACCEPTED  201  seal corpus version
    ACCEPTED  201  start dedup run
    ACCEPTED  201  create assignments
    ACCEPTED  201  record a decision
    ACCEPTED  201  new protocol version

After `:archive` — a state with no outgoing edge in `LEGAL_TRANSITIONS` — five of
the six still did.

## Why it matters

An inclusion set is published FROM a closed review. The sealed artifact is
immutable and stays correct; then `GET /flow` on the same review reports
different counts from the export taken out of it, with nothing anywhere saying
which is current, and `:verify` answering `verified` for an inclusion set the
live ledger contradicts.

## The one refusal that already existed was an accident

`record_decision` answered 409 on the archived review in that measurement, which
reads like a guard and is not: the reviewer had already decided that record in
the closed round, so it was the "a correction must supersede the previous
decision" conflict. A fresh reviewer or a fresh record sailed through. A check
that fires for an unrelated reason is worse than none, because it makes the hole
look covered — which is why the test below closes the review with a record
NOBODY has decided.

## What is deliberately NOT changed

`paused`. A pause is a suspension, not a conclusion: `paused -> active` is legal,
no export is published from a pause, and a reviewer part-way through a queue when
a lead pauses would otherwise have their next decision refused with their work
half recorded. Whether a pause should stop reviewers writing is a workflow
decision for the people running the review, not a correctness defect.
:func:`test_a_paused_review_still_accepts_work` pins that as a decision rather
than leaving it as an oversight.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-concluded")
LEAD = "concluded-lead"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def screened_review(client):  # noqa: ANN001, ANN201
    """An active review with one record, screened to completion by one reviewer."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "concluded", "external_key": f"cc-{uuid.uuid4()}", "title": "Concluded"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()["protocol_version_id"]
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["rv"]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers("rv", "reviewer"),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "round_number": 1,
            "review_record_id": record_id,
            "disposition": "include",
            "rationale": "meets the criteria",
        },
        headers=headers("rv", "reviewer"),
    )
    assert decided.status_code == 201, decided.text
    return {"review_id": review_id, "stage_id": stage_id, "record_id": record_id}


def _every_write(client, fixture) -> dict[str, int]:  # noqa: ANN001
    """Every endpoint that adds to a review's history, with its status code.

    `record a decision` is driven by a reviewer who has decided NOTHING in this
    review, so a 409 here cannot be the supersession conflict wearing a state
    check's clothes — which is exactly how the pre-fix code looked guarded.
    """
    review_id, stage_id = fixture["review_id"], fixture["stage_id"]
    out = {
        "import records": client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps({"records": [{"title": "Late", "year": 2022, "doi": f"10.1/{uuid.uuid4()}"}]}),
            headers={**headers(), "Content-Type": "application/octet-stream"},
        ).status_code,
        "seal corpus version": client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code,
        "start dedup run": client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers()).status_code,
        "create assignments": client.post(
            f"/api/reviews/{review_id}/assignments",
            json={"stage_id": stage_id, "reviewer_subject_ids": ["fresh-reviewer"]},
            headers=headers(),
        ).status_code,
        "record a decision": client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 1,
                "review_record_id": fixture["record_id"],
                "disposition": "include",
                "rationale": "still writing",
            },
            headers=headers("fresh-reviewer", "reviewer"),
        ).status_code,
        "create protocol version": client.post(
            f"/api/reviews/{review_id}/protocol-versions",
            json={
                "inclusion_criteria": {},
                "exclusion_criteria": {},
                "stage_definitions": [{"key": "ta", "name": "TA"}],
                "exclusion_reason_vocabulary": ["wrong_design"],
                "reviewers_required": 1,
            },
            headers=headers(),
        ).status_code,
    }
    return out


# --------------------------------------------------------------------------


def test_a_closed_review_refuses_every_write(client, screened_review) -> None:
    """THE DEFECT. Six endpoints answered 201 after `:close`."""
    closed = client.post(
        f"/api/reviews/{screened_review['review_id']}:close", json={"rationale": "done"}, headers=headers()
    )
    assert closed.status_code in (200, 201), closed.text

    accepted = {what: code for what, code in _every_write(client, screened_review).items() if code in (200, 201)}
    assert not accepted, f"a closed review still accepted writes: {accepted}"


def test_an_archived_review_refuses_every_write(client, screened_review) -> None:
    """`archived` has no outgoing edge, so there is not even a remedy to offer."""
    review_id = screened_review["review_id"]
    client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=headers())
    archived = client.post(f"/api/reviews/{review_id}:archive", json={"rationale": "filed"}, headers=headers())
    assert archived.status_code in (200, 201), archived.text

    accepted = {what: code for what, code in _every_write(client, screened_review).items() if code in (200, 201)}
    assert not accepted, f"an archived review still accepted writes: {accepted}"


def test_the_remedy_the_refusal_names_actually_works(client, screened_review) -> None:
    """NEGATIVE CONTROL, and the one that makes the refusal honest.

    The message tells a lead to reopen. If `:reopen` did not restore the writes,
    the refusal would be a dead end dressed as advice — and "refuse on closed"
    would be indistinguishable from "refuse always", which no test above could
    tell apart.
    """
    review_id = screened_review["review_id"]
    client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=headers())

    refused = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Late", "year": 2022, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert refused.status_code == 409, refused.text
    assert "reopen" in refused.text.lower(), "the refusal does not name the remedy: " + refused.text
    assert "attest" in refused.text.lower(), (
        "the refusal says to reopen without saying a reopen is attested, so following it lands on a "
        "422 the lead was not warned about: " + refused.text
    )

    # A reopen is ATTESTED — `transition` refuses one without an approval
    # reference bound to the closed revision. That is the remedy's real shape and
    # the refusal message says so, because "reopen it" alone would send a lead
    # into a 422 they were not warned about.
    bare = client.post(f"/api/reviews/{review_id}:reopen", json={"rationale": "more to do"}, headers=headers())
    assert bare.status_code == 422, bare.text

    reopened = client.post(
        f"/api/reviews/{review_id}:reopen",
        json={"rationale": "more to do", "approval_attestation_ref": "fr://approval/reopen-1"},
        headers=headers(),
    )
    assert reopened.status_code in (200, 201), reopened.text

    accepted = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Late", "year": 2022, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert accepted.status_code in (200, 201), (
        "the refusal told the lead to reopen and reopening did not restore the write: " + accepted.text
    )


def test_a_closed_review_can_still_be_exported_and_read(client, screened_review) -> None:
    """NEGATIVE CONTROL. Publishing from a closed review is the POINT of closing.

    A guard that also blocked the export would have made the state useless, and
    every test above would still pass.
    """
    review_id = screened_review["review_id"]
    client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=headers())

    exported = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert exported.status_code == 201, exported.text
    assert exported.json()["included_count"] == 1, exported.text

    flow = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert flow.status_code == 200, flow.text
    assert flow.json()["counts"]["included"] == 1, flow.text

    verified = client.get(f"/api/reviews/{review_id}/exports/{exported.json()['export_id']}:verify", headers=headers())
    assert verified.status_code == 200 and verified.json()["verified"] is True, verified.text


def test_a_paused_review_still_accepts_work(client, screened_review) -> None:
    """A DECISION, pinned — not an oversight.

    A pause is a suspension, not a conclusion. `paused -> active` is legal, no
    export is published from a pause, and refusing here would strand a reviewer
    part-way through a queue with their work half recorded. If that is ever to
    change it should change deliberately, with this test as the place the change
    is argued.
    """
    review_id = screened_review["review_id"]
    paused = client.post(f"/api/reviews/{review_id}:pause", json={"rationale": "waiting"}, headers=headers())
    assert paused.status_code in (200, 201), paused.text

    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "During the pause", "year": 2022, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), (
        "a paused review refused a write. That may be right, but it is a workflow change and this test "
        "is where it gets argued: " + imported.text
    )
