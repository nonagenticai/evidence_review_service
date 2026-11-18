"""Flow conservation over randomized histories — `ER-005.1`, `01-…:511`.

The derivation is a pure function, so this needs no database. That is the point
of keeping `derive` free of I/O: the property can be exercised over thousands of
generated histories in under a second, which is the only way a property test
actually finds the case a hand-written fixture misses.

Randomness is seeded, so a failure is reproducible from the printed seed rather
than being a story about a run nobody can repeat.
"""

from __future__ import annotations

import random
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from domain.policies.flow_derivation import (  # noqa: E402
    ConservationError,
    FlowInputs,
    RecordDisposition,
    StageDescriptor,
    StagePosition,
    canonical_counts_hash,
    check_stage_conservation,
    derive,
)

DISPOSITIONS = ("include", "exclude", "uncertain")
REASONS = ("wrong_design", "wrong_population", "not_retrievable")

#: The stages a generated review may have worked at. Two ordinary ones in order,
#: a second round of the first, and — deliberately — a FOURTH sharing
#: `stage_order` 2 with `full-text`, which is the shape a review re-locked
#: against a second protocol version materialises. `StagePosition` has to give a
#: total order over that set or the derivation is scan-order dependent.
POSITIONS = (
    StagePosition(stage_order=1, stage_key="title-abstract", round_number=1),
    StagePosition(stage_order=1, stage_key="title-abstract", round_number=2),
    StagePosition(stage_order=2, stage_key="full-text", round_number=1),
    StagePosition(stage_order=2, stage_key="full-text-v2", round_number=1),
)


def _random_history(rng: random.Random) -> FlowInputs:
    """A plausible review at an arbitrary point in its life.

    Deliberately includes the awkward states: zero records, everything
    quarantined, a merge that removed a record that also has decisions on it,
    adjudications on records with no conflict, reviewers who have half finished,
    records worked at several stages and rounds, records that stopped at the
    first stage, and stages whose `reviewers_required` differs from the
    protocol's.
    """
    n_rows = rng.randint(0, 40)
    statuses = [rng.choice(["accepted", "accepted", "accepted", "quarantined", "rejected"]) for _ in range(n_rows)]
    record_ids = [uuid.uuid4() for s in statuses if s == "accepted"]

    reviewers_required = rng.choice([1, 2, 3])
    # A stage may raise or lower the protocol's requirement, and some stages
    # declare nothing and inherit it.
    by_stage = {
        position.stage_key: rng.choice([1, 2, 3])
        for position in POSITIONS
        if rng.random() < 0.5  # noqa: PLR2004 - a generator weight, not a threshold
    }

    removed: dict[uuid.UUID, uuid.UUID] = {}
    for record_id in record_ids:
        if rng.random() < 0.15:
            removed[record_id] = uuid.uuid4()

    decisions: dict[tuple[StagePosition, uuid.UUID], list[tuple[str, str | None]]] = {}
    for record_id in record_ids:
        # A record stops when it is excluded, so most have work at one position
        # and some at several. Zero is the untouched record.
        for position in rng.sample(POSITIONS, rng.randint(0, len(POSITIONS))):
            votes = rng.randint(0, max(reviewers_required, *by_stage.values(), 3) + 1)
            if not votes:
                continue
            cast: list[tuple[str, str | None]] = []
            for _ in range(votes):
                disposition = rng.choice(DISPOSITIONS)
                reason = rng.choice(REASONS) if disposition == "exclude" else None
                cast.append((disposition, reason))
            decisions[(position, record_id)] = cast

    adjudications: dict[tuple[StagePosition, uuid.UUID], tuple[str, str | None]] = {}
    for record_id in record_ids:
        for position in POSITIONS:
            if rng.random() < 0.1:
                disposition = rng.choice(DISPOSITIONS)
                adjudications[(position, record_id)] = (
                    disposition,
                    rng.choice(REASONS) if disposition == "exclude" else None,
                )

    return FlowInputs(
        import_row_statuses=statuses,
        record_ids=set(record_ids),
        removed_by_merge=removed,
        decisions=decisions,
        adjudications=adjudications,
        reviewers_required=reviewers_required,
        reviewers_required_by_stage=by_stage,
    )


@pytest.mark.parametrize("seed", range(200))
def test_conservation_holds_on_randomized_histories(seed: int) -> None:
    """`derive` either conserves or raises. It never returns wrong counts.

    `derive` calls `check_conservation` itself, so reaching the assertions below
    already means the invariant held. They are restated here because the point
    of the property test is to pin the invariant, not to trust that the
    implementation still checks it.
    """
    rng = random.Random(seed)  # noqa: S311 - a test fixture, not a security decision
    inputs = _random_history(rng)

    try:
        result = derive(inputs)
    except ConservationError as exc:  # pragma: no cover - the failure we are hunting
        pytest.fail(f"seed {seed}: conservation broke — {exc}")

    counts = result["counts"]
    assert counts["identified_total"] == (counts["quarantined"] + counts["rejected"] + counts["records_total"]), (
        f"seed {seed}"
    )
    assert counts["records_total"] == (
        counts["duplicates_removed"] + counts["records_screened"] + counts["not_yet_screened"]
    ), f"seed {seed}"
    assert counts["records_screened"] == (counts["included"] + counts["excluded"] + counts["uncertain"]), f"seed {seed}"


@pytest.mark.parametrize("seed", range(200))
def test_every_record_lands_in_exactly_one_terminal_bucket(seed: int) -> None:
    """The other half of the invariant, which the counts alone cannot show.

    Two records could swap buckets and leave every total unchanged. This asserts
    on identities: each `review_record_id` appears once, and the set of ids in
    the result is exactly the set that went in.
    """
    rng = random.Random(seed)  # noqa: S311
    inputs = _random_history(rng)
    dispositions = derive(inputs)["dispositions"]

    seen = [d.review_record_id for d in dispositions]
    assert len(seen) == len(set(seen)), f"seed {seed}: a record appears in two buckets"
    assert set(seen) == inputs.record_ids, f"seed {seed}: the bucketed set is not the input set"


@pytest.mark.parametrize("seed", range(50))
def test_the_counts_hash_is_stable_under_reordering(seed: int) -> None:
    """`canonical_counts_hash` must not depend on Python dict ordering.

    This is what makes `flow:rebuild`'s comparison meaningful: if the hash moved
    when the same numbers were assembled in a different order, a rebuild would
    report drift that did not exist and nobody would trust the check again.
    """
    rng = random.Random(seed)  # noqa: S311
    counts = derive(_random_history(rng))["counts"]
    shuffled_keys = list(counts)
    rng.shuffle(shuffled_keys)
    reordered = {k: counts[k] for k in shuffled_keys}
    assert canonical_counts_hash(counts) == canonical_counts_hash(reordered)


@pytest.mark.parametrize("seed", range(50))
def test_the_derivation_does_not_depend_on_input_ordering(seed: int) -> None:
    """The claim `flow:rebuild` rests on, tested where it can actually fail.

    `test_the_counts_hash_is_stable_under_reordering` shuffles the OUTPUT dict,
    which only ever exercised the canonicaliser. The defect was upstream: the
    inputs are built by iterating rows out of Postgres in whatever order the scan
    yields, and a record disputed in two stages had its two current adjudications
    collapsed by a `dict` comprehension keyed on the record alone — so which one
    won was decided by physical row order.

    `flow:rebuild` could not have caught that. It compares a hash produced by
    this code against a hash produced by this code, so a derivation that is a
    function of scan order agrees with itself.
    """
    rng = random.Random(seed)  # noqa: S311
    inputs = _random_history(rng)

    def _shuffled(mapping: dict) -> dict:
        items = list(mapping.items())
        rng.shuffle(items)
        return dict(items)

    reordered = FlowInputs(
        import_row_statuses=inputs.import_row_statuses,
        record_ids=inputs.record_ids,
        removed_by_merge=_shuffled(inputs.removed_by_merge),
        decisions=_shuffled(inputs.decisions),
        adjudications=_shuffled(inputs.adjudications),
        reviewers_required=inputs.reviewers_required,
        reviewers_required_by_stage=_shuffled(inputs.reviewers_required_by_stage),
    )
    assert canonical_counts_hash(derive(inputs)["counts"]) == canonical_counts_hash(derive(reordered)["counts"]), (
        f"seed {seed}: the ledger is a function of the order its inputs arrived in"
    )


def test_a_record_is_settled_by_its_latest_stage_not_by_every_stage_at_once() -> None:
    """The headline defect, pinned on the pure function.

    Unanimous `include` at title/abstract, unanimous `exclude` at full text. The
    pre-fix derivation merged the four votes, saw two dispositions, and filed the
    record `not_yet_screened` — with no conflict anywhere to resolve.
    """
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    counts = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={
                (ta, record_id): [("include", None), ("include", None)],
                (ft, record_id): [("exclude", "not_retrievable"), ("exclude", "not_retrievable")],
            },
            reviewers_required=2,
        )
    )["counts"]
    assert counts["not_yet_screened"] == 0, counts
    assert counts["excluded"] == 1 and counts["included"] == 0, counts
    assert counts["exclusion_reasons"] == {"not_retrievable": 1}


def test_a_record_that_stopped_at_the_first_stage_is_settled_there() -> None:
    """The crux. No full-text decision will ever exist for an excluded record."""
    stopped, advanced = uuid.uuid4(), uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    counts = derive(
        FlowInputs(
            import_row_statuses=["accepted", "accepted"],
            record_ids={stopped, advanced},
            decisions={
                (ta, stopped): [("exclude", "wrong_design"), ("exclude", "wrong_design")],
                (ta, advanced): [("include", None), ("include", None)],
                (ft, advanced): [("include", None), ("include", None)],
            },
            reviewers_required=2,
        )
    )["counts"]
    assert counts["not_yet_screened"] == 0, counts
    assert counts["excluded"] == 1 and counts["included"] == 1, counts


def test_a_stage_in_progress_is_still_not_yet_screened() -> None:
    """The negative control: settling from the latest stage must not settle EARLY.

    One reviewer has reported at full text and the other has not. Falling back to
    the completed title/abstract stage would report the record `included` while
    the stage that will actually decide it is half done — a review closing on a
    stage nobody finished. It is `not_yet_screened`, and it is escapable: the
    second reviewer deciding settles it.
    """
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    counts = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={
                (ta, record_id): [("include", None), ("include", None)],
                (ft, record_id): [("exclude", "not_retrievable")],
            },
            reviewers_required=2,
        )
    )["counts"]
    assert counts["not_yet_screened"] == 1, counts
    assert counts["records_screened"] == 0, counts


def test_a_stage_may_require_more_reviewers_than_the_protocol() -> None:
    """`review_stages.reviewers_required`, which the ledger used to ignore.

    Conflict detection has always used the STAGE's number
    (`screening.record_decision` passes `stage.reviewers_required or ...`). The
    ledger used the protocol's, so a full-text stage demanding three reviewers
    had its records called settled — and exportable — on two agreeing.
    """
    record_id = uuid.uuid4()
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    inputs = FlowInputs(
        import_row_statuses=["accepted"],
        record_ids={record_id},
        decisions={(ft, record_id): [("include", None), ("include", None)]},
        reviewers_required=2,
        reviewers_required_by_stage={"full-text": 3},
    )
    assert derive(inputs)["counts"]["not_yet_screened"] == 1

    inputs.decisions[(ft, record_id)].append(("include", None))
    assert derive(inputs)["counts"]["included"] == 1


def test_an_adjudication_settles_the_stage_it_was_made_in() -> None:
    """Two conflicts on one record, one per stage. The LATER one decides it."""
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={
                (ta, record_id): [("include", None), ("exclude", "wrong_design")],
                (ft, record_id): [("include", None), ("exclude", "wrong_population")],
            },
            adjudications={
                (ta, record_id): ("include", None),
                (ft, record_id): ("exclude", "wrong_population"),
            },
            reviewers_required=2,
        )
    )
    assert result["counts"]["excluded"] == 1, result["counts"]
    assert result["counts"]["adjudicated"] == 1, "one record, one settled position, one adjudication"
    assert result["dispositions"][0].settled_at == ft


def test_an_adjudication_alone_still_settles_the_record() -> None:
    """An adjudication defines a position even with no decisions beside it.

    Not reachable from data this service writes — a conflict needs decisions at
    its own `(stage, round, record)` and a correction lands at the same position
    — so this is a claim about `derive` being a total function of the public
    `FlowInputs`, not about the collector. Without it, dropping the adjudication
    half of `_positions_by_record` passes the entire suite while silently
    discarding a lead's ruling and settling the record from the stage below.
    """
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={(ta, record_id): [("include", None), ("include", None)]},
            adjudications={(ft, record_id): ("exclude", "wrong_population")},
            reviewers_required=2,
        )
    )
    assert result["counts"]["excluded"] == 1, result["counts"]
    assert result["dispositions"][0].settled_at == ft
    assert result["dispositions"][0].adjudicated is True


def test_an_unknown_disposition_is_refused_rather_than_dropped() -> None:
    """A record the flow cannot place must not silently vanish from the ledger."""
    record_id = uuid.uuid4()
    position = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    inputs = FlowInputs(
        import_row_statuses=["accepted"],
        record_ids={record_id},
        decisions={(position, record_id): [("maybe_later", None)]},
        reviewers_required=1,
    )
    with pytest.raises(ConservationError, match="terminal bucket"):
        derive(inputs)


def test_an_empty_review_conserves() -> None:
    """Zero of everything is still an arithmetic claim, and it must hold."""
    counts = derive(FlowInputs())["counts"]
    assert counts["identified_total"] == 0
    assert counts["records_total"] == 0
    assert canonical_counts_hash(counts).startswith("sha256:")


# ---------------------------------------------------------------------------
# The per-stage split — PRISMA 2020 reports each stage on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(200))
def test_the_per_stage_tallies_partition_the_flat_ones(seed: int) -> None:
    """`counts_by_stage` is a PARTITION of `counts`, over arbitrary histories.

    `derive` calls `check_stage_conservation` itself, so reaching these
    assertions already means it held — restated here for the same reason
    `test_conservation_holds_on_randomized_histories` restates its own
    invariant: the property test exists to pin the identity, not to trust that
    the implementation still checks it.

    The generated histories deliberately include the shapes that break a naive
    split: a record worked at several stages and rounds, a merge that removed a
    record which also carries decisions, an adjudication with no decisions
    beside it, and two stages sharing a `stage_order`. All four have to land in
    exactly one stage's tally or the sums move.
    """
    rng = random.Random(seed)  # noqa: S311
    result = derive(_random_history(rng))
    counts, by_stage = result["counts"], result["counts_by_stage"]

    for bucket, member in (
        ("included", "settled_include"),
        ("excluded", "settled_exclude"),
        ("uncertain", "settled_uncertain"),
    ):
        assert sum(stage[member] for stage in by_stage) == counts[bucket], f"seed {seed}: {member}"
    assert (
        sum(stage["settled_include"] + stage["settled_exclude"] + stage["settled_uncertain"] for stage in by_stage)
        == counts["records_screened"]
    ), f"seed {seed}"

    merged: dict[str, int] = {}
    for stage in by_stage:
        for code, tally in stage["exclusion_reasons"].items():
            merged[code] = merged.get(code, 0) + tally
    assert merged == counts["exclusion_reasons"], f"seed {seed}"

    for stage in by_stage:
        assert stage["settled_include"] + stage["settled_exclude"] + stage["settled_uncertain"] <= stage["reached"], (
            f"seed {seed}: {stage}"
        )


@pytest.mark.parametrize("seed", range(50))
def test_the_stage_order_of_the_split_is_deterministic(seed: int) -> None:
    """Two stages may share a `stage_order`; the list still has ONE order.

    Same claim `StagePosition` makes and for the same reason: `flow:rebuild`
    compares two derivations of identical history, and a block whose row order
    depended on a scan would make the two disagree about a review nothing
    changed. `full-text` and `full-text-v2` share order 2 in `POSITIONS`
    precisely so this is a real question.
    """
    rng = random.Random(seed)  # noqa: S311
    inputs = _random_history(rng)
    first = [(s["stage_order"], s["stage_key"]) for s in derive(inputs)["counts_by_stage"]]
    assert first == sorted(first), f"seed {seed}: not ordered by (stage_order, stage_key): {first}"
    assert first == [(s["stage_order"], s["stage_key"]) for s in derive(inputs)["counts_by_stage"]], f"seed {seed}"


def test_a_declared_stage_nobody_worked_is_a_row_of_zeros_not_an_absence() -> None:
    """An opened stage with no decisions yet is a real state and must be visible.

    Dropping it would make a two-stage review look single-stage until the first
    full-text decision landed, which is the same class of error as the merge:
    the diagram silently loses a box.
    """
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={(ta, record_id): [("exclude", "wrong_design"), ("exclude", "wrong_design")]},
            stages=(
                StageDescriptor(stage_order=1, stage_key="title-abstract", name="Title/abstract"),
                StageDescriptor(stage_order=2, stage_key="full-text", name="Full text"),
            ),
            reviewers_required=2,
        )
    )
    assert [s["stage_key"] for s in result["counts_by_stage"]] == ["title-abstract", "full-text"]
    full_text = result["counts_by_stage"][1]
    assert full_text == {
        "stage_key": "full-text",
        "stage_order": 2,
        "name": "Full text",
        "reached": 0,
        "settled_include": 0,
        "settled_exclude": 0,
        "settled_uncertain": 0,
        "exclusion_reasons": {},
    }, full_text


def test_a_stage_the_declaration_omits_is_synthesized_rather_than_dropped() -> None:
    """`derive` is a total function of `FlowInputs`, including an incomplete one.

    The property suite hands over histories with no `stages` at all, and a
    caller replaying history could hand over a partial list. Dropping the
    unlisted stage's records would silently break the partition — the flat
    `excluded` would exceed the per-stage sum — so the stage is synthesized from
    the position itself and named after its key.
    """
    record_id = uuid.uuid4()
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted"],
            record_ids={record_id},
            decisions={(ft, record_id): [("exclude", "not_retrievable"), ("exclude", "not_retrievable")]},
            stages=(StageDescriptor(stage_order=1, stage_key="title-abstract", name="Title/abstract"),),
            reviewers_required=2,
        )
    )
    synthesized = {s["stage_key"]: s for s in result["counts_by_stage"]}["full-text"]
    assert synthesized["name"] == "full-text", "an undeclared stage is labelled by its key, never by a guess"
    assert synthesized["settled_exclude"] == 1
    assert synthesized["exclusion_reasons"] == {"not_retrievable": 1}
    assert result["counts"]["excluded"] == 1


def test_a_merged_away_record_is_in_no_stage_population() -> None:
    """A duplicate never entered screening as itself, decisions or no decisions.

    `create_assignments` assigns every corpus member, so a record can carry
    decisions and still be merged away afterwards. Counting it under `reached`
    would put a number in PRISMA's eligibility box that the flat ledger — which
    files it `duplicate_removed` — does not conserve.
    """
    survivor, removed = uuid.uuid4(), uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    result = derive(
        FlowInputs(
            import_row_statuses=["accepted", "accepted"],
            record_ids={survivor, removed},
            removed_by_merge={removed: uuid.uuid4()},
            decisions={
                (ta, survivor): [("include", None), ("include", None)],
                (ta, removed): [("include", None), ("include", None)],
            },
            reviewers_required=2,
        )
    )
    assert result["counts"]["duplicates_removed"] == 1
    only = result["counts_by_stage"][0]
    assert only["reached"] == 1, f"the merged-away record still has decisions, but it is not a report: {only}"
    assert only["settled_include"] == 1


def test_the_stage_partition_check_is_not_decorative() -> None:
    """The tally half of the guard fires. Necessary, and NOT sufficient — see
    `test_a_swapped_stage_attribution_is_refused` below for the half that
    catches a misattribution, which every one of these sums survives.
    """
    counts = {"included": 1, "excluded": 2, "uncertain": 0, "exclusion_reasons": {"wrong_design": 2}}
    honest = [
        {
            "stage_key": "ta",
            "reached": 3,
            "settled_include": 1,
            "settled_exclude": 2,
            "settled_uncertain": 0,
            "exclusion_reasons": {"wrong_design": 2},
        }
    ]
    check_stage_conservation(counts, honest, {}, [])

    with pytest.raises(ConservationError, match="does not partition the ledger"):
        check_stage_conservation(counts, [{**honest[0], "settled_exclude": 1}], {}, [])
    with pytest.raises(ConservationError, match="do not partition"):
        check_stage_conservation(counts, [{**honest[0], "exclusion_reasons": {"wrong_population": 2}}], {}, [])
    with pytest.raises(ConservationError, match="ever reached it"):
        check_stage_conservation(counts, [{**honest[0], "reached": 2}], {}, [])


def test_a_swapped_stage_attribution_is_refused() -> None:
    """THE CHECK A SUM CANNOT MAKE — a record settled at a stage it never reached.

    This is the hole the tally identities left. Swap which stage two records
    settled at and every sum above is unchanged: the same records, the same
    buckets, the same reasons, merely filed under the wrong stage. The flat
    `counts` do not move, `canonical_counts_hash` does not move, and `GET /flow`
    reports the title/abstract reason as a full-text exclusion — the exact
    PRISMA lie `counts_by_stage` was added to end.

    The record here reached only title/abstract, so claiming it settled at full
    text is a claim about work that does not exist, and that is checkable
    against `_positions_by_record` without recomputing anything.
    """
    record_id = uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)

    honest = RecordDisposition(record_id, "excluded", "wrong_population", settled_at=ta)
    counts = {"included": 0, "excluded": 1, "uncertain": 0, "exclusion_reasons": {"wrong_population": 1}}
    by_stage = [
        {
            "stage_key": "title-abstract",
            "reached": 1,
            "settled_include": 0,
            "settled_exclude": 1,
            "settled_uncertain": 0,
            "exclusion_reasons": {"wrong_population": 1},
        }
    ]
    check_stage_conservation(counts, by_stage, {record_id: {ta}}, [honest])

    swapped = RecordDisposition(record_id, "excluded", "wrong_population", settled_at=ft)
    with pytest.raises(ConservationError, match="which it never reached"):
        check_stage_conservation(counts, by_stage, {record_id: {ta}}, [swapped])


def test_the_attribution_check_reads_the_record_and_not_the_stage_list() -> None:
    """A record settled at a stage OTHER records reached is still refused.

    The cheap wrong version of this guard checks the stage against the review's
    stages, or against the union of every position anybody worked. Both accept
    the swap that matters: on an ordinary two-stage review every stage has been
    reached by somebody, so a union test is satisfied by construction and the
    guard is decorative again. The set has to be the RECORD's own.
    """
    stopped_early, went_on = uuid.uuid4(), uuid.uuid4()
    ta = StagePosition(stage_order=1, stage_key="title-abstract", round_number=1)
    ft = StagePosition(stage_order=2, stage_key="full-text", round_number=1)
    positions = {stopped_early: {ta}, went_on: {ta, ft}}

    counts = {"included": 1, "excluded": 1, "uncertain": 0, "exclusion_reasons": {"wrong_population": 1}}
    by_stage = [
        {
            "stage_key": "title-abstract",
            "reached": 2,
            "settled_include": 0,
            "settled_exclude": 1,
            "settled_uncertain": 0,
            "exclusion_reasons": {"wrong_population": 1},
        },
        {
            "stage_key": "full-text",
            "reached": 1,
            "settled_include": 1,
            "settled_exclude": 0,
            "settled_uncertain": 0,
            "exclusion_reasons": {},
        },
    ]
    honest = [
        RecordDisposition(stopped_early, "excluded", "wrong_population", settled_at=ta),
        RecordDisposition(went_on, "included", None, settled_at=ft),
    ]
    check_stage_conservation(counts, by_stage, positions, honest)

    # `full-text` IS a stage of this review and WAS reached — by the other
    # record. A union-shaped guard passes this; the per-record one does not.
    lying = [
        RecordDisposition(stopped_early, "excluded", "wrong_population", settled_at=ft),
        RecordDisposition(went_on, "included", None, settled_at=ft),
    ]
    with pytest.raises(ConservationError, match="which it never reached"):
        check_stage_conservation(counts, by_stage, positions, lying)
