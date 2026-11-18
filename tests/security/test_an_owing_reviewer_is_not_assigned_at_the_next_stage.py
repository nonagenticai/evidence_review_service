"""`ERS #65` finding 2 — stage-N assignment scoping is itself a per-record leak.

`create_assignments` scopes a stage-N > 1 round to records that settled
`included` at stage N-1 (`ERS #42`), and its own comment used to claim "only
aggregate dispositions leave this block, never a per-reviewer vote" — false.
The scoped record SET itself leaves the block and becomes the stage-N queue's
membership, which discloses every other reviewer's title/abstract outcome, by
record identity and title, to a reviewer who cast no vote of their own.

Reproduced here exactly as the audit found it: LEAD owes both title/abstract
verdicts on two records; FIRST and SECOND (who have already screened) include
AAA and exclude BBB; LEAD opens full text normally alongside FIRST, and their
own full-text queue used to name exactly AAA — before LEAD had cast either of
their own title/abstract verdicts.

The fix excludes only the OWING reviewer from the stage-N insert, not the
whole call (`create_assignments` is `ON CONFLICT DO NOTHING`, so a later call
tops the reviewer up once they clear their debt).
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

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-owing-stage-assignment")
LEAD = "owing-stage-lead"
FIRST = "owing-stage-first"
SECOND = "owing-stage-second"
#: A second lead who screens nothing, and is therefore the only caller here who
#: may read the flow ledger.
#:
#: LEAD is the actor these fixtures are built around precisely BECAUSE they owe
#: both title/abstract verdicts — and FIRST and SECOND have already cast theirs
#: on the same records, so `flow.require_no_owed_decisions` refuses LEAD the
#: counts: a ledger that reports "0 not_yet_screened" over two records LEAD has
#: not judged states both partners' verdicts as a number. Same caller property
#: as the assignment rule this file tests, one route along. WITNESS holds no
#: assignment, owes nothing, and reads the ledger exactly as before.
WITNESS = "owing-stage-witness"


def headers(subject: str, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def lead_headers(subject: str = LEAD) -> dict[str, str]:
    return headers(subject, "lead")


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _decide(client, review_id, *, stage_id, record_id, reviewer, disposition, reason=None, round_number=1):  # noqa: ANN001, ANN201
    body = {
        "stage_id": stage_id,
        "round_number": round_number,
        "review_record_id": record_id,
        "disposition": disposition,
    }
    if reason is not None:
        body["exclusion_reason_code"] = reason
    response = client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(reviewer))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def lead_owing_title_abstract(client):  # noqa: ANN001, ANN201
    """AAA settles `included`, BBB settles `excluded`; LEAD has decided neither.

    FIRST and SECOND are the ONLY title/abstract voters — LEAD is named in the
    title/abstract assignment too (so LEAD genuinely OWES it, not merely
    "was never asked") but never decides either record.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "owing-stage", "external_key": f"os-{uuid.uuid4()}", "title": "Owing stage assignment"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}, {"key": "ft", "name": "Full text"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    ).json()["protocol_version_id"]
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=lead_headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=lead_headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "AAA: consensus include", "year": 2021, "doi": f"10.1/aaa-{uuid.uuid4()}"},
                    {"title": "BBB: consensus exclude", "year": 2021, "doi": f"10.1/bbb-{uuid.uuid4()}"},
                ]
            }
        ),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())
    stages = {
        s["stage_key"]: s["stage_id"]
        for s in client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()["items"]
    }
    ta, ft = stages["ta"], stages["ft"]

    assigned_ta = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ta, "reviewer_subject_ids": [LEAD, FIRST, SECOND]},
        headers=lead_headers(),
    )
    assert assigned_ta.status_code in (200, 201), assigned_ta.text

    items = client.get(
        f"/api/reviews/{review_id}/screening/queue", params={"stage_id": ta}, headers=headers(FIRST)
    ).json()["items"]
    by_title = {item["title"]: item["review_record_id"] for item in items}
    aaa, bbb = by_title["AAA: consensus include"], by_title["BBB: consensus exclude"]

    for reviewer in (FIRST, SECOND):
        _decide(client, review_id, stage_id=ta, record_id=aaa, reviewer=reviewer, disposition="include")
        _decide(
            client,
            review_id,
            stage_id=ta,
            record_id=bbb,
            reviewer=reviewer,
            disposition="exclude",
            reason="wrong_design",
        )

    counts = client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers(WITNESS)).json()["counts"]
    assert counts["not_yet_screened"] == 0, (
        f"the fixture's premise is that FIRST/SECOND alone settled both records: {counts}"
    )

    return {"review_id": review_id, "ta_stage": ta, "ft_stage": ft, "aaa": aaa, "bbb": bbb}


def test_the_owing_lead_is_excluded_from_the_stage_n_insert(client, lead_owing_title_abstract) -> None:  # noqa: ANN001
    """THE LEAK, closed. LEAD is left out; FIRST is assigned normally."""
    fixture = lead_owing_title_abstract
    review_id = fixture["review_id"]

    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ft_stage"], "reviewer_subject_ids": [LEAD, FIRST]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text
    assert LEAD not in assigned_ft.json()["reviewers"], (
        f"LEAD still owes both title/abstract verdicts and must not be handed a full-text queue: {assigned_ft.json()}"
    )

    first_queue = client.get(
        f"/api/reviews/{review_id}/screening/queue", params={"stage_id": fixture["ft_stage"]}, headers=headers(FIRST)
    ).json()
    assert [item["review_record_id"] for item in first_queue["items"]] == [fixture["aaa"]], (
        f"FIRST already screened both records and must still get the ordinary AAA-only queue: {first_queue}"
    )

    lead_queue = client.get(
        f"/api/reviews/{review_id}/screening/queue", params={"stage_id": fixture["ft_stage"]}, headers=headers(LEAD)
    ).json()
    assert lead_queue["items"] == [] and lead_queue["assigned_to_me"] == 0, (
        "LEAD must hold NO full-text assignment at all — not an empty page of one they do hold — until "
        f"their title/abstract debt is cleared: {lead_queue}"
    )


def test_topping_up_after_the_debt_clears_assigns_the_reviewer(client, lead_owing_title_abstract) -> None:  # noqa: ANN001
    """THE REMEDY. `ON CONFLICT DO NOTHING` tops LEAD up once they catch up.

    This is the documented pattern for "the prior stage wasn't finished yet
    when I first assigned this one" (`create_assignments`'s own docstring),
    applied to the SAME hole finding 2 closes: nothing about the fix strands a
    lead who genuinely finishes their own screening.
    """
    fixture = lead_owing_title_abstract
    review_id = fixture["review_id"]

    first_call = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ft_stage"], "reviewer_subject_ids": [LEAD, FIRST]},
        headers=lead_headers(),
    )
    assert first_call.status_code in (200, 201), first_call.text
    assert LEAD not in first_call.json()["reviewers"], first_call.json()

    _decide(
        client, review_id, stage_id=fixture["ta_stage"], record_id=fixture["aaa"], reviewer=LEAD, disposition="include"
    )
    _decide(
        client,
        review_id,
        stage_id=fixture["ta_stage"],
        record_id=fixture["bbb"],
        reviewer=LEAD,
        disposition="exclude",
        reason="wrong_design",
    )

    topped_up = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ft_stage"], "reviewer_subject_ids": [LEAD, FIRST]},
        headers=lead_headers(),
    )
    assert topped_up.status_code in (200, 201), topped_up.text
    assert LEAD in topped_up.json()["reviewers"], (
        f"LEAD cleared both title/abstract verdicts and must be topped up onto the full-text round: {topped_up.json()}"
    )

    lead_queue = client.get(
        f"/api/reviews/{review_id}/screening/queue", params={"stage_id": fixture["ft_stage"]}, headers=headers(LEAD)
    ).json()
    assert [item["review_record_id"] for item in lead_queue["items"]] == [fixture["aaa"]], (
        f"once LEAD's debt is cleared, the ordinary AAA-only full-text queue must appear: {lead_queue}"
    )


def test_the_exclusion_is_named_explicitly(client, lead_owing_title_abstract) -> None:  # noqa: ANN001
    """SECOND AUDIT, FINDING 3. A silent drop from `reviewers` said nothing.

    Before this fix: `POST /assignments {ft, [LEAD, FIRST]}` answered `201` with
    `reviewers: ["owing-stage-first"]` and no field anywhere said LEAD was left
    out because they still owe title/abstract — the "panel lies about
    completeness" defect `owed_hidden` exists to prevent, one endpoint over.
    Naming LEAD back is not a new disclosure: LEAD is the one who named
    themselves in this exact request.
    """
    fixture = lead_owing_title_abstract
    review_id = fixture["review_id"]

    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ft_stage"], "reviewer_subject_ids": [LEAD, FIRST]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text
    body = assigned_ft.json()
    assert body["reviewers"] == [FIRST], body
    assert body.get("reviewers_excluded_owing_prior_stage") == [LEAD], (
        f"LEAD was silently dropped with no reason given in the response: {body}"
    )

    # NEGATIVE CONTROL — the ordinary stage-1 call (no scoping, nobody can owe
    # anything) must report the field as present and empty, not absent, so a
    # client can check it unconditionally rather than branching on `stage_order`.
    reassert_ta = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ta_stage"], "reviewer_subject_ids": [LEAD, FIRST, SECOND]},
        headers=lead_headers(),
    )
    assert reassert_ta.status_code in (200, 201), reassert_ta.text
    assert reassert_ta.json().get("reviewers_excluded_owing_prior_stage") == [], reassert_ta.json()


def test_the_eligible_count_now_outruns_what_flow_discloses_to_that_caller(client, lead_owing_title_abstract) -> None:  # noqa: ANN001
    """SECOND AUDIT, FINDING 2 — ⚠️ ITS JUSTIFICATION HAS EXPIRED. Read this.

    The audit measured `records: 1` handed to an owing LEAD who knows the corpus
    is 2 records, and asked for a deliberate decision rather than silence. The
    decision made (`create_assignments`'s docstring, at the `records` field):
    leave it, because it is REDUNDANT with a number the SAME caller can already
    read from `GET /flow`'s `counts_by_stage` — "a lead-only route gated on
    nothing but the lead authority `create_assignments` already checks".

    **That premise is no longer true.** `flow.require_no_owed_decisions` refuses
    the ledger to a caller who still owes a decision on a record another reviewer
    has already judged, which is exactly this LEAD — so the number `records` is
    excused by being a duplicate of is now one this caller cannot obtain. The
    residual stands alone: an owing lead is told, on `POST /assignments`, that
    exactly 1 of the 2 records they have yet to judge settled `included`, which
    is FIRST's and SECOND's verdicts on both of them, arithmetic included.

    This test was `test_the_eligible_count_is_no_more_than_flow_already_
    discloses` and asserted the redundancy. It is kept, renamed and inverted
    rather than re-pointed at a caller who owes nothing: reading the ledger as
    WITNESS would still make the two numbers agree, and would have quietly
    re-certified a justification that had stopped applying. A test that proves
    "some other caller could have read this" does not excuse a disclosure to
    THIS one.

    It asserts three things:

    * `records` is unchanged — this change did not touch `create_assignments`,
      and `screening.py` is where any fix belongs;
    * the arithmetic identity still holds, measured through a caller entitled to
      the ledger, so a later fix can be checked against it;
    * the owing lead is refused the ledger, which is the fact that removed the
      excuse.
    """
    fixture = lead_owing_title_abstract
    review_id = fixture["review_id"]

    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": fixture["ft_stage"], "reviewer_subject_ids": [LEAD, FIRST]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text
    records = assigned_ft.json()["records"]
    assert records == 1, (
        f"the fixture's premise is one of two records (AAA) settled `included` at title/abstract: {assigned_ft.json()}"
    )

    # The identity itself, measured through a caller who owes nothing.
    flow = client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers(WITNESS)).json()
    ta_stage = next(s for s in flow["counts_by_stage"] if s["stage_key"] == "ta")
    assert ta_stage["settled_include"] == records, f"records={records} settled_include={ta_stage['settled_include']}"

    # ...and the caller who was handed `records` cannot read it.
    refused = client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers())
    assert refused.status_code == 403, (
        "the owing lead can read the ledger again, so `records` is redundant after all and this "
        f"test's premise has reverted: {refused.status_code} {refused.text[:300]}"
    )
