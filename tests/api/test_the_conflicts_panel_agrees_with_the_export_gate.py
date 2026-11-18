"""The lead's panel reported an open disagreement the export had already accepted.

A disagreement ends in one of two ways: a lead adjudicates it, or one reviewer
supersedes their own decision and the two agree. `flow._unresolved_conflicts`
knows both. `GET /conflicts` knew only the first — it computed
`total_open = len(all) - len(adjudicated)` and `resolved = adjudication is not
None`.

Measured on `4942b27`, after a consensus:

    /conflicts total=1 open=1
    POST /exports -> 201

So the panel showed an outstanding disagreement, with an "Unblind & adjudicate"
prompt, for something that needed nothing — while the export sealed and (since
the close gate) `:close` succeeded. Two spellings of one question, which is the
same defect class as the ledger and conflict detection disagreeing about
`reviewers_required`; that one silently exported on two of three required
reviewers.

`resolved` now means NOT OUTSTANDING, and `adjudication` — already in the payload
— still answers HOW it stopped being outstanding. They have to move together
because `fr`'s panel compares them directly:

    total_open > conflicts.filter((c) => !c.resolved).length

is how it tells the reader the page under-represents the review, and that banner
lies if the two definitions disagree.
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

TENANT = tenant("tenant-conflict-panel")
LEAD = "conflict-panel-lead"


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
def disputed(client):  # noqa: ANN001, ANN201
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "panel", "external_key": f"cp-{uuid.uuid4()}", "title": "Panel"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
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
        json={"stage_id": stage_id, "reviewer_subject_ids": ["r1", "r2"]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers("r1", "reviewer"),
    ).json()["items"][0]["review_record_id"]

    ids = {}
    for who, disposition in (("r1", "include"), ("r2", "exclude")):
        got = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": disposition,
                "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
                "rationale": "screened",
            },
            headers=headers(who, "reviewer"),
        )
        assert got.status_code == 201, got.text
        ids[who] = got.json()["decision_id"]

    panel = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()
    assert panel["total_open"] == 1, f"the fixture opened no conflict: {panel}"
    return {"review_id": review_id, "stage_id": stage_id, "record_id": record_id, "decisions": ids}


def _panel(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_a_consensus_clears_the_panel_as_well_as_the_export(client, disputed) -> None:
    """THE DEFECT. `total_open=1` beside `POST /exports -> 201`."""
    review_id = disputed["review_id"]
    corrected = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": disputed["stage_id"],
            "round_number": 1,
            "review_record_id": disputed["record_id"],
            "disposition": "include",
            "rationale": "on reflection it qualifies",
            "supersedes_decision_id": disputed["decisions"]["r2"],
        },
        headers=headers("r2", "reviewer"),
    )
    assert corrected.status_code == 201, corrected.text

    exported = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert exported.status_code == 201, "the fixture did not actually reach consensus: " + exported.text

    panel = _panel(client, review_id)
    assert panel["total_open"] == 0, (
        "the export sealed and the panel still reports an outstanding disagreement: " + json.dumps(panel)
    )
    assert panel["items"][0]["resolved"] is True, (
        "the row renders as unresolved, so the lead is prompted to adjudicate a disagreement that no "
        "longer exists: " + json.dumps(panel["items"][0])
    )
    assert panel["items"][0]["adjudication"] is None, (
        "nobody adjudicated this — `adjudication` must still say HOW it was settled, or the two "
        "questions have been collapsed into one"
    )


def test_an_open_disagreement_still_reads_as_open(client, disputed) -> None:
    """NEGATIVE CONTROL. Without it, "always resolved" passes the test above."""
    panel = _panel(client, disputed["review_id"])
    assert panel["total_open"] == 1, json.dumps(panel)
    assert panel["items"][0]["resolved"] is False, json.dumps(panel["items"][0])

    refused = client.post(f"/api/reviews/{disputed['review_id']}/exports", headers=headers())
    assert refused.status_code == 422, refused.text


def test_an_adjudicated_disagreement_still_says_who_ruled(client, disputed) -> None:
    """NEGATIVE CONTROL for the other arm. `resolved` moved; `adjudication` did not.

    A consumer that needs "did a lead rule on this" — separation of duty, the
    audit trail — must still be able to ask, or retargeting `resolved` has
    quietly destroyed the answer rather than moved it.
    """
    review_id = disputed["review_id"]
    conflict_id = _panel(client, review_id)["items"][0]["conflict_id"]
    client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers(),
    )
    resolved = client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:resolve",
        json={"disposition": "include", "rationale": "the design qualifies"},
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text

    panel = _panel(client, review_id)
    assert panel["total_open"] == 0, json.dumps(panel)
    assert panel["items"][0]["resolved"] is True, json.dumps(panel["items"][0])
    assert panel["items"][0]["adjudication"] is not None, (
        "the lead's ruling is no longer reported, so `resolved` did not move — it replaced: "
        + json.dumps(panel["items"][0])
    )
