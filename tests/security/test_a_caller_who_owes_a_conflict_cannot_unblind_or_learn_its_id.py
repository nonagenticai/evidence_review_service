"""`ERS #65` finding 1 — the caller a conflict withholds from can self-serve.

`list_conflicts` withholds a conflict — row and every counter — from a caller
who still owes a decision at its `(stage, round, record)` position, and reports
only a review-wide `owed_hidden` cardinality (`application/services/screening.
py::list_conflicts`). Two channels defeated that withholding entirely, and this
file is both halves, reproducing the adversarial audit's exact scenario.

## Half one — `unblind_conflict` had no owes-check

`adjudicate` explicitly 409s a caller who owes the conflict's position
(`records_the_caller_still_owes`); `unblind_conflict`, immediately above it in
the same module, did not — and `disclose_to` defaults to the actor alone. So
the caller a conflict is hidden FROM could `:unblind` it to themselves and read
it straight back off `GET /conflicts`.

## Half two — `POST /exports`' 422 named it for free

`flow.create_export`'s refusal put every unresolved `conflict_id` in the body,
review-wide, with no regard for whether the caller owed it. That made the
`:unblind` fix above necessary but not sufficient on its own: a caller who
never learns an id cannot self-unblind it, and this refusal was one HTTP call
away from handing back the exact id `list_conflicts` was withholding.

## Half three — the SECOND audit found `:close` had the identical leak

`reviews.transition(closed)` calls the same `_unresolved_conflict_ids` the
export gate used to call directly, and it was fixed alongside `create_export`
in name only: `create_export`'s 422 was filtered and `:close`'s was not, so
the fix for half two closed one of two identical channels. Measured, same
review, same owing lead, one session (the second audit's own repro):

    POST /exports          -> 422  "unresolved_conflicts": [],  "owed_hidden": 1   # fixed
    POST /reviews/{id}:close -> 422  "unresolved_conflicts": ["c2048d39-..."]      # NOT fixed

Not a full reopen of half two — `:unblind` above still refuses the id, so it
cannot become a record identity — but it is the same disclosure at the sibling
site, and it hands back a stable id the caller can poll as partners work. The
fix moved the owed-filter into `flow._unresolved_conflicts` itself (the
actor-aware wrapper around the unfiltered `_unresolved_conflict_ids`), so
`:close` gets it automatically rather than needing its own copy — see that
function's docstring for why a third caller could not reintroduce this by
reaching the unfiltered primitive without a very deliberate, differently-named
import.

## Why the fixture needs a second, LATER stage

A conflict is opened only when `reviewers_required` CURRENT decisions
disagree, and while a record's own highest stage carries an unresolved
disagreement it reads `not_yet_screened` there — `flow._unresolved_conflicts`'s
own docstring measures that this fires in NO single-stage flow. So a `POST
/exports` on a single-stage review with an open conflict never reaches the
conflicts gate this file is about; it refuses on `not_yet_screened` first. The
record here is deliberately settled by a LATER, higher stage (full text) while
title/abstract's disagreement stays open and unadjudicated — the same shape
`tests/integration/test_a_record_is_settled_by_its_latest_stage.py::
stranded_review` builds S1 with, and for the identical reason: it is the only
way to reach `_unresolved_conflicts` with something for it to refuse.
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

TENANT = tenant("tenant-owed-conflict-disclosure")
LEAD = "owed-disclosure-lead"
OTHER = "owed-disclosure-other"
THIRD = "owed-disclosure-third"
#: A second lead with no assignment anywhere in the review, used ONLY to read
#: the ground-truth conflict id while building the fixture — never as part of
#: the behaviour under test. Same role `WITNESS` plays in
#: `tests/security/test_one_stage_decided_is_not_every_stage_decided.py`.
WITNESS = "owed-disclosure-witness"


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


def _decide(  # noqa: ANN201
    client,
    review_id,
    *,
    stage_id,
    record_id,
    reviewer,
    disposition,
    reason=None,
    round_number=1,
    supersedes_decision_id=None,
):  # noqa: ANN001
    body = {
        "stage_id": stage_id,
        "round_number": round_number,
        "review_record_id": record_id,
        "disposition": disposition,
    }
    if reason is not None:
        body["exclusion_reason_code"] = reason
    if supersedes_decision_id is not None:
        body["supersedes_decision_id"] = supersedes_decision_id
    response = client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(reviewer))
    assert response.status_code == 201, response.text
    return response.json()


def _build_owed_ta_conflict_settled_at_ft(client) -> dict:  # noqa: ANN001
    """LEAD owes an open, never-adjudicated title/abstract conflict; the same
    record settled `included` at full text, so the review's `not_yet_screened`
    reads 0 while that title/abstract disagreement is still outstanding.

    LEAD is named in the title/abstract assignment (as a THIRD reviewer beside
    OTHER and THIRD) and deliberately never decides it — OTHER and THIRD alone
    meet `reviewers_required=2` and settle the record, exactly the shape
    `test_one_stage_decided_is_not_every_stage_decided.py::_conflicted_at_full_
    text` uses at full text. LEAD is never assigned to full text at all, so
    `ERS #65` finding 2's owing-reviewer exclusion has nothing to say about this
    fixture — LEAD's debt here is purely at title/abstract.
    """
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "owed-disclosure",
            "external_key": f"od-{uuid.uuid4()}",
            "title": "SECRET disputed record",
        },
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
            {"records": [{"title": "SECRET disputed record", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}
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
        json={"stage_id": ta, "reviewer_subject_ids": [LEAD, OTHER, THIRD]},
        headers=lead_headers(),
    )
    assert assigned_ta.status_code in (200, 201), assigned_ta.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": ta},
        headers=headers(OTHER),
    ).json()["items"][0]["review_record_id"]

    # OTHER and THIRD agree `include` — meets `reviewers_required=2`, settles
    # the record, opens no conflict. LEAD's assignment stays undecided.
    other_include = _decide(client, review_id, stage_id=ta, record_id=record_id, reviewer=OTHER, disposition="include")
    _decide(client, review_id, stage_id=ta, record_id=record_id, reviewer=THIRD, disposition="include")

    # NOW full text — the record has settled `included` at title/abstract, so
    # `ERS #42`'s eligibility scoping admits it. LEAD is not named here.
    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ft, "reviewer_subject_ids": [OTHER, THIRD]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text

    # THE DISAGREEMENT, BEFORE full text is decided: OTHER supersedes their
    # title/abstract `include` into `exclude`. This has to happen before the
    # full-text decisions below, not after — `settling_position` reads whichever
    # POSITION's own current decisions were touched most recently, so touching
    # title/abstract last would make the record settle THERE (in disagreement)
    # instead of at full text, and the fixture would never clear the
    # `not_yet_screened` gate to reach the one this file is about.
    _decide(
        client,
        review_id,
        stage_id=ta,
        record_id=record_id,
        reviewer=OTHER,
        disposition="exclude",
        reason="wrong_design",
        supersedes_decision_id=other_include["decision_id"],
    )

    # Full text, unanimous and LAST — the newest work, so this is what
    # `settling_position` reads.
    for reviewer in (OTHER, THIRD):
        _decide(client, review_id, stage_id=ft, record_id=record_id, reviewer=reviewer, disposition="include")

    # READ BY WITNESS, for the same reason the ground-truth read below is —
    # applied one statement earlier than it used to be. LEAD still owes a
    # full-text verdict on this record and OTHER and THIRD have both cast one,
    # so the ledger is now refused to LEAD: `flow.require_no_owed_decisions`
    # withholds the counts from a caller who would learn a partner's disposition
    # on a record they have yet to judge, which is the read-side twin of the
    # `/conflicts` withholding this whole file is about. WITNESS holds no
    # assignment, owes nothing, and reads it.
    counts = client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers(WITNESS)).json()["counts"]
    assert counts["not_yet_screened"] == 0, (
        f"the fixture's premise is that full text (the record's latest stage) settled it: {counts}"
    )

    # GROUND TRUTH, read by WITNESS — nothing under test is used to build the
    # test's own oracle.
    witness_conflicts = client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers(WITNESS)).json()
    conflict = next(c for c in witness_conflicts["items"] if c["stage_id"] == ta)
    assert not conflict["resolved"], f"the fixture's disagreement must still be open: {witness_conflicts}"

    lead_panel = client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    assert lead_panel["total_open"] == 0 and lead_panel["owed_hidden"] == 1, (
        "the fixture's premise is LEAD owing exactly this one conflict, withheld from their own panel "
        f"(the audit's own repro: total_open 0, owed_hidden 1): {lead_panel}"
    )

    return {"review_id": review_id, "conflict_id": conflict["conflict_id"], "ta_stage": ta, "record_id": record_id}


def test_self_unblind_of_an_owed_conflict_is_refused(client) -> None:  # noqa: ANN001
    """FINDING 1, half one. `unblind_conflict` had no owes-check.

    Before the fix: `disclose_to` defaults to the actor alone, so LEAD calling
    `:unblind` with no body but a reason unblinds the conflict TO THEMSELVES —
    200, and their own panel then serves the row it was built to withhold.
    """
    built = _build_owed_ta_conflict_settled_at_ft(client)

    refused = client.post(
        f"/api/reviews/{built['review_id']}/conflicts/{built['conflict_id']}:unblind",
        json={"reason": "self-service"},
        headers=lead_headers(),
    )
    assert refused.status_code == 409, (
        "LEAD owes this conflict's title/abstract position and unblinded it to themselves (the "
        f"default audience with no `disclose_to`); this must be refused, not granted: {refused.text}"
    )

    panel = client.get(f"/api/reviews/{built['review_id']}/conflicts", headers=lead_headers()).json()
    assert built["conflict_id"] not in {c["conflict_id"] for c in panel["items"]}, (
        f"the refused unblind must not have opened the row anyway: {panel}"
    )
    assert panel["owed_hidden"] == 1, f"the conflict must still read as withheld after the refusal: {panel}"


def test_exports_refusal_does_not_disclose_an_owed_conflicts_identity(client) -> None:  # noqa: ANN001
    """FINDING 1, half two. `POST /exports`' 422 named every conflict review-wide.

    Before the fix: `unresolved_conflicts` in the 422 body named this conflict
    to LEAD even though `GET /conflicts` — the route built to withhold it — was
    refusing to. That is the exact id the audit used to self-unblind with.

    ## ⚠️ THE MECHANISM MOVED, AND THE PROPERTY IS WHAT THIS TEST HOLDS

    This used to assert a `422` whose `unresolved_conflicts` omitted the owed
    id and whose `owed_hidden` was `1`. `flow.create_export` now holds
    `require_no_owed_decisions` BEFORE the conflicts gate, and LEAD's position
    here is exactly the condition that guard fires on — a conflict exists only
    where other reviewers hold current decisions, and LEAD owes that position —
    so LEAD is refused `403` and never reaches the 422 at all. The
    `owed_hidden` branch in `create_export` is therefore unreachable *at this
    route*, which its own comment now records.

    The assertion is written as the DISCLOSURE — the id is absent from whatever
    body comes back — so it survives either mechanism, and the status is pinned
    separately below as the one that shipped. What it no longer measures is the
    owed-FILTER inside `flow._unresolved_conflicts`:
    `test_close_refusal_does_not_disclose_an_owed_conflicts_identity` is now the
    only live exercise of it, because `:close` has no owed-guard in front of it.
    Deleting that test would leave the filter untested, not merely less tested.
    """
    built = _build_owed_ta_conflict_settled_at_ft(client)

    refused = client.post(f"/api/reviews/{built['review_id']}/exports", headers=lead_headers())
    blob = refused.text
    assert built["conflict_id"] not in blob, (
        f"LEAD owes this conflict and must not be handed its id by the export refusal: {blob}"
    )
    assert refused.status_code == 403, (
        "the export must be refused to a caller who owes a position other reviewers have judged, "
        f"before any gate that reports what is outstanding: {refused.status_code} {blob}"
    )
    body = refused.json()
    assert body.get("unresolved_conflicts") is None and body.get("owed_hidden") is None, (
        f"the blinding refusal carries the conflicts gate's own disclosure fields: {body}"
    )
    assert "excluded" not in blob and "not_yet_screened" not in blob, (
        f"the refusal body carries the ledger counts it exists to withhold: {blob}"
    )


def test_close_refusal_does_not_disclose_an_owed_conflicts_identity(client) -> None:  # noqa: ANN001
    """FINDING 1, half three (second audit). `:close`'s 422 named the SIBLING gate's leak.

    Before this fix: `reviews.transition(closed)` called
    `flow._unresolved_conflicts` (now `_unresolved_conflict_ids`, the unfiltered
    primitive) directly, unlike `create_export`, which by this point already
    filtered. So the export gate correctly withheld this exact id while `:close`
    handed it straight back — the identical disclosure `test_exports_refusal_
    does_not_disclose_an_owed_conflicts_identity` above closes, reopened one
    call site over.
    """
    built = _build_owed_ta_conflict_settled_at_ft(client)

    refused = client.post(
        f"/api/reviews/{built['review_id']}:close", json={"rationale": "done"}, headers=lead_headers()
    )
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert "disagreements are unresolved" in body["detail"], body

    assert built["conflict_id"] not in body.get("unresolved_conflicts", []), (
        f"LEAD owes this conflict and must not be handed its id by the close refusal: {body}"
    )
    assert body.get("owed_hidden") == 1, (
        "the refusal must still say a conflict was withheld, or a lead cannot reconcile this 422 "
        f"against their own `GET /conflicts` panel: {body}"
    )

    # AND THE REVIEW MUST STILL BE REFUSED CLOSE, not silently allowed through
    # while the id is merely hidden — dropping an owed conflict from the count
    # would be the exact "panel lies about completeness" defect `owed_hidden`
    # exists to prevent, this time at the gate that decides whether the review
    # can close at all.
    summary = client.get(f"/api/reviews/{built['review_id']}", headers=lead_headers()).json()
    assert summary["state"] == "active", f"the refused close must not have taken effect anyway: {summary}"
