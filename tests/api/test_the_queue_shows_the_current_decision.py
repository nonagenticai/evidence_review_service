"""A corrected decision is never shown, and it never reaches the sealed export.

## The defect

`screening_decisions` is append-only and a correction is a NEW row carrying
`supersedes_decision_id` = the row it replaces. The row it replaces is not
touched — it cannot be, `trg_screening_decisions_immutable` refuses the UPDATE —
so **the original keeps its `NULL`** and the correction does not.

Every read that wanted "the current decision" asked for `supersedes_decision_id
IS NULL`, which selects the **first** row of a supersession chain. So after a
reviewer corrects `include` -> `uncertain`:

* the queue keeps rendering `include` as `my_decision`;
* `GET /decisions` keeps returning the superseded row;
* `record_decision` compares the caller's `supersedes_decision_id` against that
  same first row, so a **second** correction is refused with *"the decision being
  superseded is not your current decision"* — the id the service accepts is the
  one the client has no reason to send, and the id it returned for the correction
  is the one it rejects;
* and — the one that reaches the customer-visible artifact — the flow ledger and
  `POST /exports` derive their buckets from
  `blinded_reads.unblinded_current_decisions`, which carries the same predicate.
  **A record the reviewer corrected out of the inclusion set is sealed into it.**

## Why the counters are NOT part of this

`own_progress_counts` and `records_the_caller_still_owes` also carry
`supersedes_decision_id IS NULL`, and both are **correct with it**: every chain
has exactly one head, `uq_decision_one_current_per_reviewer` guarantees one head
per (record, reviewer), so counting heads counts decided RECORDS. They are
deliberately left alone, and `test_a_corrected_decision_is_counted_once_not_twice`
already pins that. The predicate is only wrong where the row's *content* is read.

## What makes the fix cheap

No migration and no new column. `uq_decision_supersession_linear`
(`0005_er_m005_screening.py:201-203`) is already a UNIQUE index on
`supersedes_decision_id WHERE NOT NULL`, so a decision can be superseded at most
once: every chain is linear and has exactly one tail. "Current" is therefore
expressible as *"no row supersedes me"*, and that same index serves the anti-join.

The RLS policy cannot starve the anti-join either: `screening_decisions_blinded`
(`0008_er_m008_unblinding_audience.py:148-167`) keys off `tenant_id`,
`reviewer_subject_id`, `app.actor_role` and the record's unblinding audience —
all four identical for a decision and its successor, which share the reviewer,
the tenant and the (review, stage, round, record). A visible predecessor always
has a visible successor.

## Measured on `0885857`, before the fix

    test_the_queue_shows_the_corrected_disposition              'include' != 'uncertain'
    test_the_decisions_list_returns_the_correction_not_the_original   the original row
    test_the_full_chain_is_still_readable_when_current_only_is_off    only the original row
    test_a_second_correction_is_accepted                       409 "not your current decision"
    test_superseding_an_already_superseded_decision_is_refused  409, but raw Postgres:
        'duplicate key value violates unique constraint "uq_decision_supersession_linear"'
    test_a_decision_with_no_supersedes_is_still_refused_after_a_correction
        409 carrying the SUPERSEDED id as `current_decision_id`
    test_the_flow_ledger_counts_the_corrected_disposition       4 included, 0 excluded
    test_the_export_seals_the_corrected_disposition             included_count 4, not 3

⚠️ One thing the report that filed this defect got wrong, corrected by measuring:
re-superseding an already-superseded row does **not** fork the chain. It is
refused — by `uq_decision_supersession_linear`, as a raw
`duplicate key value violates unique constraint` on the wire, which leaks an
internal constraint name and says nothing a client can act on. The data is safe;
the error is not the one the service has a sentence for. After the fix the
`record_decision` check refuses it first, by name, before the write.
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

TENANT = tenant("tenant-current-decision")
LEAD = "current-decision-lead"
REVIEWER = "current-decision-reviewer"

RECORDS = 4


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def reviewer_headers() -> dict[str, str]:
    return headers(REVIEWER, "reviewer")


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


def _activated_review(client, slug: str, *, reviewers_required: int = 1) -> dict:  # noqa: ANN001
    """One activated review with `RECORDS` records, all assigned to `REVIEWER`.

    Function-scoped by every caller on purpose: these tests mutate decisions and
    one of them seals an export, so sharing a review between them would make the
    order they run in load-bearing.
    """
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "current-decision",
            "external_key": f"{slug}-{uuid.uuid4()}",
            "title": "The queue must show the current decision",
        },
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": reviewers_required,
        },
        headers=headers(),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Paper {n}", "year": 2000 + n, "doi": f"10.1/cd{uuid.uuid4()}"} for n in range(RECORDS)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    # Every reviewer the stage requires, named in ONE call: `create_assignments`
    # refuses a partial roster ("this stage requires 2 reviewers; 1 were named").
    reviewers = [REVIEWER, *(f"{REVIEWER}-{n}" for n in range(2, reviewers_required + 1))]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": reviewers},
        headers=headers(),
    )
    assert assigned.status_code == 201, assigned.text
    return {"review_id": review_id, "stage_id": stage_id, "reviewers": reviewers}


@pytest.fixture
def fx(client) -> dict:  # noqa: ANN001
    return _activated_review(client, "fx")


def queue(client, fx: dict, **params) -> dict:  # noqa: ANN001, ANN003
    response = client.get(
        f"/api/reviews/{fx['review_id']}/screening/queue",
        params={"stage_id": fx["stage_id"], **params},
        headers=reviewer_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def records_of(client, fx: dict) -> list[str]:  # noqa: ANN001
    return [item["review_record_id"] for item in queue(client, fx)["items"]]


def decide(  # noqa: ANN001
    client,
    fx: dict,
    review_record_id: str,
    *,
    disposition: str = "include",
    exclusion_reason_code: str | None = None,
    supersedes_decision_id: str | None = None,
    expect: int = 201,
):
    body: dict[str, object] = {
        "stage_id": fx["stage_id"],
        "round_number": 1,
        "review_record_id": review_record_id,
        "disposition": disposition,
    }
    if exclusion_reason_code is not None:
        body["exclusion_reason_code"] = exclusion_reason_code
    if supersedes_decision_id is not None:
        body["supersedes_decision_id"] = supersedes_decision_id
    response = client.post(f"/api/reviews/{fx['review_id']}/decisions", json=body, headers=reviewer_headers())
    assert response.status_code == expect, response.text
    return response


def my_decision(client, fx: dict, review_record_id: str) -> dict | None:  # noqa: ANN001
    for item in queue(client, fx)["items"]:
        if item["review_record_id"] == review_record_id:
            return item["my_decision"]
    raise AssertionError(f"{review_record_id} is not in the queue")


# ---------------------------------------------------------------------------
# 1. What the reviewer reads back
# ---------------------------------------------------------------------------


def test_the_queue_shows_the_corrected_disposition(client, fx) -> None:  # noqa: ANN001
    """The reviewer's own screen must agree with the reviewer's own last word.

    This is the whole defect at its smallest: decide `include`, correct to
    `uncertain`, read the queue. A reviewer who cannot see that their correction
    landed will make it again — and the second attempt is refused, so the screen
    says `include` and the service says "you already changed that".
    """
    record = records_of(client, fx)[0]
    first = decide(client, fx, record).json()
    correction = decide(client, fx, record, disposition="uncertain", supersedes_decision_id=first["decision_id"]).json()

    shown = my_decision(client, fx, record)
    assert shown is not None, "the record reads as undecided after two decisions"
    assert shown["disposition"] == "uncertain", (
        f"the queue shows {shown['disposition']!r} after the reviewer corrected it to 'uncertain'. "
        "`supersedes_decision_id IS NULL` selects the FIRST row of the chain, not the current one."
    )
    assert shown["decision_id"] == correction["decision_id"]
    assert shown["supersedes_decision_id"] == first["decision_id"]


def test_the_decisions_list_returns_the_correction_not_the_original(client, fx) -> None:  # noqa: ANN001
    """`GET /decisions` with `current_only` must mean the tail of the chain.

    Asserted separately from the queue because they are different code paths —
    `visible_decisions` and `own_decisions_for` — and an earlier fix to one of
    them would have left the other lying.
    """
    record = records_of(client, fx)[0]
    first = decide(client, fx, record, disposition="exclude", exclusion_reason_code="wrong_design").json()
    correction = decide(client, fx, record, disposition="include", supersedes_decision_id=first["decision_id"]).json()

    items = client.get(
        f"/api/reviews/{fx['review_id']}/decisions?review_record_id={record}",
        headers=reviewer_headers(),
    ).json()["items"]
    assert [i["decision_id"] for i in items] == [correction["decision_id"]], items
    assert items[0]["disposition"] == "include"


def test_the_full_chain_is_still_readable_when_current_only_is_off(client, fx) -> None:  # noqa: ANN001
    """The negative control for the two above, asserted at the service, not the route.

    Making `current_only` mean "the tail" must not delete the audit trail. With
    the flag off both rows are still returned, oldest first — a "fix" that made
    the superseded row unreadable would pass every other test in this file and
    destroy the reason the table is append-only.

    ⚠️ Asserted through `blinded_reads.visible_decisions` directly because
    **no route exposes `current_only` today**: `list_decisions`
    (`screening.py:285-290`) takes only `review_record_id` and always takes the
    default. That is not a regression from this change — before it, exactly one
    row per chain reached the wire (the head); after it, exactly one does (the
    tail). Whether the superseded rows should be reachable over HTTP is a
    product question and is deliberately NOT answered here. This test pins that
    the capability still exists underneath, so answering it later is a router
    change and not an archaeology exercise.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from actor import Actor, ActorRole
    from application.services import blinded_reads
    from database import actor_scope

    record = records_of(client, fx)[0]
    first = decide(client, fx, record).json()
    correction = decide(client, fx, record, disposition="uncertain", supersedes_decision_id=first["decision_id"]).json()

    actor = Actor(tenant_id=TENANT, subject_id=REVIEWER, role=ActorRole.REVIEWER)
    with actor_scope(actor) as session:
        every = blinded_reads.visible_decisions(
            session,
            review_id=uuid.UUID(fx["review_id"]),
            actor=actor,
            review_record_id=uuid.UUID(record),
            current_only=False,
        )
        current = blinded_reads.visible_decisions(
            session,
            review_id=uuid.UUID(fx["review_id"]),
            actor=actor,
            review_record_id=uuid.UUID(record),
        )

    assert [str(d.decision_id) for d in every] == [first["decision_id"], correction["decision_id"]], every
    assert [str(d.decision_id) for d in current] == [correction["decision_id"]], current


# ---------------------------------------------------------------------------
# 2. What the reviewer is allowed to do next
# ---------------------------------------------------------------------------


def test_a_second_correction_is_accepted(client, fx) -> None:  # noqa: ANN001
    """A reviewer may change their mind twice.

    `record_decision` validates the caller's `supersedes_decision_id` against
    `own_decision_for`, so with the first-row predicate the only value it will
    accept is the ORIGINAL — which is exactly the value a client cannot know is
    right, because the 201 it received for the correction returned the
    correction's id.
    """
    record = records_of(client, fx)[0]
    first = decide(client, fx, record).json()
    correction = decide(client, fx, record, disposition="uncertain", supersedes_decision_id=first["decision_id"]).json()
    second = decide(
        client,
        fx,
        record,
        disposition="exclude",
        exclusion_reason_code="wrong_design",
        supersedes_decision_id=correction["decision_id"],
    ).json()

    assert second["supersedes_decision_id"] == correction["decision_id"]
    shown = my_decision(client, fx, record)
    assert shown is not None and shown["disposition"] == "exclude", shown


def test_superseding_an_already_superseded_decision_is_refused(client, fx) -> None:  # noqa: ANN001
    """The chain must not fork — and the refusal must be the service's, not Postgres's.

    With the first-row predicate the ORIGINAL is what `record_decision` accepts
    forever, so a client replaying the id it first received tries to insert a
    second row superseding the same parent. Measured on `0885857`, the data is
    safe: `uq_decision_supersession_linear` refuses it. But the refusal arrives
    as `duplicate key value violates unique constraint
    "uq_decision_supersession_linear"` — an internal identifier on the wire, and
    a sentence no client can act on.

    After the fix, `record_decision`'s own check refuses it first, by name,
    before the write. The index stays as the backstop it was always meant to be.
    """
    record = records_of(client, fx)[0]
    first = decide(client, fx, record).json()
    decide(client, fx, record, disposition="uncertain", supersedes_decision_id=first["decision_id"])

    response = decide(
        client, fx, record, disposition="include", supersedes_decision_id=first["decision_id"], expect=409
    )
    assert "current decision" in response.text, response.text


def test_a_decision_with_no_supersedes_is_still_refused_after_a_correction(client, fx) -> None:  # noqa: ANN001
    """The plain "you already decided this" path must survive the change.

    It reports `current_decision_id`, and that id must now be the tail —
    otherwise the error hands the client the one value the service will reject.
    """
    record = records_of(client, fx)[0]
    first = decide(client, fx, record).json()
    correction = decide(client, fx, record, disposition="uncertain", supersedes_decision_id=first["decision_id"]).json()

    response = decide(client, fx, record, disposition="exclude", exclusion_reason_code="wrong_design", expect=409)
    assert correction["decision_id"] in response.text, response.text
    assert first["decision_id"] not in response.text, (
        "the 409 offers the superseded decision id, which the service will then refuse: " + response.text
    )


# ---------------------------------------------------------------------------
# 2b. What this change makes REACHABLE, pinned rather than decided
# ---------------------------------------------------------------------------


def test_a_correction_that_restores_agreement_stops_blocking_the_export(client) -> None:  # noqa: ANN001
    """Consensus needs no adjudicator — but the conflict stays on the record.

    Serving the current decision makes something reachable that never was: a
    correction can move a record INTO disagreement, which opens a real conflict
    (`_maybe_open_conflict` reads `unblinded_current_decisions`) — correct, and
    the half this test asserts first. Correcting straight back to agreement
    cannot close it, because `screening_conflicts` is append-only and
    `0005_er_m005_screening.py:229` says *"resolution is an adjudication row,
    not a status update"*.

    So the export gate asks the question it actually needs — *is there still
    something for a lead to decide?* — rather than *"has an adjudication been
    written"*. A reviewer who mis-clicks and fixes it costs the lead nothing;
    without this, `POST /exports` refuses the review forever over a
    disagreement that does not exist.

    **Nothing is retracted.** The conflict row, its `opened_at`, its appearance
    in `GET /conflicts` and the full decision chain that opened and closed it
    all survive — asserted below, because "stops blocking" quietly becoming
    "disappears" would delete the audit trail this table exists to keep.
    """
    fx = _activated_review(client, "conflict-pin", reviewers_required=2)
    records = records_of(client, fx)
    record = records[0]
    other = fx["reviewers"][1]

    def other_decides(review_record_id: str, disposition: str, supersedes: str | None = None):  # noqa: ANN202
        body: dict[str, object] = {
            "stage_id": fx["stage_id"],
            "round_number": 1,
            "review_record_id": review_record_id,
            "disposition": disposition,
        }
        if supersedes is not None:
            body["supersedes_decision_id"] = supersedes
        response = client.post(
            f"/api/reviews/{fx['review_id']}/decisions", json=body, headers=headers(other, "reviewer")
        )
        assert response.status_code == 201, response.text
        return response.json()

    # EVERY record screened by BOTH reviewers, in agreement. An unscreened
    # record is refused by an earlier gate in `create_export` and would mask
    # everything this test is about.
    firsts = {}
    for rid in records:
        firsts[rid] = decide(client, fx, rid).json()
        other_decides(rid, "include")
    assert client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers()).json()["count"] == 0, (
        "agreement must not open a conflict"
    )
    settled = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers())
    assert settled.status_code == 201, "the review must be exportable BEFORE the conflict: " + settled.text

    # Into disagreement...
    correction = decide(
        client, fx, record, disposition="uncertain", supersedes_decision_id=firsts[record]["decision_id"]
    ).json()
    opened = client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers()).json()
    assert opened["count"] == 1, "a correction into disagreement must open a conflict — that half is right"

    opened_at = opened["items"][0]["opened_at"]
    conflict_id = opened["items"][0]["conflict_id"]

    # ...and the export is refused while it stands. The negative control:
    # without this, a gate that stopped blocking entirely would pass below.
    #
    # It refuses as NOT YET SCREENED, not as "conflicts are unresolved", and
    # that is worth asserting exactly rather than loosely: `flow_derivation`
    # calls a record screened only when an adjudication exists or
    # `reviewers_required` current decisions AGREE, so a live disagreement is
    # caught by the earlier gate and `_unresolved_conflicts` is never reached.
    # The only state that ever reached it is the one this test unblocks.
    blocked = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers())
    assert blocked.status_code == 422, blocked.text
    assert "not yet screened" in blocked.text, blocked.text

    # ...and straight back out of it.
    decide(client, fx, record, disposition="include", supersedes_decision_id=correction["decision_id"])

    # The conflict is NOT retracted — it is still on the record, with the same
    # id and the same `opened_at`. Only its power to block is gone.
    still = client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers()).json()
    assert still["count"] == 1, "the conflict must remain visible; consensus closes it, it does not erase it"
    assert still["items"][0]["conflict_id"] == conflict_id
    assert still["items"][0]["opened_at"] == opened_at

    # And the export goes through, with no adjudication ever written.
    export = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers())
    assert export.status_code == 201, (
        "the reviewers agree again and there is nothing for a lead to decide, yet the export is "
        "still refused: " + export.text
    )
    assert export.json()["included_count"] == RECORDS, export.json()

    flow = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers()).json()["counts"]
    assert flow["adjudicated"] == 0, "consensus must not be recorded as an adjudication that never happened"


# ---------------------------------------------------------------------------
# 3. What the customer receives — the ledger and the seal
# ---------------------------------------------------------------------------


def test_the_flow_ledger_counts_the_corrected_disposition(client, fx) -> None:  # noqa: ANN001
    """`_collect` reads `unblinded_current_decisions`, which carried the same predicate.

    Four records, all decided `include`, then one corrected to `exclude`. The
    ledger must read 3 included / 1 excluded.
    """
    records = records_of(client, fx)
    firsts = [decide(client, fx, r).json() for r in records]
    decide(
        client,
        fx,
        records[0],
        disposition="exclude",
        exclusion_reason_code="wrong_design",
        supersedes_decision_id=firsts[0]["decision_id"],
    )

    counts = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers()).json()["counts"]
    assert counts["included"] == RECORDS - 1, counts
    assert counts["excluded"] == 1, counts
    assert counts["not_yet_screened"] == 0, counts
    assert counts["exclusion_reasons"] == {"wrong_design": 1}, counts


def test_the_export_seals_the_corrected_disposition(client, fx) -> None:  # noqa: ANN001
    """The artifact the whole service exists to produce.

    A record the reviewer corrected OUT of the inclusion set must not be sealed
    into it. This is the assertion that makes the defect a data-integrity
    problem rather than a display one: the export is hash-sealed and
    append-only, so a wrong `included` array is wrong permanently and is the
    thing that gets cited.
    """
    records = records_of(client, fx)
    firsts = [decide(client, fx, r).json() for r in records]
    decide(
        client,
        fx,
        records[0],
        disposition="exclude",
        exclusion_reason_code="wrong_design",
        supersedes_decision_id=firsts[0]["decision_id"],
    )

    export = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers())
    assert export.status_code == 201, export.text
    summary = export.json()
    assert summary["included_count"] == RECORDS - 1, summary
    assert summary["omissions_count"] == 1, summary

    payload = client.get(
        f"/api/reviews/{fx['review_id']}/exports/{summary['export_id']}",
        headers=headers(),
    ).json()["payload"]
    included = {row["review_record_id"] for row in payload["included"]}
    assert records[0] not in included, "the record the reviewer corrected to 'exclude' is sealed into the inclusion set"
    assert included == set(records[1:]), included

    omitted = {row["review_record_id"]: row for row in payload["omissions"]}
    assert set(omitted) == {records[0]}, omitted
    assert omitted[records[0]]["bucket"] == "excluded", omitted[records[0]]

    # The seal is over what was actually sealed, so it must still reproduce.
    verified = client.get(
        f"/api/reviews/{fx['review_id']}/exports/{summary['export_id']}:verify",
        headers=headers(),
    ).json()
    assert verified["verified"] is True, verified
