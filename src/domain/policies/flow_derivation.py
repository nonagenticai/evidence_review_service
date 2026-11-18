"""The flow ledger — derived from immutable history, never stored as truth.

`ER-005.2` / `01-…:510-511`. `flow_projections` is a **cache**;
`POST /api/reviews/{id}/flow:rebuild` recomputes from zero and must return an
identical `canonical_counts_hash`. Everything here is a pure function of five
immutable inputs:

    import_rows · corpus_version_members · duplicate_resolutions (chain tails)
    screening_decisions (chain tails) · screening_adjudications (chain tails)

## The conservation invariant

```
identified_total == quarantined + records_total
records_total    == duplicates_removed + records_screened + not_yet_screened
records_screened == included + excluded + uncertain
```

and every `review_record_id` lands in exactly one terminal bucket. This is the
property `01-…:511` asks to be randomized-tested, and it is the reason the flow
is derived rather than incremented: a counter can drift, a projection over
immutable history cannot.

Note the split at the top. `identified_total` counts **import rows**, because a
quarantined row never became a record — it has no `review_record_id` to put in a
bucket. Conflating the two levels is how a PRISMA diagram ends up unable to
explain where 40 of its 411 records went.

## What "screened" means

A review has STAGES — title/abstract, then full text — and a record is judged
separately in each one. So the question is never "what did the reviewers say
about this record", it is **"what did they say about it at the last stage it
reached"**. That stage is the record's :class:`StagePosition`, and the rule is:

* find the record's HIGHEST :class:`StagePosition` at which any current decision
  or adjudication exists — its *settling position*;
* at that position, an adjudication wins if one exists; otherwise the stage's
  `reviewers_required` current decisions must exist and all agree.

Anything else — no position at all, fewer decisions than required at the
settling position, or an unresolved disagreement there — is `not_yet_screened`.
An unresolved conflict counted as screened would let a review close with its
disagreements still open, which `01-…:125` forbids.

### Why the position and not the whole record

This used to merge every stage's votes into one list per record and demand
unanimity over that. Two stages, two reviewers, unanimous INCLUDE at
title/abstract and unanimous EXCLUDE at full text is four rows and two distinct
dispositions, so the record was filed `not_yet_screened` — while conflict
detection, which IS stage keyed, correctly opened nothing. `POST /exports`
refused forever with *"1 records are not yet screened"*, `GET /conflicts`
returned an empty list, and `:resolve` needs a `conflict_id` that did not exist.
The only escape was for both reviewers to supersede their STAGE-ONE decisions,
which retroactively falsifies the title/abstract stage. "Excluded at full text
after passing title/abstract" is what a two-stage review is FOR; a ledger that
cannot express it cannot describe a systematic review.

### Why each stage is also counted on its own

`counts` is one tally over the whole review, and PRISMA 2020 does not accept one
tally. Records excluded at title/abstract and reports excluded at full-text
eligibility are separate boxes, the second required to be "with reasons", and a
merged number is a recognised reporting failure. `counts_by_stage` is that split
— one entry per stage, ordered by `stage_order`, carrying the stage's own
buckets and its own reason breakdown plus `reached`, the population PRISMA calls
"reports assessed for eligibility".

It sits BESIDE `counts` and never inside it. `counts` is sealed into every
export against a contract that refuses a thirteenth member, and its canonical
hash is what every `flow_projection` row compares; widening it would stale the
lot. See :func:`_counts_by_stage`.

### What this still does NOT give a PRISMA author

Stated so that nobody reads `counts_by_stage` as "PRISMA is done". The box it
genuinely delivers is **"reports excluded, with reasons"**, per stage, which was
the unfillable one. These remain unfillable from anything this service stores:

* **identification split by source** — PRISMA 2020 separates records identified
  from DATABASES from those identified from REGISTERS, and again from citation
  searching, websites and organisations. `import_rows` carries no such
  provenance, so `identified_total` is one undifferentiated number.
* **"reports sought for retrieval" and "reports not retrieved"** — retrieval is
  a step between screening and eligibility that this service does not model at
  all. `not_retrievable` is an EXCLUSION REASON recorded at full text, which is
  a different claim: it says a reviewer ruled on the record, not that retrieval
  was attempted and failed. A diagram needing those two boxes needs a retrieval
  step first.
* **studies vs reports** — PRISMA counts REPORTS through the flow and STUDIES at
  the end, because several reports can describe one study. This service has one
  level, `review_record`, and `duplicate_resolutions` merges duplicate REPORTS,
  not multi-report studies. "Studies included in review" is therefore reported
  here as a report count.

### Why the record's highest position and not the review's

A record EXCLUDED at title/abstract never advances — it is never retrieved, so a
full-text decision on it will never exist. Waiting for one is the same deadlock
with a different name, and just as unresolvable: no conflict to adjudicate and no
honest decision to record. The settling position is therefore a property of the
RECORD's own history, not of how far the review as a whole has got.

`create_assignments` now honours exactly that: at stage order N > 1 it assigns
only records that settled `included` at stage N-1
(`records_settled_included_at`, below), so a record excluded at title/abstract
is never offered a full-text queue slot and will never acquire the decision
this section is about. That is a narrower guarantee than "a decision defines a
position", not a replacement for it — an assignment is still not evidence that
a record advanced, only that a reviewer was HANDED it; a decision is, which is
why only decisions and adjudications define a position.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

#: The terminal buckets a record can be in. Exactly one each.
TERMINAL_BUCKETS = ("duplicate_removed", "included", "excluded", "uncertain", "not_yet_screened")

#: A DISPOSITION is what a reviewer records (`include`); a BUCKET is where the
#: record lands in the flow (`included`). They are one letter apart, which is
#: exactly why they were silently conflated once: every screened record was
#: filed under `include`, every `buckets.get("included")` returned 0, and the
#: conservation check caught it — `records_total 4 != 0 + 0 + 0`. That is the
#: check earning its place; without it the ledger would have reported a review
#: with four records and no outcomes as if that were a fact.
DISPOSITION_TO_BUCKET = {"include": "included", "exclude": "excluded", "uncertain": "uncertain"}


def bucket_for(disposition: str) -> str:
    """Map a recorded disposition onto its terminal bucket.

    Raises on an unknown value rather than defaulting: a disposition the flow
    cannot place is a record that would vanish from the ledger, and the
    conservation check would then report a failure whose cause is three
    functions away.
    """
    try:
        return DISPOSITION_TO_BUCKET[disposition]
    except KeyError as exc:
        raise ConservationError(f"disposition {disposition!r} has no terminal bucket") from exc


def agreement_key(disposition: str, exclusion_reason_code: str | None) -> tuple[str, str | None]:
    """WHAT TWO REVIEWERS HAVE TO MATCH ON TO BE CALLED IN AGREEMENT.

    One function, called by every derivation that asks the question, because
    there are THREE of them and they must not be able to drift:

    * `screening._maybe_open_conflict` — decides whether a conflict OPENS;
    * `derive`, below — decides whether the record is SCREENED;
    * `flow._unresolved_conflicts` — decides whether an open conflict has been
      settled by the reviewers agreeing since, which is what the export gate,
      `:close` and the lead's `/conflicts` panel all read.

    Any two of those disagreeing produces a state the ledger cannot describe: a
    conflict open on a record the ledger calls screened, or a record stuck
    `not_yet_screened` with no `conflict_id` for a lead to resolve, or — the
    worst of the three — a review that CLOSES over a disagreement nobody ruled
    on, which `01-…:125` forbids and `derive`'s own docstring names as the thing
    this code must never do.

    ## Why the reason is part of it

    It was not, and the loss was silent. Two reviewers both `exclude` the same
    record, one `wrong_design` and one `wrong_population`: no conflict, and the
    ledger reported `exclusion_reasons {"wrong_design": 1}` — the EARLIER
    reviewer's answer, with the later one stored, never contradicted and absent
    from the sealed manifest. Measured end to end: 0 conflicts, `POST /exports`
    201.

    The two alternatives were considered and are worse:

    * keep the earliest and document it — this asks a PRISMA reader to accept a
      per-reason breakdown from which one reviewer's stated reason was deleted
      with no trace. `inclusion-set-export.v2`'s own `$comment` on `excluded`
      says "broken down by reason code below", and "broken down by" asserts a
      partition of a population, not a sample of one;
    * count the record under every reason given — this breaks
      `sum(exclusion_reasons.values()) == excluded`. That identity is stated as
      PROSE in `$defs/flow_counts.$comment` and deliberately not declared as a
      cross-field rule, so this option breaks no validator while breaking the
      meaning, which makes it the worst of the three: nothing would fail.

    Treating it as a disagreement leaves the manifest's meaning exactly as
    published — `exclusion_reasons` still partitions `excluded`, the sum
    identity still holds — and every entry in it is now one that reviewers or a
    lead actually agreed on. **No contract version bump**, because nothing the
    contract says changes; what changes is which states can produce a seal.

    ## Why only on `exclude`

    `DecisionBody` accepts `exclusion_reason_code` beside ANY disposition and
    `record_decision` only validates it against the vocabulary when the
    disposition is `exclude`, so a stray reason code can be stored on an
    `include`. It reaches no output: `exclusion_reasons` counts only records in
    the `excluded` bucket. Keying on it there would open a conflict over a
    difference the manifest cannot express and an adjudication cannot name —
    a refusal a lead has no way to act on. So the reason is load-bearing exactly
    where it is published, and nowhere else.
    """
    return (disposition, exclusion_reason_code if disposition == "exclude" else None)


@dataclass(frozen=True, order=True)
class StagePosition:
    """WHERE in a review a piece of screening work was done. Ordered, so that
    ``max()`` over a record's positions means "the last stage it reached".

    ## The three fields, and why each is in the sort key

    ``stage_order`` is the authority — it is `review_stages.stage_order`,
    materialised at protocol lock from the position of the stage in the locked
    protocol's `stage_definitions`, and it is the only statement anywhere about
    which stage comes after which.

    ``stage_key`` breaks a tie in ``stage_order``, which the schema permits: the
    order is `enumerate(stage_definitions, start=1)` and is unique WITHIN one
    protocol version, but `review_stages` is only unique on
    `(review_id, stage_key)`, so a review re-locked against a second protocol
    version materialises a second set of stages numbered from 1 again. Ordering
    on `stage_order` alone would then be a partial order, and a partial order
    resolved by scan position is exactly the non-determinism this class exists to
    remove. `stage_key` is unique per review by constraint, so
    `(stage_order, stage_key)` is a TOTAL order over a review's stages —
    identical on every read of the same history, which is the property
    `canonical_counts_hash` is comparing.

    Be clear about what that tie-break is and is not. Alphabetical order of a
    stage key carries NO meaning: in a tie it is arbitrary, and it is chosen
    because determinism is the property that is load-bearing here and
    arbitrariness is not the same as instability. The principled ordering would
    be the locked protocol version's `version_number` — a stage materialised by a
    later protocol really is later work — and it is deliberately not used,
    because it costs a join on every derivation to order a shape no review in
    this service has yet reached. If two protocol versions ever get screened
    against in one review, that is the change to make, and this paragraph is the
    reason it is a change rather than a fix.

    ``round_number`` orders re-screens WITHIN a stage, and sorts after
    `stage_key` rather than before it because a round is a subdivision of a
    stage.

    That used to be justified here as "stage 2 round 1 is later work than stage 1
    round 4". It is not, and believing it discarded real work: a record screened
    at both stages and then re-screened at stage 1, where both reviewers
    EXCLUDED it, kept its inclusion and sealed. A stage order cannot say which
    piece of work came later once rounds exist, because a round happens whenever
    the lead calls for one.

    So this ordering is no longer what settles a record — see
    :func:`settling_position`, which uses `decided_at` and falls back to this
    order only when it has no times to compare. This class stays a TOTAL order
    because it is still the deterministic tie-break, and `canonical_counts_hash`
    depends on there being one.
    """

    stage_order: int
    stage_key: str
    round_number: int


class Unattributable(Enum):
    """The type of :data:`UNATTRIBUTABLE`, and the only thing that inhabits it.

    An `Enum` rather than a bare ``object()`` so that a checker narrows
    ``position is UNATTRIBUTABLE`` against the union :func:`settling_position`
    returns. A plain singleton is the same value at runtime and an opaque
    `object` to `ty`, which would push every call site into an `isinstance` it
    does not otherwise need — and a call site that forgets the check is exactly
    the failure this sentinel exists to make impossible to forget.
    """

    TOKEN = "unattributable"


#: NO POSITION SETTLES THIS RECORD, AND THE LEDGER REFUSES TO INVENT ONE.
#:
#: :func:`settling_position`'s answer for a PARTIALLY timed history — some of the
#: positions a record reached carry a `decided_at` and some do not. It is not an
#: error value and it is not a bucket: `derive` files such a record
#: `not_yet_screened` with no `settled_at`, so the ledger still conserves and
#: still serves, and `flow.create_export` refuses to seal while one exists and
#: names it. See :meth:`FlowInputs.settling_position` for why this is neither a
#: fallback nor a raise.
UNATTRIBUTABLE = Unattributable.TOKEN


def settling_position(
    reached: set[StagePosition], worked_at: dict[StagePosition, datetime]
) -> StagePosition | Unattributable:
    """The newest work among `reached`; its highest position when NOTHING is
    timed; :data:`UNATTRIBUTABLE` when only SOME of it is.

    ONE spelling, called by the ledger and by `flow._decided_versions`, because
    the two answer the same question — "which position settles this record?" —
    and a second spelling is how they came to disagree about `reviewers_required`
    in the first place.

    See :meth:`FlowInputs.settling_position` for why time and not stage order,
    and for why the middle arm is a sentinel rather than a fallback or a raise.

    ## Coverage is measured against `reached`, not against `len(worked_at)`

    This was `if worked_at and len(worked_at) == len(reached)`. Both callers
    build `worked_at` as a restriction of `reached`, so the two spellings agree
    for them — but a `worked_at` carrying one key OUTSIDE `reached` and missing
    one INSIDE it has equal lengths and partial coverage, and the length test
    reads that as total. It would then rank a position it has no time for as
    though it had one, which is the guess the all-or-nothing rule exists to
    refuse, reached through the one input nobody checks. Intersecting first
    costs one pass over a set that is never larger than a review's stage count.
    """
    timed = {position: worked_at[position] for position in reached if position in worked_at}
    if not timed:
        # NOTHING is timed, so there is no second ordering to contradict the
        # stage order and the stage order is the only order these inputs
        # contain. Deliberately still an answer and not the sentinel: see the
        # method below for why "no times" and "some times" are different
        # questions.
        return max(reached)
    if len(timed) < len(reached):
        return UNATTRIBUTABLE
    return max(((when, position) for position, when in timed.items()))[1]


@dataclass(frozen=True, order=True)
class StageDescriptor:
    """One of the review's stages, as `review_stages` declares it.

    `StagePosition` carries the two fields the ORDER turns on and deliberately
    not the third: a `name` in the sort key would let a rename change which
    stage settles a record. But a PRISMA diagram is LABELLED, and `full-text` is
    a key, not a label — so the name has to reach the reader somehow, and this
    is the object that carries it.

    Sorted on `(stage_order, stage_key, name)`, which is the same total order
    `StagePosition` uses for its first two fields; `stage_key` is unique per
    review by constraint, so the third component is never reached and the two
    orderings cannot disagree.
    """

    stage_order: int
    stage_key: str
    name: str


@dataclass
class RecordDisposition:
    """One record's settled position, and how it got there."""

    review_record_id: UUID
    bucket: str
    exclusion_reason_code: str | None = None
    #: True when an adjudication settled it rather than reviewer agreement.
    adjudicated: bool = False
    #: The stage the bucket was settled AT, or `None` for a record that is
    #: `duplicate_removed` or has no screening work at all.
    #:
    #: This used to say "not counted and not exported — the flow is an
    #: aggregate". It was populated at both call sites below, and
    #: `grep -rn settled_at src/` then found NO consumer anywhere: the field was
    #: written and read by nothing but two assertions in the property suite.
    #:
    #: That is what made the flow un-PRISMA-able. `exclusion_reasons` counted
    #: every `excluded` record with no filter on this field, so a record dropped
    #: from its ABSTRACT and a report dropped at FULL TEXT landed in one
    #: undifferentiated tally — and PRISMA 2020 reports those as two separate
    #: numbers, the second "with reasons". The `omissions` array, the one place
    #: a consumer could have rebuilt the split themselves, dropped it too.
    #:
    #: It is now the key of `counts_by_stage` and a member of every omission.
    settled_at: StagePosition | None = None
    #: True when this record is `not_yet_screened` because its history CANNOT BE
    #: ORDERED — :data:`UNATTRIBUTABLE`, some positions timed and some not — as
    #: opposed to the ordinary reasons (nobody has looked at it, a stage is half
    #: done, reviewers disagree with no adjudication yet).
    #:
    #: A separate flag and not a sixth bucket, deliberately. `TERMINAL_BUCKETS`
    #: is the partition `check_conservation` is arithmetic over and
    #: `inclusion-set-export.v2` seals a `bucket` string per omission; a new
    #: member would move `canonical_counts_hash` for every review at once and
    #: stale every `flow_projections` row — a fleet-wide `projection_stale` false
    #: alarm raised by a guard. The record IS not-yet-screened: no stage can be
    #: shown to have settled it. This says WHY, to the one caller whose remedy
    #: differs.
    #:
    #: And the remedy really is different, which is the whole reason the flag
    #: exists. "N records are not yet screened" tells a lead to go screen them,
    #: and screening an unattributable record adds another position without
    #: removing the untimed one — so coverage stays partial and the advice loops
    #: forever. `flow.create_export` reads this and says something a lead can act
    #: on instead.
    #:
    #: Never sealed. `_omission_row` writes `bucket` and `settled_at_*` and not
    #: this, so the exported cargo is byte-identical to what an ordinary
    #: `not_yet_screened` record produces — and an export carrying one cannot be
    #: minted anyway, because the gate refuses first.
    unattributable: bool = False


@dataclass
class FlowInputs:
    """Everything the derivation reads. Nothing else is consulted."""

    import_row_statuses: list[str] = field(default_factory=list)
    record_ids: set[UUID] = field(default_factory=set)
    #: `review_record_id` -> the merge that removed it (survivors excluded).
    removed_by_merge: dict[UUID, UUID] = field(default_factory=dict)
    #: `(position, review_record_id)` -> current dispositions, one per reviewer,
    #: **at that stage and round**. Keyed by position and not by record alone:
    #: see the module docstring's "Why the position and not the whole record".
    decisions: dict[tuple[StagePosition, UUID], list[tuple[str, str | None]]] = field(default_factory=dict)
    #: `(position, review_record_id)` -> adjudicated `(disposition, reason)`.
    #: A conflict is keyed `(stage_id, round_number, review_record_id)` in the
    #: database, so a record disputed in two stages has TWO current
    #: adjudications; keying them by record alone collapsed them to whichever
    #: one an unordered scan yielded last.
    adjudications: dict[tuple[StagePosition, UUID], tuple[str, str | None]] = field(default_factory=dict)
    #: THE REVIEW'S STAGES, in the order `review_stages` declares them.
    #:
    #: Only ever read to LABEL and to ORDER `counts_by_stage`, never to decide
    #: where a record settles — that is `decisions` and `adjudications` and
    #: nothing else, exactly as `_positions_by_record` says. So a stage listed
    #: here that nobody worked appears as a row of zeros, which is the honest
    #: answer for a review whose second stage has opened and screened nothing,
    #: and a stage NOT listed here that decisions do reach is synthesized from
    #: those decisions rather than dropped: a tally that silently omits a stage
    #: is the defect `counts_by_stage` exists to end, one level down.
    #:
    #: Defaulted empty so that `derive` stays a total function of a hand-built
    #: `FlowInputs` — the property suite constructs ten of them — and so that
    #: the per-stage conservation identity holds unconditionally rather than
    #: only for inputs the collector built.
    stages: tuple[StageDescriptor, ...] = ()
    #: The protocol's requirement — the fallback for a stage that declares none.
    reviewers_required: int = 2
    #: `stage_key` -> that stage's `reviewers_required`. A stage may raise or
    #: lower the protocol's number (`review_stages.reviewers_required`), and
    #: `screening.record_decision` already passes the STAGE's value to conflict
    #: detection. The ledger read the protocol's, so a stage requiring three
    #: reviewers had records called settled by two agreeing — screened, in the
    #: ledger, by a stage that had not finished with them.
    reviewers_required_by_stage: dict[str, int] = field(default_factory=dict)

    #: `(position, review_record_id)` -> when the most recent current DECISION at
    #: that position was recorded, from `screening_decisions.decided_at`:
    #: persisted and immutable, never a clock read at derivation time, so the
    #: derivation stays a pure function of stored history.
    #:
    #: Decisions only. An adjudication contributes a POSITION but not a time —
    #: it settles what an earlier stage should have concluded and does not
    #: overturn a stage screened since. See `flow._collect`.
    worked_at: dict[tuple[StagePosition, UUID], datetime] = field(default_factory=dict)

    def reviewers_required_at(self, position: StagePosition) -> int:
        return self.reviewers_required_by_stage.get(position.stage_key, self.reviewers_required)

    def settling_position(self, record_id: UUID, reached: set[StagePosition]) -> StagePosition | Unattributable:
        """WHERE this record is settled: the newest work on it.

        NOT `max(reached)`. `StagePosition` sorts `(stage_order, stage_key,
        round_number)` with the round LAST, justified as "stage 2 round 1 is
        later work than stage 1 round 4". Measured, that is false: screen a
        record at title/abstract and full text, then re-screen title/abstract in
        round 2 where both reviewers EXCLUDE it, and the exclusion — the newest
        work the review contains — was discarded. No conflict, no
        `not_yet_screened`, no omission entry. The record sealed as included into
        an append-only artifact with an empty `omissions` array, and `:verify`
        answered verified.

        A stage order cannot answer "which came later" once rounds exist, because
        a round is a re-screen and a re-screen happens whenever the lead calls for
        one. `decided_at` can, and it is already stored.

        Ties break on the `StagePosition` itself, so two pieces of work recorded
        in the same transaction still order deterministically and
        `canonical_counts_hash` stays reproducible.

        ## The all-or-nothing rule, and the THIRD answer it needs

        Time is used only when EVERY position reached by this record carries a
        timestamp. A partially timed set cannot be compared — an untimed position
        would have to be ranked above or below every timed one, and either choice
        is a guess about ordering, which is the thing this method exists to stop
        making.

        There are therefore THREE answers, not two, and the middle one is
        :data:`UNATTRIBUTABLE`:

        * **every position timed** — the newest `decided_at` wins;
        * **some timed, some not** — no answer. The sentinel;
        * **none timed** — `max(reached)`, the pre-`#42` stage ordering.

        The first and third are not the same rule applied twice; they are two
        different questions. With NO times the inputs contain exactly one
        ordering, the stage order, and using it invents nothing. With SOME times
        the inputs contain TWO orderings that can disagree, and picking one is
        the invention. The untimed arm exists for `FlowInputs` built by hand —
        the property suite constructs ten of them — and is why the sentinel is
        keyed on *partial* coverage and not on *incomplete* coverage.

        ## Why a sentinel, and not either of the two obvious repairs

        This used to fall through to `max(reached)` on partial coverage, in one
        expression shared with the untimed arm. Both alternatives to a sentinel
        were tried against the shape below and are worse:

        * **keep the fallback** — this is the defect. Exhibited: a record with
          decisions at title/abstract round 1 and full text, RE-SCREENED at
          title/abstract round 2 where both reviewers excluded it (the newest
          work the review contains, and timed), plus an adjudication at a
          full-text round 2 that carries no decisions and is therefore untimed.
          Coverage goes partial, the whole record silently reverts to stage
          order, `max(reached)` is the untimed adjudication, and the exclusion is
          discarded — `#42` resurrected in full, with no conflict, no
          `not_yet_screened` and no omission entry to show for it. The fallback
          does not degrade to a worse answer for the untimed position; it throws
          away the timed ordering for the whole record.
        * **raise `ConservationError`** — this is the reflex, and it takes the
          wrong thing down. The counts still conserve: one record moves into
          `not_yet_screened` and every identity holds. Raising instead makes
          `GET /flow` a 500 for the entire review — every other record's tally
          included — so the lead loses the one page that could tell them WHICH
          record is wrong. A ledger refuses to seal; it does not refuse to be
          read.

        The sentinel is the third thing: the ledger reports the record as
        `not_yet_screened` with no `settled_at`, which is exactly true (no stage
        can be shown to have settled it), it enters no `counts_by_stage` tally,
        `check_conservation` and `check_stage_conservation` both pass unweakened,
        and `flow.create_export` refuses the seal and NAMES the record. The
        review stays readable and the wrong artifact stays unwritten.

        ## Reachability, and why the code carries the guard anyway

        `flow._collect` populates `worked_at` from the same rows it builds
        `decisions` from, and `screening_decisions.decided_at` is `NOT NULL`
        (`0005`), so every position a DECISION reaches is timed;
        `test_the_collector_times_every_position_it_reports` pins that. The only
        untimed position the collector can produce is one reached by an
        ADJUDICATION alone, and a conflict needs `reviewers_required` disagreeing
        decisions at its own `(stage, round, record)` before it can exist — so
        for data this service wrote, partial coverage is unreachable.

        Unreachable is not prevented. Nothing in the schema ties a
        `screening_conflicts` row to the existence of current decisions at its
        position: no constraint, no trigger, no check. A migration replaying
        history, a repair script, or a future collector that stops timing one
        source produces the shape without violating anything the database
        enforces, and the failure it produced was SILENT — a sealed export, not
        an error. A guard whose premise is currently unmet is cheap; the
        artifact it prevents is permanent and gets cited.
        """
        return settling_position(
            reached,
            {
                position: self.worked_at[(position, record_id)]
                for position in reached
                if (position, record_id) in self.worked_at
            },
        )


class ConservationError(AssertionError):
    """The derivation produced counts that do not add up.

    Raised rather than logged. A flow ledger that does not conserve is not a
    degraded ledger, it is a wrong one, and returning it would put a number on a
    PRISMA diagram that no history supports.
    """


def _positions_by_record(inputs: FlowInputs) -> dict[UUID, set[StagePosition]]:
    """Every stage a record has actually been WORKED at.

    Built from decisions and adjudications and from nothing else. An assignment
    is deliberately not evidence, and that stays true even though
    `create_assignments` no longer opens a stage over the whole corpus: at stage
    order N > 1 it now assigns only records that settled `included` at stage
    N-1 (`records_settled_included_at`, below), but an assignment still is not a
    decision — a reviewer can be handed a record and never act on it, and a
    round can be re-called before anyone does. Reading assignments here would
    file every merely-assigned record `not_yet_screened` on the strength of
    being offered the work rather than having done it — a different deadlock
    from the one this used to describe (ERS #42, a full-text stage opening over
    records excluded at title/abstract that could never be retrieved), but a
    deadlock all the same. `worked_at`/`decisions`/`adjudications` are the only
    honest inputs; see the module docstring's "Why the record's highest
    position and not the review's" for why decisions are what defines it.

    An adjudication contributes a position of its own, and that union is
    deliberately NOT justified as "the decisions might have gone away": they
    cannot. A conflict needs `reviewers_required` disagreeing decisions at its
    own `(stage, round, record)`, and a correction is a new row at that SAME
    position, so a position carrying an adjudication always carries decisions
    too — for data this service wrote. Mutating this union away leaves the whole
    suite green against a live database, which is exactly what says so.

    It is here because :class:`FlowInputs` is a public dataclass and `derive` is
    a total function of it. A caller — the property suite, a future collector, a
    migration replaying history — can hand over an adjudication whose decisions
    are not in the same object, and the alternative to the union is that the
    lead's ruling is silently discarded and the record settles from a stage
    below it. `test_an_adjudication_alone_still_settles_the_record` is the pin.
    """
    out: dict[UUID, set[StagePosition]] = {}
    for position, record_id in inputs.decisions:
        out.setdefault(record_id, set()).add(position)
    for position, record_id in inputs.adjudications:
        out.setdefault(record_id, set()).add(position)
    return out


def _counts_by_stage(
    inputs: FlowInputs,
    positions: dict[UUID, set[StagePosition]],
    dispositions: list[RecordDisposition],
) -> list[dict[str, Any]]:
    """THE PRISMA SPLIT — one tally per stage, beside the flat one, never inside it.

    PRISMA 2020 does not report "excluded" as one number. Records excluded at
    TITLE/ABSTRACT are their own box; reports excluded at FULL-TEXT eligibility
    are a different box, and that one is required to be broken down by reason.
    They answer different questions — a record dropped from its abstract was
    never retrieved, a report dropped at full text was read in full — and a
    diagram that cannot separate them is rejected.

    `counts` merged them, and every input needed to separate them was already
    here: :attr:`RecordDisposition.settled_at` names the stage each record
    settled at and was populated at both call sites in :func:`derive` with no
    consumer anywhere. This is a projection over what the ledger already knew.

    ## THE MEMBERS, AND WHICH PRISMA BOX EACH ONE IS

    Read this before transcribing any of them into a diagram. Two of the five
    tallies are PRISMA quantities, two are not, and the difference is not
    visible from the numbers.

    * ``reached`` — PRISMA's "reports assessed for eligibility" **for stage 2+**,
      and "records screened" for stage 1. Every record worked at this stage at
      all, whether or not it settled here. It is NOT derivable from the three
      tallies below, which count only the records that STOPPED here: a stage
      that passed everything on has zero in all three, and that is not a stage
      nobody looked at. Without `reached` the diagram has no arrow between one
      stage and the next. See the caveat on it below.
    * ``settled_exclude`` — PRISMA's "reports excluded", and at the full-text
      stage that is the box that must carry reasons. ``exclusion_reasons`` is
      that breakdown, for THIS stage only.
    * ``exclusion_reasons`` — the per-reason partition of ``settled_exclude``.
    * ``settled_uncertain`` — NOT a PRISMA box. PRISMA has no "unsure" arm; this
      is an operational state of this service.
    * ``settled_include`` — **NOT a PRISMA box, at any stage but the last**, and
      the reason it is not called `included`.

    ### Why `included` was the wrong name and `settled_include` is not

    It was `included`, and that name is a trap. What the number actually means is
    "records whose NEWEST WORK anywhere in this review is an `include` recorded
    at THIS stage". At the final stage that is the inclusion set. At any earlier
    stage it is "passed here, and no later stage has ruled yet" — a review in
    flight, not a finding.

    Measured on a real review: title/abstract `reached 10, settled_exclude 4,
    settled_include 3` while full-text `reached 3`. A reader transcribing
    `included` would put 3 into "studies included in review", and 3 is also
    inside the flat `counts.included` — so the same three records would be
    reported twice, once as a stage-1 outcome and once as the review's result,
    while the arrow `screened − excluded → assessed for eligibility`
    (10 − 4 = 6, not 3) visibly does not hold.

    `settled_include` names the MECHANISM rather than an outcome — the record
    settled here, with disposition `include` — which is exactly true at a
    terminal and at a non-terminal stage alike, and reads as nothing a PRISMA
    box is called. The review's inclusion set is `counts.included` and only ever
    that.

    ``settled_exclude`` and ``settled_uncertain`` are renamed with it for one
    reason: three tallies that partition "records that settled at this stage"
    should not be named by two different conventions, and `excluded` sitting
    beside `settled_include` would read as an oversight rather than a
    distinction. The prefix says what all three have in common.

    ## TWO INHERITED LIMITATIONS, PUBLISHED UNDER A STAGE NAME FOR THE FIRST TIME

    Both predate this projection and neither is fixed here. They are recorded
    HERE because this is the first output that attributes a number to a named
    stage, which is where a PRISMA author will look for its definition.

    (a) **A record excluded at title/abstract that is ALSO screened at full text
    loses its title/abstract exclusion entirely.** A record settles at ONE
    position — its newest work — so a later full-text decision replaces the
    earlier exclusion rather than sitting beside it, and the title/abstract
    tally simply does not contain it. This USED TO be the common case, because
    `create_assignments` opened the full-text stage over the whole corpus with
    no way to assign a subset — a reviewer who worked its queue top to bottom
    would produce exactly this shape for every record excluded at the abstract.
    It no longer is: `create_assignments` now assigns stage N > 1 only over
    records that settled `included` at stage N-1
    (`records_settled_included_at`), so a record excluded at title/abstract is
    not offered a full-text queue slot in the ordinary flow. The shape is still
    REACHABLE, through a correction rather than a fresh assignment: a record can
    settle `included` at title/abstract, be assigned and decided at full text,
    and only afterwards have its title/abstract decision corrected to `exclude`
    — a correction is a new row at the SAME position, so it does not retract the
    full-text assignment already made, and the full-text decision (the newer
    work) still wins the settling position. Fixing that residual case still
    means deciding whether a stage-2 decision left standing under a
    since-reversed stage-1 outcome is real work or an operator error, which is
    a product question, not a projection one.

    (b) **`reached` is "has at least one current decision here", not "was
    assessed".** A record with one of two required reviewers reported counts as
    reached, because the position exists the moment any reviewer records
    anything. So `reached` leads the truth on a stage in progress. It is the
    honest number for a FINISHED stage, which is when a diagram is drawn, and it
    is a ceiling for one still running.

    ## Which stages appear

    `inputs.stages` is the review's declaration and gives the order and the
    names. Any stage_key that decisions actually reached and the declaration
    does not mention is added from the position itself, named after its key: a
    per-stage tally that silently drops a stage would be the same class of
    defect as the merge it replaces. The declaration wins on a collision, so a
    renamed stage is labelled by `review_stages` and not by a screening row.

    ## Which records are in `reached`

    Two populations are deliberately excluded. A record `duplicate_removed`
    never entered screening as itself, and a record outside `record_ids` is not
    part of this review at all; counting either would put a number in the
    eligibility box that the flat ledger does not conserve. Because both are
    also unable to contribute to the three `settled_*` tallies — `derive` files a
    merged-away record as `duplicate_removed` with no `settled_at`, and it
    iterates `record_ids` — the identity `settled <= reached` holds by
    construction, and :func:`check_stage_conservation` asserts it anyway.
    """
    declared: dict[str, StageDescriptor] = {stage.stage_key: stage for stage in inputs.stages}
    for record_positions in positions.values():
        for position in record_positions:
            declared.setdefault(
                position.stage_key,
                StageDescriptor(
                    stage_order=position.stage_order, stage_key=position.stage_key, name=position.stage_key
                ),
            )

    screened_population = set(inputs.record_ids) - set(inputs.removed_by_merge)
    reached: dict[str, set[UUID]] = {}
    for record_id, record_positions in positions.items():
        if record_id not in screened_population:
            continue
        for position in record_positions:
            reached.setdefault(position.stage_key, set()).add(record_id)

    buckets_at: dict[str, Counter[str]] = {}
    reasons_at: dict[str, Counter[str]] = {}
    for disposition in dispositions:
        if disposition.settled_at is None:
            continue
        stage_key = disposition.settled_at.stage_key
        buckets_at.setdefault(stage_key, Counter())[disposition.bucket] += 1
        # THE SAME FILTER THE FLAT TALLY USES, and it has to be: `exclusion_reasons`
        # counts only `excluded` records carrying a code, so a per-stage tally
        # that counted anything else could not partition it. See `agreement_key`
        # for why a reason is load-bearing exactly where it is published.
        if disposition.bucket == "excluded" and disposition.exclusion_reason_code:
            reasons_at.setdefault(stage_key, Counter())[disposition.exclusion_reason_code] += 1

    out: list[dict[str, Any]] = []
    for stage in sorted(declared.values()):
        tally = buckets_at.get(stage.stage_key, Counter())
        out.append(
            {
                "stage_key": stage.stage_key,
                "stage_order": stage.stage_order,
                "name": stage.name,
                "reached": len(reached.get(stage.stage_key, ())),
                # `settled_include`, NOT `included` — see the docstring's
                # "Why `included` was the wrong name". The review's inclusion
                # set is `counts.included` and nothing here.
                "settled_include": tally.get("included", 0),
                "settled_exclude": tally.get("excluded", 0),
                "settled_uncertain": tally.get("uncertain", 0),
                "exclusion_reasons": dict(sorted(reasons_at.get(stage.stage_key, Counter()).items())),
            }
        )
    return out


def derive(inputs: FlowInputs) -> dict[str, Any]:
    identified_total = len(inputs.import_row_statuses)
    quarantined = sum(1 for s in inputs.import_row_statuses if s == "quarantined")
    rejected = sum(1 for s in inputs.import_row_statuses if s == "rejected")

    positions = _positions_by_record(inputs)

    dispositions: list[RecordDisposition] = []
    for record_id in sorted(inputs.record_ids, key=str):
        if record_id in inputs.removed_by_merge:
            dispositions.append(RecordDisposition(record_id, "duplicate_removed"))
            continue

        reached = positions.get(record_id)
        if not reached:
            # No decision and no adjudication anywhere. Nobody has looked at it.
            dispositions.append(RecordDisposition(record_id, "not_yet_screened"))
            continue

        # THE SETTLING POSITION: the NEWEST WORK on this record, not the last
        # stage it reached and not the last stage the review reached. See
        # `FlowInputs.settling_position` for why a stage order stops being able
        # to answer "which came later" the moment rounds exist.
        position = inputs.settling_position(record_id, reached)
        if position is UNATTRIBUTABLE:
            # ITS HISTORY CANNOT BE ORDERED. Some of the positions this record
            # reached are timed and some are not, so "the newest work on it" has
            # no answer that is not a guess about where the untimed one sits.
            #
            # Filed `not_yet_screened` with NO `settled_at`, which is the honest
            # reading of both fields: no stage can be shown to have settled it,
            # and attributing it to one would put a record in a
            # `counts_by_stage` tally on the strength of an ordering nobody can
            # produce. Conservation is untouched — it moves between two of the
            # five terminal buckets, and `check_stage_conservation` skips a
            # `settled_at is None` record exactly as it does for the other
            # `not_yet_screened` ones.
            #
            # `unattributable` carries the WHY to `flow.create_export`, whose
            # refusal has to name a remedy that works. See
            # `FlowInputs.settling_position` for why this is not a fallback and
            # not a raise.
            dispositions.append(RecordDisposition(record_id, "not_yet_screened", unattributable=True))
            continue

        adjudicated = inputs.adjudications.get((position, record_id))
        if adjudicated is not None:
            dispositions.append(
                RecordDisposition(
                    record_id, bucket_for(adjudicated[0]), adjudicated[1], adjudicated=True, settled_at=position
                )
            )
            continue

        votes = inputs.decisions.get((position, record_id), [])
        if len(votes) < inputs.reviewers_required_at(position):
            # The stage is in progress on this record: some reviewers have
            # reported and some have not. NOT a deadlock — the remaining
            # reviewer deciding settles it.
            dispositions.append(RecordDisposition(record_id, "not_yet_screened"))
            continue
        # `agreement_key`, and NOT `v[0]`. The disposition alone called two
        # reviewers agreed when they had given two different reasons for
        # excluding the same record, and this is one of the three places that
        # question is asked — see that function for why they are one function
        # and what silently went missing while they were not.
        unique = {agreement_key(*v) for v in votes}
        if len(unique) != 1:
            # Disagreement with no adjudication yet. Not screened — and a
            # conflict row exists for it, so a lead has a `conflict_id` to
            # resolve. That is the difference between this and the state the
            # cross-stage merge produced.
            dispositions.append(RecordDisposition(record_id, "not_yet_screened"))
            continue
        # THE AGREED REASON, and there is now only one of them.
        #
        # This used to be `next((v[1] for v in votes if v[1]), None)` — the
        # EARLIEST non-null reason — with a long comment saying so and calling
        # the loss deliberate. It is not deliberate any more: `agreement_key`
        # above has already refused to settle a record whose reviewers gave
        # different reasons, so every vote in `votes` carries the same one and
        # picking it out of the key is picking the only value there is.
        #
        # Read off the KEY rather than re-derived from `votes`, so the reason
        # that reaches the manifest is provably the one the agreement was
        # tested on. Re-deriving it is how the two could drift apart again.
        settled_disposition, settled_reason = unique.pop()
        dispositions.append(
            RecordDisposition(record_id, bucket_for(settled_disposition), settled_reason, settled_at=position)
        )

    buckets = Counter(d.bucket for d in dispositions)
    included = buckets.get("included", 0)
    excluded = buckets.get("excluded", 0)
    uncertain = buckets.get("uncertain", 0)
    duplicates_removed = buckets.get("duplicate_removed", 0)
    not_yet_screened = buckets.get("not_yet_screened", 0)
    records_screened = included + excluded + uncertain
    records_total = len(inputs.record_ids)

    counts = {
        "identified_total": identified_total,
        "quarantined": quarantined,
        "rejected": rejected,
        "records_total": records_total,
        "duplicates_removed": duplicates_removed,
        "records_screened": records_screened,
        "not_yet_screened": not_yet_screened,
        "included": included,
        "excluded": excluded,
        "uncertain": uncertain,
        "exclusion_reasons": dict(
            sorted(
                Counter(
                    d.exclusion_reason_code for d in dispositions if d.bucket == "excluded" and d.exclusion_reason_code
                ).items()
            )
        ),
        "adjudicated": sum(1 for d in dispositions if d.adjudicated),
    }

    # BESIDE `counts`, NEVER INSIDE IT. `inclusion-set-export.v2`'s
    # `$defs/flow_counts` is `additionalProperties: false` and its `$comment`
    # says why: the conservation identities are arithmetic over exactly those
    # twelve members, and a thirteenth tally arriving unnoticed would make the
    # sealed block say something the identities were never checked against.
    # Adding a member would also move `canonical_counts_hash` for every review
    # at once, staling every row in `flow_projections` and reporting
    # `projection_stale` fleet-wide — a false alarm about the integrity of the
    # ledger, raised by a reporting improvement.
    #
    # Nothing seals the `/flow` body and no schema declares it, so a sibling key
    # there is free and needs no contract release. The alternative — per-stage
    # tallies as sealed manifest members — needs
    # `urn:datacompany:inclusion-set-export:3` and is deliberately not this change.
    counts_by_stage = _counts_by_stage(inputs, positions, dispositions)

    check_conservation(counts)
    check_stage_conservation(counts, counts_by_stage, positions, dispositions)
    return {"counts": counts, "counts_by_stage": counts_by_stage, "dispositions": dispositions}


def records_settled_included_at(dispositions: list[RecordDisposition], stage_order: int) -> set[UUID]:
    """Record ids whose settled bucket is `included`, settled AT the stage
    whose `stage_order` is given — `create_assignments`' scope for stage
    `stage_order + 1` (`ER-042`).

    A record advances to stage N+1 only by having settled `included` at stage
    N. This is deliberately NOT a fourth definition of "included": it reads the
    answer straight off `derive(inputs)["dispositions"]`, the one object that
    already ran `agreement_key` and adjudication precedence and
    `settling_position`'s newest-work rule to decide both a record's bucket and
    the position it settled at. A hand-rolled SQL query for "reviewers agreed to
    include" would be a second spelling of `agreement_key`, which is exactly the
    drift its docstring warns three call sites against becoming four.

    Pure and Postgres-free — `dispositions` is a plain list, so this is testable
    against a hand-built `FlowInputs` the same way the rest of this module is.

    A record excluded, uncertain, `not_yet_screened`, `duplicate_removed`, or
    settled at any OTHER stage order (including a later one, for a record whose
    stage-N inclusion was later revisited) is not in the returned set — stage
    N+1's assignment scope is exactly "settled included at stage N", nothing
    looser.
    """
    return {
        disposition.review_record_id
        for disposition in dispositions
        if disposition.bucket == "included"
        and disposition.settled_at is not None
        and disposition.settled_at.stage_order == stage_order
    }


def check_conservation(counts: dict[str, Any]) -> None:
    """The invariant, checked on every derivation — not only in tests."""
    if counts["identified_total"] != counts["quarantined"] + counts["rejected"] + counts["records_total"]:
        raise ConservationError(
            f"identified_total {counts['identified_total']} != quarantined {counts['quarantined']} "
            f"+ rejected {counts['rejected']} + records_total {counts['records_total']}"
        )
    if counts["records_total"] != (
        counts["duplicates_removed"] + counts["records_screened"] + counts["not_yet_screened"]
    ):
        raise ConservationError(
            f"records_total {counts['records_total']} != duplicates_removed "
            f"{counts['duplicates_removed']} + records_screened {counts['records_screened']} "
            f"+ not_yet_screened {counts['not_yet_screened']}"
        )
    if counts["records_screened"] != counts["included"] + counts["excluded"] + counts["uncertain"]:
        raise ConservationError(
            f"records_screened {counts['records_screened']} != included {counts['included']} "
            f"+ excluded {counts['excluded']} + uncertain {counts['uncertain']}"
        )


def check_stage_conservation(
    counts: dict[str, Any],
    counts_by_stage: list[dict[str, Any]],
    positions: dict[UUID, set[StagePosition]],
    dispositions: list[RecordDisposition],
) -> None:
    """The per-stage tallies must PARTITION the flat ones AND name a stage each
    record actually reached — checked, not assumed.

    Raised for the same reason :func:`check_conservation` is. A per-stage
    breakdown that does not reconcile with `counts` is worse than no breakdown
    at all: it is a second set of numbers for the same review, published beside
    the sealed one, with nothing to tell a reader which is the ledger.

    ## THE TALLY IDENTITIES, AND WHY THEY ARE NOT ENOUGH ON THEIR OWN

    The first two checks below are sums, and a **misattribution preserves every
    sum**. Swap which stage two records settled at and `included`, `excluded`,
    `uncertain` and the merged `exclusion_reasons` are all unchanged — so the
    equalities pass, `canonical_counts_hash` does not move, and `GET /flow`
    serves the title/abstract reason under the full-text stage and vice versa.
    That is precisely the PRISMA lie this whole projection exists to end, and
    for one revision of this file the guard could not see it: only
    `settled <= reached` could ever fire on a swap, and on the ordinary
    two-stage shape — where the surviving records reached BOTH stages — it does
    not.

    Measured, on the canonical two-stage fixture, with every settled record
    shifted to the next declared stage: no `ConservationError`, an unchanged
    hash, and a served breakdown naming the wrong stage for both exclusions.

    ## THE ATTRIBUTION CHECK, WHICH IS PER-RECORD AND NOT PER-TALLY

    So the load-bearing check is the last one, and it is about identities rather
    than totals — the same reason
    `test_every_record_lands_in_exactly_one_terminal_bucket` exists beside the
    counts property: two records can swap places and leave every total intact.

    **Every record's settling position must be one that record actually
    reached.** `positions` is `_positions_by_record` — built from decisions and
    adjudications and from nothing else — and `settled_at` is chosen out of
    exactly that set by `FlowInputs.settling_position`, so this holds by
    construction today and the check is what pins the construction. Anything
    that ever picks a stage from somewhere else — a rewritten
    `settling_position`, a stage list consulted where it must not be, an
    off-by-one over `stage_order` — attributes a record to a stage that never
    worked it, and this is where that stops.

    What it deliberately does NOT do is recompute the tally from `dispositions`
    and compare. That would be a second spelling of `_counts_by_stage` living
    inside its own checker: it would agree with the code by construction while
    the code was wrong, which is the failure `agreement_key`'s docstring
    describes for the three copies of "are these reviewers agreeing". The
    cross-layer check that the tally matches what was attributed is the sealed
    export's `omissions` array, which reads `settled_at` independently.

    Every screened record settles at exactly ONE position, so the sums are
    equalities and not bounds. `reached` is the one inequality, because a stage
    may pass records on.
    """
    for bucket, member in (
        ("included", "settled_include"),
        ("excluded", "settled_exclude"),
        ("uncertain", "settled_uncertain"),
    ):
        total = sum(stage[member] for stage in counts_by_stage)
        if total != counts[bucket]:
            raise ConservationError(
                f"the per-stage {member} tallies sum to {total} but the flow reports "
                f"{counts[bucket]} {bucket}; the stage breakdown does not partition the ledger"
            )

    merged: Counter[str] = Counter()
    for stage in counts_by_stage:
        merged.update(stage["exclusion_reasons"])
    if dict(merged) != counts["exclusion_reasons"]:
        raise ConservationError(
            f"the per-stage exclusion reasons {dict(sorted(merged.items()))} do not partition "
            f"the flow's {counts['exclusion_reasons']}"
        )

    for stage in counts_by_stage:
        settled = stage["settled_include"] + stage["settled_exclude"] + stage["settled_uncertain"]
        if settled > stage["reached"]:
            raise ConservationError(
                f"stage {stage['stage_key']!r} settled {settled} records but only {stage['reached']} "
                "ever reached it, so the eligibility count is smaller than the population it describes"
            )

    # THE ONE CHECK A SWAP CANNOT SURVIVE. Per RECORD, not per tally.
    for disposition in dispositions:
        if disposition.settled_at is None:
            continue
        reached_by_record = positions.get(disposition.review_record_id, set())
        if disposition.settled_at not in reached_by_record:
            raise ConservationError(
                f"record {disposition.review_record_id} is reported settled at stage "
                f"{disposition.settled_at.stage_key!r} (order {disposition.settled_at.stage_order}, "
                f"round {disposition.settled_at.round_number}), which it never reached — it was worked "
                f"only at {sorted((p.stage_order, p.stage_key, p.round_number) for p in reached_by_record)}. "
                "The per-stage breakdown would attribute an exclusion to a stage that never saw the record, "
                "and every tally would still add up."
            )


def canonical_counts_hash(counts: dict[str, Any]) -> str:
    """RFC 8785 over the counts block, through the published contract wheel.

    The rebuild test compares this value, not the dict: two dicts with the same
    numbers in a different insertion order are the same fact, and a hash is the
    only comparison that says so without depending on Python's dict ordering.
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(counts)
