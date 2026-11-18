"""A stage N > 1 must not re-open records the review already excluded — `ERS #42`.

## The defect

`screening.create_assignments` assigned EVERY corpus member to every named
reviewer at EVERY stage. A full-text stage therefore opened over records that
were excluded at title/abstract and were never retrieved: the reviewer's queue
offered them, and any decision recorded on one is stray work on a record the
review already dropped.

## The fix, and what this file proves about it

At stage order N > 1, `create_assignments` now assigns only records whose
settled bucket at stage N-1 is `included`
(`flow_derivation.records_settled_included_at`, read off `derive`'s own
dispositions — the one function three call sites already share for "did the
reviewers agree").

This file proves, against a live database and the real HTTP surface:

1. A record excluded at title/abstract is NOT assigned at full text — absent
   from the full-text queue, and `POST /decisions` on it at that stage answers
   `404 "no such assignment"` (the write path closes for free, per the fix's
   own reasoning — no new check was added to get this).
2. A record included at title/abstract IS assigned at full text.
3. Doing this does NOT reinstate the ERS #42 deadlock: a record stuck
   `not_yet_screened` forever with no conflict to adjudicate. The excluded
   record settles `excluded` at title/abstract — not `not_yet_screened` — and
   the review closes: `POST /exports` succeeds once the reachable work is
   done, it does not refuse forever the way the deadlock did.
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

TENANT = tenant("tenant-stage-scope")
LEAD = "stage-scope-lead"
REVIEWER = "stage-scope-reviewer"


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


def _decide(client, review_id, *, stage_id, record_id, disposition, reason=None):  # noqa: ANN001, ANN202
    body = {"stage_id": stage_id, "review_record_id": record_id, "disposition": disposition}
    if reason is not None:
        body["exclusion_reason_code"] = reason
    response = client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(REVIEWER, "reviewer"))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="module")
def scoped_review(client) -> dict:  # noqa: ANN001
    """A two-stage review, one record excluded at title/abstract and one
    included, screened all the way through so `POST /exports` can be reached.
    `reviewers_required=1` throughout — the assignment scope under test is
    orthogonal to how many reviewers a stage needs to agree.
    """
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "stage-scope",
            "external_key": f"stage-scope-{uuid.uuid4()}",
            "title": "Stage assignment scoping",
        },
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {"design": "randomised controlled trial"},
            "exclusion_criteria": {"design": "not a randomised controlled trial"},
            "stage_definitions": [
                {"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 1},
                {"key": "full-text", "name": "Full text", "reviewers_required": 1},
            ],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "Excluded at the abstract", "doi": f"10.9000/{uuid.uuid4()}", "year": 2020},
                    {"title": "Included at the abstract", "doi": f"10.9000/{uuid.uuid4()}", "year": 2021},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text

    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code == 201, sealed.text

    stages = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]
    ta_stage, ft_stage = stages[0]["stage_id"], stages[1]["stage_id"]

    assigned_ta = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ta_stage, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned_ta.status_code == 201, assigned_ta.text
    assert assigned_ta.json()["assignments_created"] == 2, assigned_ta.json()

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={ta_stage}", headers=headers(REVIEWER, "reviewer")
    ).json()
    titles = {item["review_record_id"]: item["title"] for item in queue["items"]}
    assert len(titles) == 2

    excluded_id = next(rid for rid, title in titles.items() if title.startswith("Excluded"))
    included_id = next(rid for rid, title in titles.items() if title.startswith("Included"))

    _decide(client, review_id, stage_id=ta_stage, record_id=excluded_id, disposition="exclude", reason="wrong_design")
    _decide(client, review_id, stage_id=ta_stage, record_id=included_id, disposition="include")

    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ft_stage, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned_ft.status_code == 201, assigned_ft.text

    return {
        "review_id": review_id,
        "ta_stage": ta_stage,
        "ft_stage": ft_stage,
        "excluded_id": excluded_id,
        "included_id": included_id,
        "assigned_ft": assigned_ft.json(),
    }


def test_only_the_included_record_is_assigned_at_full_text(scoped_review) -> None:  # noqa: ANN001
    """`POST /assignments` on the full-text stage creates exactly ONE row's
    worth of work — the record that settled `included` at title/abstract —
    and reports that scope back honestly, not the corpus size.
    """
    body = scoped_review["assigned_ft"]
    assert body["assignments_created"] == 1, body
    assert body["records"] == 1, body


def test_the_excluded_record_is_absent_from_the_full_text_queue(client, scoped_review) -> None:  # noqa: ANN001
    queue = client.get(
        f"/api/reviews/{scoped_review['review_id']}/screening/queue?stage_id={scoped_review['ft_stage']}",
        headers=headers(REVIEWER, "reviewer"),
    ).json()
    ids = {item["review_record_id"] for item in queue["items"]}
    assert scoped_review["included_id"] in ids, queue
    assert scoped_review["excluded_id"] not in ids, queue


def test_a_decision_on_the_excluded_record_at_full_text_is_refused_404(client, scoped_review) -> None:  # noqa: ANN001
    """The write path closes for free — no new check, per the fix's own
    reasoning: `record_decision` already answers `404 "no such assignment"` for
    any record the caller was never assigned, and this record now never is.
    """
    response = client.post(
        f"/api/reviews/{scoped_review['review_id']}/decisions",
        json={
            "stage_id": scoped_review["ft_stage"],
            "review_record_id": scoped_review["excluded_id"],
            "disposition": "include",
        },
        headers=headers(REVIEWER, "reviewer"),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "no such assignment"
    # L5: the record identifier is not echoed back.
    assert scoped_review["excluded_id"] not in json.dumps(response.json())


def test_the_excluded_record_settles_excluded_not_stuck_unscreened(client, scoped_review) -> None:  # noqa: ANN001
    """Proof this does NOT reinstate ERS #42: that deadlock was a record stuck
    `not_yet_screened` forever with no conflict to adjudicate. Here the excluded
    record settles `excluded` at title/abstract — a terminal bucket, reached
    with no full-text work needed and none possible.
    """
    flow = client.get(f"/api/reviews/{scoped_review['review_id']}/flow", headers=headers()).json()
    assert flow["counts"]["not_yet_screened"] == 0, flow["counts"]
    assert flow["counts"]["excluded"] == 1, flow["counts"]


def test_the_review_closes_the_full_text_stage_and_exports(client, scoped_review) -> None:  # noqa: ANN001
    """The other half of "not a deadlock": the review can actually finish.
    ERS #42 refused `POST /exports` forever with no honest remedy; here,
    finishing the one reachable full-text decision is enough to seal.
    """
    _decide(
        client,
        scoped_review["review_id"],
        stage_id=scoped_review["ft_stage"],
        record_id=scoped_review["included_id"],
        disposition="include",
    )
    flow = client.get(f"/api/reviews/{scoped_review['review_id']}/flow", headers=headers()).json()
    assert flow["counts"]["not_yet_screened"] == 0, flow["counts"]
    assert flow["counts"]["included"] == 1, flow["counts"]
    assert flow["counts"]["excluded"] == 1, flow["counts"]

    export = client.post(f"/api/reviews/{scoped_review['review_id']}/exports", headers=headers())
    assert export.status_code == 201, export.text
    assert export.json()["included_count"] == 1, export.json()

    fetched = client.get(
        f"/api/reviews/{scoped_review['review_id']}/exports/{export.json()['export_id']}", headers=headers()
    )
    assert fetched.status_code == 200, fetched.text
    included = fetched.json()["payload"]["included"]
    assert [item["review_record_id"] for item in included] == [scoped_review["included_id"]], included
