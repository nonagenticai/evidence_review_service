"""A two-stage review must be able to finish — `ER-004.1`, `ER-005.1`, `ER-009`.

"Excluded at full text after passing title/abstract" is what a two-stage
systematic review is FOR. It is also the state the flow ledger could not
represent: `flow._collect` collected every reviewer's decision into
`decisions[review_record_id]` with `stage_id` and `round_number` thrown away, and
`flow_derivation.derive` then demanded unanimity over that MERGED cross-stage
list. Two stages, two reviewers, unanimous INCLUDE at title/abstract and
unanimous EXCLUDE at full text is four decision rows, two distinct dispositions,
and therefore `not_yet_screened` — while `_maybe_open_conflict`, which IS stage
keyed, correctly opened nothing.

The result was a review that could never export and had no honest remedy:

    GET  /conflicts  ->  {"items": [], "open": 0, "total_open": 0}
    GET  /flow       ->  records_screened 1, not_yet_screened 1
    POST /exports    ->  422 "1 records are not yet screened"

`:resolve` needs a `conflict_id` and none exists; the only escape was for both
reviewers to supersede their STAGE-ONE decisions, which retroactively falsifies
the title/abstract stage.

These tests are written against the API, not against `derive`, because the
defect lived in the seam between the two — `derive` was faithfully answering the
question `_collect` asked it.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-two-stage")
LEAD = "lead-dana"
REVIEWER_A = "reviewer-ada"
REVIEWER_B = "reviewer-bem"

#: Four records, no shared identifiers — deduplication is not what is under
#: test here and a merge would move a record out of the screening buckets.
CORPUS = {
    "records": [
        {"title": "Trial one: promising abstract, unobtainable full text", "doi": "10.1000/one", "year": 2021},
        {"title": "Trial two: good at both stages", "doi": "10.1000/two", "year": 2021},
        {"title": "Trial three: wrong design, visible from the abstract", "doi": "10.1000/three", "year": 2020},
        {"title": "Trial four: disputed at both stages", "doi": "10.1000/four", "year": 2022},
    ]
}


def headers(subject: str, *, tenant: str = TENANT, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": tenant, "X-Subject-Id": subject, "X-Actor-Role": role}


def lead_headers(tenant: str = TENANT) -> dict[str, str]:
    return headers(LEAD, tenant=tenant, role="lead")


@pytest.fixture(scope="module")
def app_client():  # noqa: ANN201
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"

    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


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
):  # noqa: ANN001, ANN202
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


@pytest.fixture(scope="module")
def two_stage_review(app_client):  # noqa: ANN201, ANN001
    """A review screened through BOTH stages, in the order a real one is.

    Module-scoped: every assertion below is about the state this run produced,
    and rebuilding it per test would be a different review.

    Stage 1 — title/abstract, both reviewers, unanimous throughout:

        R1 include   R2 include   R3 exclude(wrong_design)   R4 A:include B:exclude

    R4's disagreement opens a stage-1 conflict, which the lead adjudicates
    `include` — so R4 advances.

    Stage 2 — full text, assigned only over R1, R2 and R4 (`create_assignments`
    now scopes stage order N > 1 to records that settled `included` at stage
    N-1, `ERS #42` — R3 settled `excluded` above and is never offered a
    full-text slot):

        R1 exclude(not_retrievable) x2      R2 include x2
        R3 not assigned, no decisions       R4 A:exclude B:include

    R4's second disagreement opens a stage-2 conflict, adjudicated `exclude`.

    R3 is the crux: it was EXCLUDED at title/abstract and therefore never
    advanced. It has no full-text decision and must never acquire one, so a
    ledger that demanded a decision at the highest stage of the REVIEW rather
    than at the highest stage this RECORD reached would deadlock on it just as
    hard — one deadlock swapped for another.
    """
    external_key = f"two-stage-{uuid.uuid4()}"
    created = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": external_key, "title": "Two-stage screening"},
        headers=lead_headers(),
    )
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {"design": "randomised controlled trial"},
            "exclusion_criteria": {"design": "not a randomised controlled trial"},
            "stage_definitions": [
                {"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2},
                {"key": "full-text", "name": "Full text", "reviewers_required": 2},
            ],
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population", "not_retrievable"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]

    locked = app_client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=lead_headers())
    assert locked.status_code == 200, locked.text

    activated = app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "two-stage run"},
        headers=lead_headers(),
    )
    assert activated.status_code == 200, activated.text

    imported = app_client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["accepted_count"] == 4

    sealed = app_client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())
    assert sealed.status_code == 201, sealed.text

    stages = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()
    assert [s["stage_key"] for s in stages["items"]] == ["title-abstract", "full-text"], (
        "stages must come back in stage_order; the ledger's whole answer depends on that order"
    )
    ta_stage = stages["items"][0]["stage_id"]
    ft_stage = stages["items"][1]["stage_id"]

    # ---- stage 1: title/abstract -----------------------------------------
    assigned = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ta_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["assignments_created"] == 8

    queue = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={ta_stage}", headers=headers(REVIEWER_A)
    ).json()
    records = [item["review_record_id"] for item in queue["items"]]
    assert len(records) == 4
    r1, r2, r3, r4 = records

    plan_ta = {
        REVIEWER_A: [("include", None), ("include", None), ("exclude", "wrong_design"), ("include", None)],
        REVIEWER_B: [("include", None), ("include", None), ("exclude", "wrong_design"), ("exclude", "wrong_design")],
    }
    for reviewer, plan in plan_ta.items():
        for record_id, (disposition, reason) in zip(records, plan, strict=True):
            _decide(
                app_client,
                review_id,
                stage_id=ta_stage,
                record_id=record_id,
                reviewer=reviewer,
                disposition=disposition,
                reason=reason,
            )

    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    ta_conflicts = [c for c in conflicts["items"] if c["stage_id"] == ta_stage]
    assert len(ta_conflicts) == 1 and ta_conflicts[0]["review_record_id"] == r4, conflicts
    resolved = app_client.post(
        f"/api/reviews/{review_id}/conflicts/{ta_conflicts[0]['conflict_id']}:resolve",
        json={"disposition": "include", "rationale": "the abstract is ambiguous; retrieve the full text"},
        headers=lead_headers(),
    )
    assert resolved.status_code == 200, resolved.text

    # ---- stage 2: full text ----------------------------------------------
    assigned_ft = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ft_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code == 201, assigned_ft.text

    plan_ft = {
        REVIEWER_A: {r1: ("exclude", "not_retrievable"), r2: ("include", None), r4: ("exclude", "wrong_population")},
        REVIEWER_B: {r1: ("exclude", "not_retrievable"), r2: ("include", None), r4: ("include", None)},
    }
    for reviewer, plan in plan_ft.items():
        for record_id, (disposition, reason) in plan.items():
            _decide(
                app_client,
                review_id,
                stage_id=ft_stage,
                record_id=record_id,
                reviewer=reviewer,
                disposition=disposition,
                reason=reason,
            )

    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    ft_conflicts = [c for c in conflicts["items"] if c["stage_id"] == ft_stage]
    assert len(ft_conflicts) == 1 and ft_conflicts[0]["review_record_id"] == r4, conflicts
    resolved = app_client.post(
        f"/api/reviews/{review_id}/conflicts/{ft_conflicts[0]['conflict_id']}:resolve",
        json={
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_population",
            "rationale": "the full text shows a paediatric population",
        },
        headers=lead_headers(),
    )
    assert resolved.status_code == 200, resolved.text

    return {
        "review_id": review_id,
        "title_abstract_stage_id": ta_stage,
        "full_text_stage_id": ft_stage,
        "records": {"r1": r1, "r2": r2, "r3": r3, "r4": r4},
    }


def test_two_unanimous_stages_open_no_conflict(app_client, two_stage_review):
    """The premise: `_maybe_open_conflict` is stage keyed and was always right.

    Only R4 was ever disputed, once per stage, and both are adjudicated. R1 —
    unanimous `include` at title/abstract and unanimous `exclude` at full text —
    is not a disagreement between reviewers and there is nothing for a lead to
    resolve. The ledger disagreeing with that is what made the deadlock
    inescapable: `:resolve` needs a `conflict_id`, and for R1 none exists.
    """
    conflicts = app_client.get(f"/api/reviews/{two_stage_review['review_id']}/conflicts", headers=lead_headers()).json()
    assert conflicts["total"] == 2, conflicts
    assert conflicts["total_open"] == 0, conflicts
    assert {c["review_record_id"] for c in conflicts["items"]} == {two_stage_review["records"]["r4"]}


def test_a_record_excluded_at_full_text_is_excluded_not_unscreened(app_client, two_stage_review):
    """THE DEFECT. R1 passed title/abstract and failed full text.

    Four decision rows, unanimous within each stage. The merged list holds
    `{include, exclude}`, so the pre-fix derivation filed R1 as
    `not_yet_screened` — an assertion that nobody has judged a record that two
    reviewers judged twice each.
    """
    flow = app_client.get(f"/api/reviews/{two_stage_review['review_id']}/flow", headers=lead_headers())
    assert flow.status_code == 200, flow.text
    counts = flow.json()["counts"]

    assert counts["not_yet_screened"] == 0, (
        f"every record was decided at the stage it reached; counts={counts}. "
        "A record excluded at full text is EXCLUDED, not unscreened."
    )
    assert counts["records_total"] == 4
    assert counts["records_screened"] == 4
    assert counts["included"] == 1, "only R2 survives both stages"
    assert counts["excluded"] == 3, "R1 at full text, R3 at title/abstract, R4 by adjudication at full text"
    assert counts["uncertain"] == 0
    assert counts["adjudicated"] == 1, "R4 is settled by its FULL-TEXT adjudication, and by exactly one"


def test_the_collected_inputs_keep_every_stage_apart(two_stage_review):
    """The seam, asserted at the seam. `derive` was answering faithfully.

    Both defects are visible as sizes here, without any need to reason about
    which bucket anything lands in:

    * `decisions` held one entry per RECORD — 4 — so a record's title/abstract
      and full-text votes were the same list. It must hold one entry per
      `(stage, round, record)` that was actually worked: 4 at title/abstract and
      3 at full text, because R3 stopped;
    * `adjudications` held one entry per RECORD, so R4's two current
      adjudications — one per stage, both the tail of their own chain —
      collapsed to whichever conflict an unordered `select(ScreeningConflict)`
      yielded last.
    """
    from actor import Actor, ActorRole
    from application.services.flow import _collect
    from database import actor_scope

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        inputs = _collect(session, uuid.UUID(two_stage_review["review_id"]))

    assert len(inputs.decisions) == 7, (
        f"expected one vote-list per (stage, round, record) worked — 4 + 3 — got {len(inputs.decisions)}. "
        "4 means the key is the review_record_id and both stages share a list."
    )
    assert len(inputs.adjudications) == 2, (
        f"R4 has a CURRENT adjudication in each of two stages; {len(inputs.adjudications)} survived the "
        "keying. 1 means the ledger picked one of them by scan order."
    )
    assert inputs.reviewers_required_by_stage == {"title-abstract": 2, "full-text": 2}


@pytest.fixture(scope="module")
def export(app_client, two_stage_review):  # noqa: ANN201, ANN001
    """`POST /exports` — the call the whole defect made unreachable."""
    review_id = two_stage_review["review_id"]
    created = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert created.status_code == 201, created.text
    body = created.json()
    fetched = app_client.get(
        f"/api/reviews/{review_id}/exports/{body['export_id']}?with_payload=true", headers=lead_headers()
    )
    assert fetched.status_code == 200, fetched.text
    return {"summary": body, "payload": fetched.json()["payload"]}


def _omissions_by_record(export) -> dict:  # noqa: ANN001
    return {row["review_record_id"]: row for row in export["payload"]["omissions"] if "review_record_id" in row}


def test_a_record_excluded_at_title_abstract_is_not_unscreened_for_lack_of_a_full_text_decision(
    two_stage_review, export
):
    """THE CRUX. R3 never advanced, and must not be waited on.

    A record excluded at title/abstract is never retrieved, so a full-text
    decision on it will never exist. Settling a record from the review's highest
    stage rather than from the record's own highest DECIDED stage swaps this
    deadlock for an identical one, with the same absence of a remedy: there is
    no conflict to resolve and no honest decision to record.
    """
    r3 = two_stage_review["records"]["r3"]
    omitted = _omissions_by_record(export)
    assert omitted[r3]["bucket"] == "excluded", omitted[r3]
    assert omitted[r3]["exclusion_reason_code"] == "wrong_design"
    assert omitted[r3]["adjudicated"] is False


def test_the_export_seals_the_full_text_survivor_and_nothing_else(app_client, two_stage_review, export):
    """`ER-009` — the whole point of the two-stage review.

    Refused entirely before the fix: `POST /exports` answered 422 "1 records are
    not yet screened" for a review in which every record had been judged.
    """
    assert export["summary"]["included_count"] == 1
    assert export["summary"]["omissions_count"] == 3
    assert [row["review_record_id"] for row in export["payload"]["included"]] == [two_stage_review["records"]["r2"]]

    verified = app_client.get(
        f"/api/reviews/{two_stage_review['review_id']}/exports/{export['summary']['export_id']}:verify",
        headers=lead_headers(),
    ).json()
    assert verified["verified"] is True, verified


def test_a_record_disputed_in_two_stages_is_settled_by_the_later_adjudication(two_stage_review, export):
    """The second defect in the same lines, and it is a determinism bug.

    `_collect` built `adjudications[review_record_id]` from an UNORDERED
    `select(ScreeningConflict)`, so R4 — which has a conflict AND an
    adjudication in each of the two stages — collapsed to whichever conflict the
    scan happened to yield last. Both are `current`, so both are equally
    "correct" to a keying that cannot tell them apart, and the ledger could
    therefore answer `included` or `excluded` for the same immutable history.

    That is the property `flow_derivation`'s module docstring claims is
    impossible ("a projection over immutable history cannot drift"), and
    `canonical_counts_hash` is the thing that would have caught it — except that
    `flow:rebuild` compares a hash computed by the same scan-order-dependent
    code against a hash computed by it a moment earlier.
    """
    r4 = two_stage_review["records"]["r4"]
    omitted = _omissions_by_record(export)

    assert r4 in omitted, "R4 was adjudicated OUT at full text and must not be in the inclusion set"
    assert omitted[r4]["bucket"] == "excluded"
    assert omitted[r4]["adjudicated"] is True
    assert omitted[r4]["exclusion_reason_code"] == "wrong_population", (
        "the STAGE-ONE adjudication said include with no reason; reading it here means the "
        "ledger picked the wrong stage"
    )


def test_the_ledger_hash_is_stable_across_repeated_reads(app_client, two_stage_review):
    """Same immutable history, same hash — read it enough times to mean it.

    Ten reads rather than two: the pre-fix collapse was decided by physical scan
    order over two rows, which is stable within a session far more often than it
    is stable in general. This is deliberately a weak instrument, and it is here
    because a hash that moved would be unambiguous; the strong instrument is the
    test above, which asserts WHICH of the two adjudications wins.
    """
    review_id = two_stage_review["review_id"]
    hashes = {
        app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers()).json()["canonical_counts_hash"]
        for _ in range(10)
    }
    assert len(hashes) == 1, hashes

    rebuilt = app_client.post(f"/api/reviews/{review_id}/flow:rebuild", headers=lead_headers()).json()
    assert rebuilt["matches_previous_projection"] is True, rebuilt
    assert rebuilt["canonical_counts_hash"] in hashes


# ==========================================================================
# The adversarial half: what ELSE could a multi-stage review deadlock on?
#
# The fix is "settle a record from its highest DECIDED stage". Two states it
# does not settle are reachable, and neither is a deadlock — the difference
# from the defect this file exists for is that both name a remedy the lead or
# the reviewers can actually perform. Asserted rather than argued, because
# "escapable in principle" is what the old code's `:resolve` advice was too.
# ==========================================================================


@pytest.fixture(scope="module")
def stranded_review(app_client):  # noqa: ANN201, ANN001
    """A second review, built to sit in both of the awkward states at once.

    S1 — a title/abstract disagreement left OPEN, then screened unanimously at
    full text anyway. `derive` settles it from full text and reports nothing
    outstanding; `_unresolved_conflicts` still refuses the export, because an
    export asserts the review's disagreements were resolved and one was not.

    S2 — excluded at title/abstract, and then ONE reviewer records a full-text
    decision on it. The record's highest decided stage is now full text and
    that stage is half done, so it is `not_yet_screened`. A decision cannot be
    retracted — a correction is a new row at the same position — so the ONLY
    exit is the partner deciding too. That exit exists, and this fixture's
    tests take it.

    ## The bootstrap round, and why it is a SUPERSEDE, not a second round
    ##
    ## (⚠️ CORRECTED — this used to be a throwaway `round_number=99`, and
    ## `ERS #65` finding 2 retired that shape.)

    `create_assignments` now scopes stage order N > 1 to records that settled
    `included` at stage N-1 (`ERS #42`), so a full-text assignment can no
    longer be minted over a record that has not yet settled `included` at
    title/abstract — which is exactly the state both S1 (an open disagreement)
    and S2 (excluded) are in by design.

    This used to sidestep that with a THROWAWAY ROUND (`round_number=99`):
    both reviewers unanimously included both records there, the full-text
    assignment was minted while only that round's data existed, and the real
    per-record decisions below were taken afterwards, on round 1, as fresh
    (never-superseded) rows. `ERS #65` finding 2 closed a DIFFERENT hole with
    the same shape: `create_assignments` now also excludes any named reviewer
    who still owes a decision at stage N-1, and at the moment that throwaway
    round's full-text assignment was requested, REVIEWER_A and REVIEWER_B each
    held an undecided ROUND-1 title/abstract assignment on both records — the
    round-99 vote did not discharge it, because owing is computed per
    `(stage, round, record)`, exactly like `records_the_caller_still_owes`.
    The fixture's own bootstrap had become an instance of the thing finding 2
    refuses, and the full-text assignment would mint for nobody.

    So the bootstrap is round 1 now, for real, and the per-record scenarios
    are built by SUPERSEDING it rather than by deciding a fresh round
    afterwards: both reviewers genuinely decide `include` on both records at
    round 1 (discharging the debt, so the full-text assignment below succeeds
    for both), and the corrections that follow supersede those exact rows.
    This settles nothing new either — a supersession IS the newest decision at
    its position, so `settling_position` reads the correction, not the
    bootstrap vote, exactly as the old two-round version intended — and the
    full-text ASSIGNMENT it unlocked is still append-only and does not
    un-happen when a later supersession changes the record's title/abstract
    state.
    """
    external_key = f"stranded-{uuid.uuid4()}"
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": external_key, "title": "Stranded shapes"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {"design": "randomised controlled trial"},
            "exclusion_criteria": {"design": "not a randomised controlled trial"},
            "stage_definitions": [
                {"key": "title-abstract", "name": "Title/abstract"},
                {"key": "full-text", "name": "Full text"},
            ],
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population", "not_retrievable"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    ).json()["protocol_version_id"]
    app_client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=lead_headers())
    app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "stranded shapes"},
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "Stranded one: disputed early, screened late", "doi": "10.2000/one", "year": 2021},
                    {"title": "Stranded two: excluded early, touched late", "doi": "10.2000/two", "year": 2021},
                ]
            }
        ),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    app_client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())

    stages = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()["items"]
    ta_stage, ft_stage = stages[0]["stage_id"], stages[1]["stage_id"]

    # ROUND 1: the real title/abstract assignment, needed both to identify the
    # two records below and to carry S1's and S2's actual scenario decisions
    # further down.
    app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ta_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )

    queue = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={ta_stage}", headers=headers(REVIEWER_A)
    ).json()
    s1, s2 = (item["review_record_id"] for item in queue["items"])

    # THE BOOTSTRAP: both records, unanimously included, on ROUND 1 — for real,
    # not a throwaway round. See the docstring's "The bootstrap round" for why
    # this has to discharge the reviewers' ACTUAL round-1 title/abstract debt
    # rather than a separate round's: it is what lets the full-text assignment
    # below succeed under `ERS #65` finding 2's owing-reviewer exclusion.
    bootstrap_decision_id = {}
    for reviewer in (REVIEWER_A, REVIEWER_B):
        bootstrap_decision_id[reviewer] = {}
        for record_id in (s1, s2):
            decided = _decide(
                app_client,
                review_id,
                stage_id=ta_stage,
                record_id=record_id,
                reviewer=reviewer,
                disposition="include",
            )
            bootstrap_decision_id[reviewer][record_id] = decided["decision_id"]

    # Now that both reviewers have genuinely decided (and both records read
    # `included` at) title/abstract, full text can be assigned to both of them.
    app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ft_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )

    # S1: an open disagreement at title/abstract — REVIEWER_B corrects their
    # bootstrap `include` into an `exclude`, superseding it. REVIEWER_A's
    # bootstrap `include` is untouched and stands as their real vote.
    _decide(
        app_client,
        review_id,
        stage_id=ta_stage,
        record_id=s1,
        reviewer=REVIEWER_B,
        disposition="exclude",
        reason="wrong_design",
        supersedes_decision_id=bootstrap_decision_id[REVIEWER_B][s1],
    )
    # ...screened unanimously at full text regardless.
    for reviewer in (REVIEWER_A, REVIEWER_B):
        _decide(app_client, review_id, stage_id=ft_stage, record_id=s1, reviewer=reviewer, disposition="include")

    # S2: excluded at title/abstract — BOTH reviewers correct their bootstrap
    # `include` into an `exclude`, superseding it — then one stray full-text
    # decision.
    for reviewer in (REVIEWER_A, REVIEWER_B):
        _decide(
            app_client,
            review_id,
            stage_id=ta_stage,
            record_id=s2,
            reviewer=reviewer,
            disposition="exclude",
            reason="wrong_design",
            supersedes_decision_id=bootstrap_decision_id[reviewer][s2],
        )
    _decide(
        app_client,
        review_id,
        stage_id=ft_stage,
        record_id=s2,
        reviewer=REVIEWER_A,
        disposition="exclude",
        reason="not_retrievable",
    )

    return {"review_id": review_id, "ta_stage": ta_stage, "ft_stage": ft_stage, "s1": s1, "s2": s2}


def test_a_stray_later_decision_leaves_the_record_open_and_says_so(app_client, stranded_review):
    """S2. Half a full-text stage is `not_yet_screened`, and the export says which.

    The negative control on the fix: settling from the record's latest stage must
    not fall BACK to an earlier settled stage when the latest one is in progress.
    Falling back would report S2 `excluded` while a full-text stage was still
    running on it, which is a review closing over a stage nobody finished.
    """
    counts = app_client.get(f"/api/reviews/{stranded_review['review_id']}/flow", headers=lead_headers()).json()[
        "counts"
    ]
    assert counts["not_yet_screened"] == 1, counts

    refused = app_client.post(f"/api/reviews/{stranded_review['review_id']}/exports", headers=lead_headers())
    assert refused.status_code == 422, refused.text
    assert refused.json()["not_yet_screened"] == 1


def test_an_open_earlier_conflict_still_refuses_the_export_and_names_it(app_client, stranded_review):
    """S1. `_unresolved_conflicts` is the backstop, and it is doing real work.

    S2's exit is taken first — the partner records the full-text decision they
    owe — so `not_yet_screened` reaches 0 and the export gets past the counts
    gate. It must still be refused: S1's settling stage is full text and the
    reviewers agreed there, but a title/abstract conflict the lead never
    adjudicated is not made resolved by a later stage going smoothly, and an
    export asserts that the review's disagreements were resolved.

    The refusal names a `conflict_id`, which is precisely what the cross-stage
    deadlock never had. That is the whole difference between a workflow state
    and a defect.
    """
    review_id = stranded_review["review_id"]

    _decide(
        app_client,
        review_id,
        stage_id=stranded_review["ft_stage"],
        record_id=stranded_review["s2"],
        reviewer=REVIEWER_B,
        disposition="exclude",
        reason="not_retrievable",
    )
    counts = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers()).json()["counts"]
    assert counts["not_yet_screened"] == 0, counts

    refused = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert refused.status_code == 422, refused.text
    assert "conflicts are unresolved" in refused.json()["detail"], refused.text

    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    assert conflicts["total_open"] == 1, conflicts
    open_conflict = next(c for c in conflicts["items"] if not c["resolved"])
    assert open_conflict["review_record_id"] == stranded_review["s1"]
    assert open_conflict["stage_id"] == stranded_review["ta_stage"], "the conflict is the TITLE/ABSTRACT one"
    assert refused.json()["unresolved_conflicts"] == [open_conflict["conflict_id"]], (
        "the refusal must name the conflict the lead can act on; a 422 with no id is the deadlock again"
    )


def test_both_stranded_shapes_have_an_exit_and_the_review_then_exports(app_client, stranded_review):
    """Take the last exit, and prove the review finishes. This is the whole test.

    A state nobody can leave is the defect; a state with a named remedy is a
    workflow. The lead adjudicates the title/abstract conflict on S1 and
    `POST /exports` returns 201 — no supersession of anybody's earlier decision,
    and no stage retroactively falsified.
    """
    review_id = stranded_review["review_id"]

    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    open_conflict = next(c for c in conflicts["items"] if not c["resolved"])
    resolved = app_client.post(
        f"/api/reviews/{review_id}/conflicts/{open_conflict['conflict_id']}:resolve",
        json={"disposition": "include", "rationale": "retrieved and eligible; the abstract was thin"},
        headers=lead_headers(),
    )
    assert resolved.status_code == 200, resolved.text

    export = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert export.status_code == 201, export.text
    assert export.json()["included_count"] == 1, "S1 survived full text; S2 was excluded at title/abstract"
    assert export.json()["omissions_count"] == 1


def test_the_earlier_adjudication_does_not_override_the_later_stage(app_client, stranded_review):
    """S1 is `included` because FULL TEXT included it, not because the lead did.

    The lead's adjudication settles the title/abstract conflict, at
    `stage_order` 1. The record's settling position is still full text, so the
    ledger must read the reviewers' full-text agreement — `adjudicated` false —
    rather than the adjudication one stage below it. Keying adjudications by
    record alone got this backwards for free.
    """
    review_id = stranded_review["review_id"]
    export = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert export.status_code == 201, export.text
    payload = app_client.get(
        f"/api/reviews/{review_id}/exports/{export.json()['export_id']}?with_payload=true", headers=lead_headers()
    ).json()["payload"]

    assert [row["review_record_id"] for row in payload["included"]] == [stranded_review["s1"]]
    counts = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers()).json()["counts"]
    assert counts["adjudicated"] == 0, (
        f"S1 was settled by its FULL-TEXT decisions; an `adjudicated` of {counts['adjudicated']} means "
        "the title/abstract adjudication was read as the record's disposition"
    )
