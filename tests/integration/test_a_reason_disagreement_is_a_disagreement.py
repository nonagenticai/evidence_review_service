"""Two reviewers, one `exclude`, two different reasons — `ER-004.4` / `ER-005.2`.

`flow_derivation` took the exclusion reason from the EARLIEST vote:

    reason = next((v[1] for v in votes if v[1]), None)

so two reviewers who both excluded a record and disagreed about WHY were called
in agreement, the record was sealed screened, and the second reviewer's stated
reason was stored, never contradicted, and absent from the manifest. Measured on
5481931, r1 `wrong_design` against r2 `wrong_population`:

    AssertionError: two reviewers gave two different reasons for excluding this
    record and no conflict opened, so the second reviewer's stated reason is
    stored, never contradicted and invisible to the lead: {'items': [], 'count':
    0, 'open': 0, 'has_more': False, 'total': 0, 'total_open': 0}
    assert 0 == 1

    AssertionError: the review closed over an open, unadjudicated reason
    disagreement ... "state":"closed" ...
    assert 200 == 422

## Why this is a disagreement and not a documented lossy field

`exclusion_reasons` is a member of the released
`urn:datacompany:inclusion-set-export:2` manifest, and the schema's `$comment` on
`excluded` says "broken down by reason code below". "Broken down by" asserts a
PARTITION of a population — a breakdown that silently deletes one reviewer's
answer is not one. Counting the record under both reasons instead would break
`sum(exclusion_reasons.values()) == excluded`, which is stated as prose in
`$defs/flow_counts.$comment` and deliberately not declared as a cross-field
rule, so it would break no validator while breaking the meaning.

Treating it as a disagreement leaves the published meaning untouched — the
partition still partitions, the sum identity still holds — and every entry is
now one that reviewers or a lead actually agreed on. No contract version bump.
:func:`test_two_reviewers_excluding_for_the_same_reason_still_open_no_conflict`
is the control that pins that, and it passed before this change as well as
after: nothing about agreement moved.

## ⚠️ THREE derivations had to move, not two

`screening._maybe_open_conflict` opens the conflict and `flow_derivation.derive`
decides whether the record is screened. There is a third —
`flow._unresolved_conflicts` — which decides whether an OPEN conflict has been
settled by the reviewers agreeing since, and the export gate, `:close` and the
lead's `/conflicts` panel all read it. Measured with the first two moved and the
third left comparing dispositions alone:

    GET /conflicts -> total=1  open=0  items[0].resolved=true
                      items[0].adjudication=null
    POST :close    -> 200
    GET /flow      -> not_yet_screened=1

A conflict the lead's panel calls resolved with nothing that resolved it, and a
review closed over it. `:close` gates on that helper and deliberately NOT on
`not_yet_screened`, so it was the only thing between a reason disagreement and a
concluded review — whose exit then needs an attested reopen.
:func:`test_a_review_cannot_be_closed_over_an_unadjudicated_reason_disagreement`
is the test that caught it and is why all three now call one `agreement_key`.

## The escape

:func:`test_a_lead_adjudicating_the_reason_settles_the_record`. `AdjudicateBody`
already carries `exclusion_reason_code`, so the lead names the reason that
reaches the manifest. A refusal with no remedy would be a worse defect than the
one being fixed.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-reason-split")
LEAD = "reason-split-lead"
FIRST = "reason-split-r1"
SECOND = "reason-split-r2"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def undecided(client):  # noqa: ANN001, ANN201
    """One record, two reviewers, a vocabulary with TWO exclusion reasons in it."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "reason-split", "external_key": f"rs-{uuid.uuid4()}", "title": "Reason split"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population"],
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
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [FIRST, SECOND]},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(FIRST, "reviewer"),
    ).json()["items"][0]["review_record_id"]
    return {"review_id": review_id, "stage_id": stage_id, "record_id": record_id}


def _exclude(client, undecided, who: str, reason: str):  # noqa: ANN001, ANN201
    got = client.post(
        f"/api/reviews/{undecided['review_id']}/decisions",
        json={
            "stage_id": undecided["stage_id"],
            "round_number": 1,
            "review_record_id": undecided["record_id"],
            "disposition": "exclude",
            "exclusion_reason_code": reason,
            "rationale": "screened",
        },
        headers=headers(who, "reviewer"),
    )
    assert got.status_code == 201, got.text
    return got.json()


def _flow(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()["counts"]


def _conflicts(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_two_reviewers_excluding_for_different_reasons_open_a_conflict(client, undecided) -> None:
    """THE DEFECT. `exclude/wrong_design` against `exclude/wrong_population`."""
    _exclude(client, undecided, FIRST, "wrong_design")
    _exclude(client, undecided, SECOND, "wrong_population")

    conflicts = _conflicts(client, undecided["review_id"])
    assert conflicts["total"] == 1, (
        "two reviewers gave two different reasons for excluding this record and no conflict "
        "opened, so the second reviewer's stated reason is stored, never contradicted and "
        f"invisible to the lead: {conflicts}"
    )
    assert conflicts["total_open"] == 1, (
        f"the conflict is not counted OPEN, so the lead's panel shows nothing to adjudicate: {conflicts}"
    )

    counts = _flow(client, undecided["review_id"])
    assert counts["not_yet_screened"] == 1, (
        "the ledger called the record screened while the reviewers disagree about why it was "
        f"excluded, so a conflict is open and the ledger does not know it: {counts}"
    )
    assert counts["excluded"] == 0, counts
    assert counts["exclusion_reasons"] == {}, (
        "the ledger sealed ONE of the two reasons given. `exclusion_reasons` is published as a "
        "breakdown OF `excluded`, and a breakdown that silently drops one reviewer's answer is "
        f"not one: {counts['exclusion_reasons']}"
    )

    export = client.post(f"/api/reviews/{undecided['review_id']}/exports", headers=headers())
    assert export.status_code == 422, (
        "the inclusion set sealed a per-reason breakdown built from one reviewer's answer: " + export.text
    )


def test_a_review_cannot_be_closed_over_an_unadjudicated_reason_disagreement(client, undecided) -> None:
    """`:close` gates on the conflict, NOT on `not_yet_screened`.

    So the conflict-clearing rule in `flow._unresolved_conflicts` has to key on
    the reason too. It compares DISPOSITIONS only, and two `exclude` votes are
    unanimous by that measure — it would call the conflict settled by consensus,
    hide it from the lead's panel, and let the review close over a disagreement
    nobody ever ruled on. `01-…:125` forbids exactly that.
    """
    _exclude(client, undecided, FIRST, "wrong_design")
    _exclude(client, undecided, SECOND, "wrong_population")

    closed = client.post(f"/api/reviews/{undecided['review_id']}:close", json={"rationale": "done"}, headers=headers())
    assert closed.status_code == 422, (
        "the review closed over an open, unadjudicated reason disagreement. A close is a "
        "statement that the review's disagreements were settled, and this one was not: " + closed.text
    )


def test_two_reviewers_excluding_for_the_same_reason_still_open_no_conflict(client, undecided) -> None:
    """THE NEGATIVE CONTROL, and the reason no contract version is bumped.

    `exclusion_reasons` still partitions `excluded` and the sum identity stated
    in `inclusion-set-export.v2`'s `$defs/flow_counts.$comment` still holds.
    """
    _exclude(client, undecided, FIRST, "wrong_design")
    _exclude(client, undecided, SECOND, "wrong_design")

    conflicts = _conflicts(client, undecided["review_id"])
    assert conflicts["total"] == 0, f"two reviewers who agree completely opened a conflict: {conflicts}"

    counts = _flow(client, undecided["review_id"])
    assert counts["records_screened"] == 1, counts
    assert counts["excluded"] == 1, counts
    assert counts["exclusion_reasons"] == {"wrong_design": 1}, counts
    assert sum(counts["exclusion_reasons"].values()) == counts["excluded"], counts

    export = client.post(f"/api/reviews/{undecided['review_id']}/exports", headers=headers())
    assert export.status_code == 201, export.text


def test_a_lead_adjudicating_the_reason_settles_the_record(client, undecided) -> None:
    """The escape. A reason disagreement is adjudicable, so this is not a trap.

    `AdjudicateBody` already carries `exclusion_reason_code`, so the lead names
    the reason that reaches the manifest — which is what makes candidate 3 a
    complete answer rather than a refusal with no remedy.
    """
    _exclude(client, undecided, FIRST, "wrong_design")
    _exclude(client, undecided, SECOND, "wrong_population")

    conflict_id = _conflicts(client, undecided["review_id"])["items"][0]["conflict_id"]
    unblinded = client.post(
        f"/api/reviews/{undecided['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudication panel", "disclose_to": [FIRST, SECOND]},
        headers=headers(),
    )
    assert unblinded.status_code in (200, 201), unblinded.text
    resolved = client.post(
        f"/api/reviews/{undecided['review_id']}/conflicts/{conflict_id}:resolve",
        json={
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_population",
            "rationale": "the population is the binding objection",
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text

    counts = _flow(client, undecided["review_id"])
    assert counts["records_screened"] == 1, counts
    assert counts["excluded"] == 1, counts
    assert counts["exclusion_reasons"] == {"wrong_population": 1}, (
        "the adjudicated reason is not the one the ledger reports: " + json.dumps(counts)
    )
    export = client.post(f"/api/reviews/{undecided['review_id']}/exports", headers=headers())
    assert export.status_code == 201, export.text
