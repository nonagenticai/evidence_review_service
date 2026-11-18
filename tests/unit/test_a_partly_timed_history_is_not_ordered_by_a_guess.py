"""A record whose positions are only PARTLY timed has no settling stage.

The ledger settles a record at its NEWEST WORK, read from
`screening_decisions.decided_at` (`#42`). `settling_position` uses time only when
EVERY position the record reached carries one, because ranking an untimed
position against timed ones is a guess about ordering — and the whole reason the
stage order stopped being the answer is that a stage order cannot say which of
two pieces of work came later once rounds exist.

That rule had two arms and needed three. Partial coverage fell through to the
same `max(reached)` as no coverage at all, which is not a degraded answer for the
untimed position — it discards the timed ordering for the WHOLE record.

Measured on `4349131`, one record, decisions at title/abstract round 1, full text
round 1 and title/abstract round 2 (the re-screen, unanimous `exclude`, and the
newest work the review contains), every one of them timed:

    FULL coverage:    excluded 1, not_yet_screened 0, settled_at ta/round 2

Add ONE untimed position — an adjudication at full text round 2, which
contributes a position but deliberately no time (`flow._collect`) — and nothing
else changes:

    PARTIAL coverage: included 1, excluded 0, not_yet_screened 0, settled_at ft/round 2
                      exclusion_reasons {}

The record flips from excluded to included, the reason vanishes, and
`not_yet_screened` is 0 — so `POST /exports` would SEAL it. No conflict, no
omission, nothing to read. This is `#42` resurrected in full, reached through the
one input that is not checked.

## Why these tests are here and not in the integration suite

`derive` is a pure function of `FlowInputs`, so the defect is reproducible with
no database — and it has to be, because the shape is not reachable through the
public API today. `screening_decisions.decided_at` is `NOT NULL` (`0005`) and a
conflict cannot exist without `reviewers_required` decisions at its own position,
so no `POST /decisions` sequence produces an untimed position. Nothing in the
schema PREVENTS one either: no constraint ties a `screening_conflicts` row to the
existence of current decisions, so a migration replaying history or a collector
that stops timing one source produces it. A guard whose premise is currently
unmet is cheap; the artifact it prevents is permanent and gets cited.

Mutation-checked rather than assumed: with the sentinel removed and
`settling_position` returning `max(reached)` on partial coverage, the first test
below fails on the flipped bucket. Before the fix it fails; there was no DB-free
test that could see this at all.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from application.services.flow import unattributable_record_ids  # noqa: E402
from domain.policies.flow_derivation import (  # noqa: E402
    UNATTRIBUTABLE,
    FlowInputs,
    StagePosition,
    canonical_counts_hash,
    check_conservation,
    derive,
    settling_position,
)

TA1 = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
TA2 = StagePosition(stage_order=1, stage_key="title-abstract", round_number=2)
FT1 = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
#: A position an ADJUDICATION reached and no decision did, so it carries no time.
#: It also sorts HIGHEST, which is what makes the stage-order fallback pick it.
FT2 = StagePosition(stage_order=2, stage_key="full-text", round_number=2)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _history(record_id: uuid.UUID) -> dict:
    """The measured shape: three timed positions, the newest an EXCLUSION.

    Built once and shared so that the two derivations below differ in exactly
    one input — the untimed adjudication — and nothing else can explain a
    difference in what they answer.
    """
    return {
        "import_row_statuses": ["accepted"],
        "record_ids": {record_id},
        "decisions": {
            (TA1, record_id): [("include", None), ("include", None)],
            (FT1, record_id): [("include", None), ("include", None)],
            # THE NEWEST WORK, and the only one that should settle this record.
            (TA2, record_id): [("exclude", "wrong_population"), ("exclude", "wrong_population")],
        },
        "worked_at": {
            (TA1, record_id): T0,
            (FT1, record_id): T0 + timedelta(hours=1),
            (TA2, record_id): T0 + timedelta(hours=2),
        },
        "reviewers_required": 2,
    }


def test_a_fully_timed_history_settles_at_the_newest_work() -> None:
    """The control. Without it the test below cannot distinguish the sentinel
    firing correctly from the sentinel firing on everything."""
    record_id = uuid.uuid4()
    result = derive(FlowInputs(**_history(record_id)))
    assert result["counts"]["excluded"] == 1, result["counts"]
    assert result["counts"]["not_yet_screened"] == 0, result["counts"]
    assert result["dispositions"][0].settled_at == TA2
    assert result["dispositions"][0].unattributable is False


def test_one_untimed_position_does_not_flip_the_record_to_the_stage_order_answer() -> None:
    """THE DEFECT. Adding an untimed position must not re-decide the timed ones.

    Pre-fix this asserted `included 1 / excluded 0`, which is the fallback
    silently handing a full-text adjudication a record that the newest work
    excluded. The record is now `not_yet_screened` and flagged, which is the only
    honest reading: some of its work is timed and some is not, so no stage can be
    SHOWN to have settled it.

    Note what is deliberately NOT asserted — that the record is `excluded`.
    Guessing the exclusion is still guessing; it happens to be the answer a human
    would reach, and the ledger does not get to reach it from inputs that do not
    contain the ordering.
    """
    record_id = uuid.uuid4()
    inputs = FlowInputs(**_history(record_id), adjudications={(FT2, record_id): ("include", None)})
    result = derive(inputs)
    counts = result["counts"]

    assert counts["included"] == 0, f"the stage-order fallback is back: {counts}"
    assert counts["excluded"] == 0, counts
    assert counts["not_yet_screened"] == 1, counts
    assert counts["exclusion_reasons"] == {}, counts

    disposition = result["dispositions"][0]
    assert disposition.bucket == "not_yet_screened"
    assert disposition.settled_at is None, "an unorderable record must be attributed to no stage"
    assert disposition.unattributable is True


def test_an_unattributable_record_never_reaches_a_stage_tally() -> None:
    """It must not appear in `counts_by_stage` under a stage it may not have
    settled at — that is the PRISMA misattribution `check_stage_conservation`
    exists to refuse, and `settled_at is None` is what keeps it out."""
    record_id = uuid.uuid4()
    result = derive(FlowInputs(**_history(record_id), adjudications={(FT2, record_id): ("include", None)}))
    for stage in result["counts_by_stage"]:
        settled = stage["settled_include"] + stage["settled_exclude"] + stage["settled_uncertain"]
        assert settled == 0, f"stage {stage['stage_key']!r} claims a record nobody can place: {stage}"
        assert stage["exclusion_reasons"] == {}, stage
    # `reached` still counts it: the record really was worked at those stages.
    # Not knowing WHICH one settled it is a different fact from not being there.
    assert {stage["stage_key"]: stage["reached"] for stage in result["counts_by_stage"]} == {
        "title-abstract": 1,
        "full-text": 1,
    }


def test_the_ledger_still_conserves_and_still_answers() -> None:
    """The negative control on the OTHER obvious repair — raising.

    `derive` returns, `check_conservation` passes on its own output, and the
    counts hash is computable. A ledger refuses to SEAL an unorderable history;
    it does not refuse to be READ, or a lead loses the one page that could tell
    them which record is wrong.
    """
    record_id, clean = uuid.uuid4(), uuid.uuid4()
    history = _history(record_id)
    history["import_row_statuses"] = ["accepted", "accepted", "quarantined"]
    history["record_ids"] = {record_id, clean}
    history["decisions"][(TA1, clean)] = [("exclude", "wrong_design"), ("exclude", "wrong_design")]
    history["worked_at"][(TA1, clean)] = T0
    counts = derive(FlowInputs(**history, adjudications={(FT2, record_id): ("include", None)}))["counts"]

    check_conservation(counts)
    assert counts["identified_total"] == 3 and counts["quarantined"] == 1, counts
    assert counts["records_total"] == 2, counts
    assert counts["not_yet_screened"] == 1 and counts["excluded"] == 1, counts
    # The record that CAN be placed is unaffected: the sentinel is per record.
    assert counts["exclusion_reasons"] == {"wrong_design": 1}, counts
    assert isinstance(canonical_counts_hash(counts), str)


def test_the_export_gate_names_the_records_it_cannot_place() -> None:
    """`create_export`'s refusal reads this, and it must not be an empty list.

    The predicate is exercised directly rather than through `POST /exports`,
    which needs a live database — a refusal whose only test needs Postgres is a
    refusal that gets written once and then trusted.

    Ordinary `not_yet_screened` records must NOT be named: they are #1's problem
    and #1's remedy (screen them) works on them, which is the entire reason the
    two refusals are separate.
    """
    unorderable, half_done = uuid.uuid4(), uuid.uuid4()
    history = _history(unorderable)
    history["record_ids"] = {unorderable, half_done}
    history["import_row_statuses"] = ["accepted", "accepted"]
    history["decisions"][(TA1, half_done)] = [("include", None)]  # one of two reviewers
    history["worked_at"][(TA1, half_done)] = T0
    result = derive(FlowInputs(**history, adjudications={(FT2, unorderable): ("include", None)}))

    assert result["counts"]["not_yet_screened"] == 2, result["counts"]
    assert unattributable_record_ids(result["dispositions"]) == [str(unorderable)]


def test_a_history_with_no_times_at_all_still_orders_by_stage() -> None:
    """The arm that is NOT the sentinel, and the one the property suite rests on.

    "No times" and "some times" are different questions. With no times the inputs
    contain exactly one ordering and using it invents nothing; with some times
    they contain two that can disagree. Ten hand-built `FlowInputs` in
    `tests/property/test_flow_conservation.py` set no `worked_at` at all, and
    turning them all `not_yet_screened` would be this fix eating the suite that
    guards it.
    """
    record_id = uuid.uuid4()
    assert settling_position({TA1, FT1, TA2}, {}) == FT1
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={
                (TA1, record_id): [("include", None), ("include", None)],
                (FT1, record_id): [("exclude", "not_retrievable"), ("exclude", "not_retrievable")],
            },
            reviewers_required=2,
        )
    )
    assert result["counts"]["excluded"] == 1, result["counts"]
    assert result["dispositions"][0].settled_at == FT1
    assert result["dispositions"][0].unattributable is False


def test_coverage_is_measured_against_reached_and_not_by_length() -> None:
    """`len(worked_at) == len(reached)` is not "every position is timed".

    A `worked_at` holding one key OUTSIDE `reached` and missing one INSIDE it has
    equal lengths and partial coverage. The length test reads that as total and
    ranks a position it has no time for — the guess the all-or-nothing rule
    exists to refuse, reached through the one input nobody checks. Both callers
    build `worked_at` as a restriction of `reached` today, which is exactly why
    this could sit here unnoticed.
    """
    assert settling_position({TA1, FT1}, {TA1: T0, FT2: T0 + timedelta(hours=5)}) is UNATTRIBUTABLE
    # And the honest total-coverage case still answers, so the guard above is not
    # simply refusing everything.
    assert settling_position({TA1, FT1}, {TA1: T0 + timedelta(hours=5), FT1: T0}) == TA1


def test_two_positions_sharing_a_decided_at_settle_deterministically() -> None:
    """`decided_at` is `server_default=now()` — TRANSACTION start — so work
    written in one transaction carries the IDENTICAL timestamp.

    A tie in time therefore is not exotic, it is the normal case for a batch, and
    it is what `blinded_reads.unblinded_current_decisions` adds `decision_id` to
    its ORDER BY for. At this level the tie breaks on the `StagePosition` itself,
    which is a total order by construction, so the settled bucket and
    `canonical_counts_hash` are the same value however the rows arrived.

    Asserted over an explicit re-ordering of the input dicts rather than trusting
    one run: a hash is only reproducible if two different arrival orders of the
    same immutable history produce it.
    """
    record_id = uuid.uuid4()
    tied = {
        "import_row_statuses": ["accepted"],
        "record_ids": {record_id},
        "decisions": {
            (TA2, record_id): [("include", None), ("include", None)],
            (FT1, record_id): [("exclude", "wrong_design"), ("exclude", "wrong_design")],
        },
        "worked_at": {(TA2, record_id): T0, (FT1, record_id): T0},
        "reviewers_required": 2,
    }
    forward = derive(FlowInputs(**tied))
    reversed_inputs = FlowInputs(
        import_row_statuses=["accepted"],
        record_ids={record_id},
        decisions=dict(reversed(list(tied["decisions"].items()))),
        worked_at=dict(reversed(list(tied["worked_at"].items()))),
        reviewers_required=2,
    )
    backward = derive(reversed_inputs)

    assert forward["dispositions"][0].settled_at == FT1, "the tie must break on the position, not on scan order"
    assert backward["dispositions"][0].settled_at == FT1
    assert canonical_counts_hash(forward["counts"]) == canonical_counts_hash(backward["counts"])
    assert forward["counts"]["excluded"] == 1, forward["counts"]
