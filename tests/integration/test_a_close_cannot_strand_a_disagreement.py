"""`:close` succeeded over an open disagreement, and now that would strand it.

`01-…:125` — a close cannot happen with disagreements unresolved. The export gate
has always enforced that. `:close` never did. Measured on `21685a6`:

    /conflicts total=1 open=1
    POST /exports -> 422
    POST :close   -> 200

That was a missing gate. It became worse than that once a concluded review
stopped accepting writes: `:resolve` is a write, so closing over an open conflict
means the only route back to the adjudication is an ATTESTED reopen. A lead could
put a review into a state whose exit needs an FR approval reference, by clicking
close.

## What this does NOT gate on

`not_yet_screened`. Abandoning a half-screened review is a legitimate thing to
do, and the export already refuses to publish one. Closing is a statement about
the review's working life, not a claim that it produced an inclusion set — and
those are different claims. :func:`test_an_unfinished_review_can_still_be_closed`
pins that as a decision.
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

TENANT = tenant("tenant-strand")
LEAD = "strand-lead"


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
def disputed_review(client):  # noqa: ANN001, ANN201
    """One record, two reviewers, one open and unadjudicated disagreement."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "strand", "external_key": f"st-{uuid.uuid4()}", "title": "Stranded"},
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

    conflicts = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()
    assert conflicts["total"] >= 1, f"the fixture opened no conflict: {conflicts}"

    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "record_id": record_id,
        "conflict_id": conflicts["items"][0]["conflict_id"],
        "decisions": ids,
    }


def _close(client, review_id: str):  # noqa: ANN001, ANN201
    return client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=headers())


# --------------------------------------------------------------------------


def test_closing_over_an_open_disagreement_is_refused(client, disputed_review) -> None:
    """THE DEFECT. `:close` answered 200 while `/conflicts` reported one open."""
    refused = _close(client, disputed_review["review_id"])
    assert refused.status_code == 422, (
        f"a review closed over an unresolved disagreement (HTTP {refused.status_code}): {refused.text}"
    )
    assert disputed_review["conflict_id"] in json.dumps(refused.json()), (
        "the refusal does not name the conflict, so a lead cannot act on it: " + refused.text
    )


def test_an_adjudicated_disagreement_closes(client, disputed_review) -> None:
    """NEGATIVE CONTROL. The gate must clear, or `:close` becomes unreachable.

    Without this, "refuse when a conflict exists" and "refuse always" are the
    same test — and a review that can never be closed can never be archived
    either.
    """
    review_id = disputed_review["review_id"]
    unblinded = client.post(
        f"/api/reviews/{review_id}/conflicts/{disputed_review['conflict_id']}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers(),
    )
    assert unblinded.status_code in (200, 201), unblinded.text
    resolved = client.post(
        f"/api/reviews/{review_id}/conflicts/{disputed_review['conflict_id']}:resolve",
        json={"disposition": "include", "rationale": "the design does qualify"},
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text

    closed = _close(client, review_id)
    assert closed.status_code in (200, 201), (
        "the disagreement was adjudicated and the review still refuses to close: " + closed.text
    )


def test_a_disagreement_settled_by_consensus_closes(client, disputed_review) -> None:
    """The other way a disagreement ends: one reviewer comes round.

    `_unresolved_conflicts` treats complete unanimity at the conflict's own
    position as settled, and `:close` must honour that. Requiring an adjudication
    row would force a lead to rule on a disagreement that no longer exists — the
    "refuses over a disagreement that does not exist" state that function's own
    docstring exists to prevent.
    """
    review_id = disputed_review["review_id"]
    corrected = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": disputed_review["stage_id"],
            "round_number": 1,
            "review_record_id": disputed_review["record_id"],
            "disposition": "include",
            "rationale": "on reflection it qualifies",
            "supersedes_decision_id": disputed_review["decisions"]["r2"],
        },
        headers=headers("r2", "reviewer"),
    )
    assert corrected.status_code == 201, corrected.text

    closed = _close(client, review_id)
    assert closed.status_code in (200, 201), (
        "both reviewers now agree and the review still refuses to close: " + closed.text
    )


def test_an_unfinished_review_can_still_be_closed(client) -> None:
    """A DECISION, pinned — this gate is about disagreements, not about progress.

    Abandoning a half-screened review is a legitimate thing to do, and the export
    already refuses to publish one. Closing is a statement about the review's
    working life, not a claim that it produced an inclusion set.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "strand", "external_key": f"st2-{uuid.uuid4()}", "title": "Abandoned"},
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
        content=json.dumps({"records": [{"title": "Never screened", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())

    counts = client.get(f"/api/reviews/{review_id}/flow", headers=headers()).json()["counts"]
    assert counts["not_yet_screened"] == 1, counts
    refused = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert refused.status_code == 422, refused.text

    closed = _close(client, review_id)
    assert closed.status_code in (200, 201), (
        "an unfinished review could not be abandoned. That may be right, but it is a workflow change "
        "and this test is where it gets argued: " + closed.text
    )
