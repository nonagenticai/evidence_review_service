"""L2 — the ONLY module permitted to read `screening_decisions`.

`ER-004.2` (`01-…:471-472`) requires blinding in "repositories, list/detail
queries, cursors, counts, errors, events and exports". Six layers implement
that, and this module is L2: a single chokepoint, so that every read of a
decision goes through code that was reviewed for what it returns.

⚠️ **Five until `flow.require_no_owed_decisions`.** L1–L5 all narrow what a
caller reads about a decision ROW; none of them touches an AGGREGATE derived
from every reviewer's rows, which is what the flow ledger and the sealed export
serve. L6 is that layer, it lives in `application.services.flow`, and
:func:`an_aggregate_would_count_work_this_caller_may_not_read` below is the
predicate it asks — see the README's layer table.

L1 (the RLS policy in `ER-M005`) already prevents another reviewer's row from
being *selected*. L2 exists because L1 alone is not enough:

* a `COUNT(*)` under L1 returns the caller's own count, which is correct — but a
  handler that then compares it to `reviewers_required` and renders "1 of 2
  complete" has leaked the other reviewer's progress without ever reading their
  row. Counts are a leak channel, which is why
  :func:`own_decision_for` is the only per-record read exposed and there is no
  "how many decisions exist" function at all;
* `session.execute(text(...))` is not covered by L1 if a future maintainer binds
  a lead actor to run a report.

`tests/security/test_blinding_layers.py::test_l2_single_read_path` greps the
source tree and fails on any `ScreeningDecision` reference outside this module.
That test is what keeps this docstring true.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, aliased

from actor import Actor, ActorRole
from models.screening import ScreeningAssignment, ScreeningDecision

#: The same table, joined to itself to ask "did anything replace this row".
#: Module-level so every caller of :func:`_is_current` produces the identical
#: subquery and Postgres can reuse the plan.
_superseding = aliased(ScreeningDecision, name="superseding_decision")


def _is_current() -> ColumnElement[bool]:
    """ "Nothing supersedes me" — the predicate for the CURRENT decision.

    ## Why this is not `supersedes_decision_id IS NULL`

    It was, everywhere, and that selects the **first** decision of a supersession
    chain rather than the last. `screening_decisions` is append-only: a
    correction is a NEW row pointing AT the row it replaces, and
    `trg_screening_decisions_immutable` refuses any UPDATE, so the row being
    replaced keeps its `NULL` forever. The queue kept rendering the disposition
    the reviewer had already changed, `GET /decisions` returned the superseded
    row, and — the one that reaches the customer — the flow ledger and the
    hash-sealed export derived their buckets from it, so a record a reviewer
    corrected OUT of the inclusion set was sealed INTO it.

    ## Why an anti-join and not a new column

    A `superseded_at` or `is_current` column would have to be written by an
    UPDATE on a row the immutability trigger forbids updating. A chain-walk in
    Python is a statement per correction on the queue's hot path. The anti-join
    needs neither, and it needs **no migration**, because the index it wants is
    already there:

    `uq_decision_supersession_linear` (`0005_er_m005_screening.py:201-203`) is
    UNIQUE on `supersedes_decision_id WHERE NOT NULL`, so a decision can be
    superseded **at most once**. Every chain is therefore linear, and combined
    with `uq_decision_one_current_per_reviewer` (one chain HEAD per
    `(review, stage, round, record, reviewer)`) each reviewer has exactly one
    chain per record and it has exactly one tail. That same unique index is the
    lookup this subquery probes.

    ## ⚠️ WHERE "the same reviewer's" ACTUALLY COMES FROM — not from the schema

    ⚠️ **CORRECTED.** Two surviving mutants of this predicate were recorded as
    equivalent on the grounds that `uq_decision_supersession_linear` plus the
    self-referencing `supersedes_decision_id` foreign key give the property. They
    do not, and a reader sent to those two controls would be looking at the wrong
    thing. Between them they give exactly two facts: the row named exists
    (the FK), and it is named by at most one successor (the index). **Neither
    says the successor belongs to the same reviewer**, and no constraint on
    `screening_decisions` does. Reviewer `B` superseding reviewer `A`'s decision
    satisfies every check the database performs.

    The control that gives it is an APPLICATION one, in
    `application.services.screening.record_decision`
    (`screening.py:961-962`): a request naming `supersedes_decision_id` is
    refused unless that id is the caller's OWN current decision, resolved
    through :func:`own_decision_for`, which filters on
    `reviewer_subject_id = actor.subject_id` in SQL. `record_decision` is the
    only caller of :func:`insert_decision`, and L2 (`test_l2_single_read_path`)
    is what keeps it the only one — so the property holds for every write path
    that exists, and holds because of a check somebody can delete, not because
    of a constraint they cannot.

    That is where the argument below rests, and it is why this paragraph names
    it rather than leaving "because it is the same reviewer's decision" as an
    assertion about the data.

    ## Why RLS cannot starve it

    The subquery reads `screening_decisions`, so `screening_decisions_blinded`
    (`0008_er_m008_unblinding_audience.py:148-167`) applies to it too — and that
    is safe rather than merely tolerable. Every arm of that policy keys off
    `tenant_id`, `reviewer_subject_id`, `app.actor_role`, or the unblinding
    audience of the `(review, stage, round, record)`. A correction shares all
    four with the row it supersedes, because it is the same reviewer's decision
    on the same record — which is the application check named above, not
    something the schema enforces. So a predecessor the caller can see always
    has a successor the caller can see: the anti-join can never hide a
    correction and resurrect the row it replaced.

    ## What deliberately does NOT use this

    The two COUNT paths — :func:`own_progress_counts` and
    :func:`records_the_caller_still_owes` — keep `supersedes_decision_id IS
    NULL`, and are correct with it. Exactly one row per chain carries a NULL, so
    counting heads counts decided RECORDS, which is the question both are
    asking. Switching them would change no answer and would cost them the
    partial index `ix_decisions_reviewer_progress` (`0010`), which is defined
    `WHERE supersedes_decision_id IS NULL`. The predicates diverge on purpose,
    and each site says which it is using and why.
    """
    return ~(
        select(_superseding.decision_id)
        .where(_superseding.supersedes_decision_id == ScreeningDecision.decision_id)
        .exists()
    )


@dataclass(frozen=True)
class DecisionView:
    """What a caller is allowed to know about a decision.

    Deliberately not the ORM row: an ORM row serialized by a future handler
    would carry `reviewer_subject_id` into a response the moment somebody added
    `model_dump()`. This carries only fields whose disclosure was considered.
    """

    decision_id: uuid.UUID
    review_record_id: uuid.UUID
    #: The record version this judgement was taken against. Disclosure IS
    #: considered: the screening queue served this exact id to this exact
    #: reviewer for this record one request earlier, and a version id says what
    #: was read, never who decided what — so it carries no attribution. It is
    #: here because "the decision cites the content the reviewer saw" has to be
    #: checkable from outside the service.
    review_record_version_id: uuid.UUID
    #: WHERE the judgement was made. No new disclosure: the caller supplied the
    #: stage for their own rows, `GET /stages` lists every stage of the review to
    #: anybody who can read it, and a position says nothing about who decided
    #: what. It is here because the rule this module enforces is per POSITION —
    #: `records_the_caller_still_owes` — and a payload that cannot say which
    #: stage a decision belongs to cannot be checked against that rule from
    #: outside the service.
    stage_id: uuid.UUID
    round_number: int
    disposition: str
    exclusion_reason_code: str | None
    rationale: str | None
    decided_at: Any
    supersedes_decision_id: uuid.UUID | None
    #: Present ONLY for `ActorRole.LEAD` — the REALM role, tested by
    #: :func:`_may_attribute` and by nothing else. `None` otherwise, including
    #: for the reviewer's own rows: a reviewer does not need to be told their own
    #: subject id, and a field that is sometimes populated is one a client will
    #: start relying on.
    #:
    #: **This said "for a lead or after a recorded unblinding", and the second
    #: half was never true of this field.** An unblinding widens which ROWS L1
    #: lets a caller SELECT — `0008`'s policy has an audience arm — and it does
    #: not widen who is NAMED in them, because `_may_attribute` never reads the
    #: audience. So a non-lead in the declared audience of a recorded unblinding
    #: reads the disputed dispositions unattributed, which is the fail-closed
    #: half of the disagreement and, on the evidence, the intended one:
    #: `tests/unit/test_the_adjudicator_is_named_without_widening_blinding.py`
    #: exists specifically to keep reviewer attribution behind a ROLE test while
    #: the adjudicator's own id travels beside it. The docstring was corrected to
    #: the code rather than the other way round.
    #:
    #: The creator grant does not reach here either: `require_review_lead` admits
    #: `reviews.created_by_subject_id`, and that is authority over one review,
    #: not `actor.role`. A creator without the realm role reads what L1 shows
    #: them and is told nobody's name.
    reviewer_subject_id: str | None = None

    def to_public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "decision_id": str(self.decision_id),
            "review_record_id": str(self.review_record_id),
            "review_record_version_id": str(self.review_record_version_id),
            "stage_id": str(self.stage_id),
            "round_number": self.round_number,
            "disposition": self.disposition,
            "exclusion_reason_code": self.exclusion_reason_code,
            "rationale": self.rationale,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "supersedes_decision_id": (str(self.supersedes_decision_id) if self.supersedes_decision_id else None),
        }
        if self.reviewer_subject_id is not None:
            body["reviewer_subject_id"] = self.reviewer_subject_id
        return body


def _may_attribute(actor: Actor) -> bool:
    return actor.role is ActorRole.LEAD


def _view(row: ScreeningDecision, actor: Actor) -> DecisionView:
    return DecisionView(
        decision_id=row.decision_id,
        review_record_id=row.review_record_id,
        review_record_version_id=row.review_record_version_id,
        stage_id=row.stage_id,
        round_number=row.round_number,
        disposition=row.disposition,
        exclusion_reason_code=row.exclusion_reason_code,
        rationale=row.rationale,
        decided_at=row.decided_at,
        supersedes_decision_id=row.supersedes_decision_id,
        reviewer_subject_id=row.reviewer_subject_id if _may_attribute(actor) else None,
    )


def records_the_caller_still_owes(
    session: Session, *, review_id: uuid.UUID, actor: Actor
) -> set[tuple[uuid.UUID, int, uuid.UUID]]:
    """`(stage_id, round_number, review_record_id)` this subject owes a decision on.

    The set a caller may not read across on, whatever their role. Two statements,
    computed once per request — `list_conflicts` passes it into every
    :func:`visible_decisions` call rather than letting each one recompute it.

    ## PER POSITION, NOT PER RECORD

    This compared `assigned - decided` keyed by `review_record_id` alone, while
    both `screening_assignments` and `screening_decisions` are keyed by
    `(stage_id, round_number, review_record_id)`. So ONE decision anywhere in the
    review discharged the debt on that record EVERYWHERE in it.

    Measured, two stages, the lead also assigned as a reviewer at both:

        OTHER decides at title/abstract and at full text
        lead has decided nothing            -> lead sees 0 of their decisions
        lead decides at title/abstract ONLY -> lead sees 2 of their decisions

    The second of those two is the other reviewer's FULL-TEXT verdict, on a stage
    the lead has not ruled on and is still assigned to. That is precisely the
    disclosure :func:`visible_decisions` documents and refuses at stage one,
    reached by walking one stage further — and `#42` is what made a review with
    two working stages reachable at all.

    Rounds are in the key for the same reason: a re-screen is a fresh judgement,
    and having decided round 1 is not having decided round 2.
    """
    if not actor.subject_id:
        return set()
    assigned = set(
        session.execute(
            select(
                ScreeningAssignment.stage_id,
                ScreeningAssignment.round_number,
                ScreeningAssignment.review_record_id,
            ).where(
                ScreeningAssignment.review_id == review_id,
                ScreeningAssignment.reviewer_subject_id == actor.subject_id,
            )
        ).all()
    )
    if not assigned:
        return set()
    decided = set(
        session.execute(
            select(
                ScreeningDecision.stage_id,
                ScreeningDecision.round_number,
                ScreeningDecision.review_record_id,
            ).where(
                ScreeningDecision.review_id == review_id,
                ScreeningDecision.reviewer_subject_id == actor.subject_id,
                # DELIBERATELY the chain HEAD, not :func:`_is_current`. The
                # question is "has this caller decided this position at all", and
                # exactly one row per chain carries a NULL, so heads count
                # positions. `_is_current` would return the same set at the cost
                # of an anti-join per row.
                ScreeningDecision.supersedes_decision_id.is_(None),
            )
        ).all()
    )
    return {tuple(row) for row in assigned} - {tuple(row) for row in decided}


def an_aggregate_would_count_work_this_caller_may_not_read(
    session: Session, *, review_id: uuid.UUID, actor: Actor
) -> bool:
    """Would a review-wide COUNT include a row `visible_decisions` hides from this caller?

    The predicate an AGGREGATE read needs, expressed in terms of the one a ROW
    read already uses. :func:`visible_decisions` drops exactly the rows that
    another subject wrote at a position this caller still owes; this asks
    whether that filter would drop anything at all.

    ## Why an aggregate cannot use the row rule directly

    `visible_decisions` drops rows and keeps the rest, so a caller who has
    finished stage 1 still reads stage 1. A count has nothing to drop:
    `flow_derivation.derive`'s buckets sum to `records_total` — the conservation
    identity `tests/property/test_flow_conservation.py` pins — so blanking one
    bucket hands it back by subtraction, and at `records_total` 1 the total
    names the disposition on its own. The answer is therefore all-or-nothing,
    and this is the question that decides which.

    ## Why it is not simply "does the caller owe anything"

    That was the first shape of this function and it was too coarse by a wide
    margin: it refused the ledger to a caller who owed a position **nobody else
    had touched**, where the counts contain only the caller's own work and other
    reviewers' work at positions they do not owe — every row of which
    `GET /decisions` already serves them. Measured against the suite, that
    version reddened `test_the_creator_governs_their_own_review_and_only_theirs`,
    whose creator is the review's ONLY reviewer and can therefore learn nothing
    from it. A guard that refuses where there is nothing to disclose is not a
    stricter guard, it is a broken one, and it teaches the next reader that the
    rule is about roles again.

    ## ⚠️ CALL IT UNDER LEAD VISIBILITY

    The second statement reads OTHER subjects' rows, and L1 shows a non-lead
    only their own — so under the caller's own binding it returns nothing and
    this function answers `False` for precisely the callers it exists to refuse.
    `flow.require_no_owed_decisions` wraps it in
    `flow._reading_every_reviewers_work` for that reason, the same elevation
    `_collect` needs and for the same reason `_collect` needed it.

    Returns a BOOLEAN and never the rows or the positions. A caller handed the
    positions would be one refactor away from "3 of your 40 are already
    decided", which is the partner's progress in the refusal that exists to
    withhold it.
    """
    owed = records_the_caller_still_owes(session, review_id=review_id, actor=actor)
    if not owed:
        return False
    positions = session.execute(
        select(
            ScreeningDecision.stage_id,
            ScreeningDecision.round_number,
            ScreeningDecision.review_record_id,
        ).where(
            ScreeningDecision.review_id == review_id,
            # NOT the caller's own work. Their own rows at an owed position do
            # not exist — owing a position IS having written nothing there — but
            # stating it keeps the predicate true of what it claims rather than
            # true by accident of the data.
            ScreeningDecision.reviewer_subject_id != (actor.subject_id or ""),
            # The chain TAIL, matching what the ledger actually counts
            # (`unblinded_current_decisions`), not the head. A superseded row is
            # not in the counts, so it must not close the ledger either.
            _is_current(),
        )
    ).all()
    return any(tuple(position) in owed for position in positions)


def reviewer_subjects_owing_any_of(
    session: Session,
    *,
    review_id: uuid.UUID,
    stage_ids: Sequence[uuid.UUID],
    reviewer_subject_ids: Sequence[str],
) -> set[str]:
    """Which of `reviewer_subject_ids` still owe a decision somewhere in `stage_ids`.

    The same `assigned - decided` shape as :func:`records_the_caller_still_owes`,
    generalised over MANY subjects at once and scoped to a caller-chosen set of
    stages rather than the whole review.

    ## Why `create_assignments` needs this, and cannot compute it itself

    `application.services.screening.create_assignments` scopes a stage-N round to
    the records that settled `included` at stage N-1 — see that function's own
    docstring, "Stage order N > 1 scopes to what advanced". That scoping is safe
    for a reviewer who has ALREADY decided everything assigned to them at N-1:
    they already know their own vote, and the record's presence or absence in
    the N-1 tally is at most a fact about the OTHER reviewer relative to a vote
    the caller cast themselves — the same bounded inference `record_decision`
    already accepts when a conflict opens.

    It is NOT safe for a reviewer who has cast NO vote at N-1 and is handed a
    stage-N queue anyway: the queue's membership — which records are in it and
    which are not — is then the raw settling disposition of every record in the
    stage, read off a reviewer who contributed nothing to settling any of them.
    Measured (`ERS #65`, finding 2): a lead who owed both title/abstract
    verdicts, assigned to full text alongside a reviewer who had already
    screened, received a full-text queue containing exactly the records the
    other two reviewers had agreed to include — by record identity and title —
    before casting a single verdict of their own.

    `create_assignments` needs the answer to decide who to leave OUT of the
    stage-N insert, but it cannot read `screening_decisions` to compute it:
    `ScreeningDecision` may only be named inside this module (`test_l2_single_
    read_path` greps the tree for that). This is the read it uses instead — it
    returns only the offending subset of subjects, never a row, a disposition,
    or a record id.
    """
    if not stage_ids or not reviewer_subject_ids:
        return set()
    subjects = list(reviewer_subject_ids)
    assigned = set(
        session.execute(
            select(
                ScreeningAssignment.reviewer_subject_id,
                ScreeningAssignment.stage_id,
                ScreeningAssignment.round_number,
                ScreeningAssignment.review_record_id,
            ).where(
                ScreeningAssignment.review_id == review_id,
                ScreeningAssignment.stage_id.in_(stage_ids),
                ScreeningAssignment.reviewer_subject_id.in_(subjects),
            )
        ).all()
    )
    if not assigned:
        return set()
    decided = set(
        session.execute(
            select(
                ScreeningDecision.reviewer_subject_id,
                ScreeningDecision.stage_id,
                ScreeningDecision.round_number,
                ScreeningDecision.review_record_id,
            ).where(
                ScreeningDecision.review_id == review_id,
                ScreeningDecision.stage_id.in_(stage_ids),
                ScreeningDecision.reviewer_subject_id.in_(subjects),
                # DELIBERATELY the chain HEAD, not `_is_current` — see the
                # identical choice in `records_the_caller_still_owes`: exactly
                # one row per chain carries a NULL, so heads count positions.
                ScreeningDecision.supersedes_decision_id.is_(None),
            )
        ).all()
    )
    owing_positions = {tuple(row) for row in assigned} - {tuple(row) for row in decided}
    return {position[0] for position in owing_positions}


def visible_decisions(
    session: Session,
    *,
    review_id: uuid.UUID,
    actor: Actor,
    stage_id: uuid.UUID | None = None,
    round_number: int | None = None,
    review_record_id: uuid.UUID | None = None,
    current_only: bool = True,
    still_owed: set[tuple[uuid.UUID, int, uuid.UUID]] | None = None,
) -> list[DecisionView]:
    """Every decision this actor may see, under L1's policy — minus what they owe.

    L1's predicate is NOT restated here. L1 decides visibility; restating it in
    Python would create a second answer, and the two would drift, with the Python
    copy winning silently on the read path that matters.

    ## The one thing L1 CANNOT decide

    L1's second clause is `app.actor_role = 'lead'`, and a lead may read across
    reviewers because somebody has to adjudicate. Nothing in that policy — or
    anywhere else — stopped a lead from ALSO being an assigned reviewer on the
    same stage. Measured, with no unblinding recorded, no audience, no audit row
    and `/conflicts` reporting zero:

        reviewer R1 excludes a record
        the lead, assigned to that record and having decided NOTHING, reads
        `GET /decisions` and receives R1's `exclude` WITH `reviewer_subject_id`

    That is the blinding break `ER-004.2`, `ER-M008` and the declared-audience
    mechanism all exist to prevent, reached without touching any of them. It was
    unreachable in production only because `ActorRole.LEAD` was unreachable until
    the `review-lead` realm role was created; creating it made this live.

    The separation-of-duty trigger does not cover it. That refuses the lead as
    ADJUDICATOR of a conflict they voted in — correctly, and far too late: by the
    time there is a conflict to adjudicate, the disclosure has happened.

    So the rule is a caller property, not a role property, and it belongs in L2:
    **you may not read another reviewer's decision on a record you still owe a
    decision on.** A lead who is not screening is unaffected; a lead who is
    screening is a reviewer for exactly as long as their own verdict is
    outstanding.
    """
    stmt = select(ScreeningDecision).where(ScreeningDecision.review_id == review_id)
    if stage_id is not None:
        stmt = stmt.where(ScreeningDecision.stage_id == stage_id)
    if round_number is not None:
        stmt = stmt.where(ScreeningDecision.round_number == round_number)
    if review_record_id is not None:
        stmt = stmt.where(ScreeningDecision.review_record_id == review_record_id)
    if current_only:
        # The TAIL of each supersession chain — see :func:`_is_current`. With
        # the flag off, every row is still returned oldest-first, which is what
        # makes the append-only table readable as the audit trail it is.
        stmt = stmt.where(_is_current())
    stmt = stmt.order_by(ScreeningDecision.decided_at, ScreeningDecision.decision_id)
    rows = list(session.execute(stmt).scalars().all())

    owed = (
        records_the_caller_still_owes(session, review_id=review_id, actor=actor) if still_owed is None else still_owed
    )
    if owed:
        # Their OWN row at such a position still shows — a reviewer may always see
        # what they have said, and at an owed position they have said nothing, so
        # this removes only other people's.
        #
        # Matched on the ROW's position, not on the record: hiding a whole record
        # because one of its stages is owed would blank the caller's view of a
        # stage they have finished, and keying the debt by record alone let one
        # stage's decision unlock every other stage. See
        # :func:`records_the_caller_still_owes`.
        rows = [
            row
            for row in rows
            if (row.stage_id, row.round_number, row.review_record_id) not in owed
            or row.reviewer_subject_id == actor.subject_id
        ]

    return [_view(row, actor) for row in rows]


def own_decisions_for(
    session: Session,
    *,
    review_id: uuid.UUID,
    actor: Actor,
    stage_id: uuid.UUID,
    round_number: int,
    review_record_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, DecisionView]:
    """:func:`own_decision_for` for a page of records, in ONE statement.

    The queue is the reviewer's working screen and it called the single-record
    form per item — 2 statements per item counting the version read beside it,
    so 102 at the default page of 50 and 402 at the maximum of 200.

    Batching does not widen anything. The `reviewer_subject_id = actor.subject_id`
    predicate is unchanged and still in SQL, L1's policy is row-scoped so it
    applies identically to a set, and the result is keyed by record before it is
    used — a caller can only reach the row it asks for.

    That predicate is load-bearing and is NOT covered by L1 for every caller. A
    LEAD may read across reviewers under the policy, so without it a lead calling
    their own queue is handed another reviewer's disposition, with attribution,
    in `my_decision` — which is exactly what the single-record
    :func:`own_decision_for` documents it exists to stop.

    An earlier version of this docstring claimed
    `tests/security/test_blinding_layers.py` asserted that. **It did not**, and an
    adversarial pass proved it by deleting the predicate and watching all 749
    tests stay green. `test_a_leads_own_queue_shows_only_their_own_decisions`
    now covers it, and was checked by the same deletion.
    """
    if not actor.subject_id or not review_record_ids:
        return {}
    rows = (
        session.execute(
            select(ScreeningDecision).where(
                ScreeningDecision.review_id == review_id,
                ScreeningDecision.stage_id == stage_id,
                ScreeningDecision.round_number == round_number,
                ScreeningDecision.review_record_id.in_(review_record_ids),
                ScreeningDecision.reviewer_subject_id == actor.subject_id,
                _is_current(),
            )
        )
        .scalars()
        .all()
    )
    return {row.review_record_id: _view(row, actor) for row in rows}


def _records_removed_by_a_merge(session: Session, review_id: uuid.UUID) -> set[uuid.UUID]:
    """Deferred to `screening`, which owns the one spelling of this set.

    Imported at call time because `screening` imports this module at module
    scope; the direction only exists inside the call.
    """
    from application.services.screening import _records_removed_by_a_merge as impl

    return impl(session, review_id)


def own_progress_counts(
    session: Session,
    *,
    review_id: uuid.UUID,
    actor: Actor,
    stage_id: uuid.UUID,
    round_number: int,
) -> tuple[int, int]:
    """`(assigned, decided)` for THIS caller in THIS stage and round — both stage-wide.

    ## Why the two numbers are computed together

    They were not. `assigned_to_me` was a stage-wide `COUNT` and `decided_by_me`
    was `len([i for i in items if i["my_decision"]])` — the decisions on the PAGE
    — and the queue renders the pair as one ratio. At the default page of 50 a
    reviewer with 60 assignments therefore read "2 of 60" after deciding five
    records, because three of them were past the page boundary, and could never
    read more than "50 of 60" however much work they did. The counter cannot
    answer the only question it exists to answer.

    Returning both from one function is what stops that recurring: the scopes are
    now impossible to change independently, because there is one `WHERE` shape
    and one place to change it.

    ## Why it is still ONE statement

    Two scalar subqueries in a single `SELECT` with no `FROM`, not a join and not
    two round trips. `test_read_screen_throughput.py` asserts the queue's
    statement count is invariant in the page size AND bounded; this replaces the
    old `_own_assignment_count` statement rather than adding to it, so the
    measured count is unchanged at 9.

    A join would have been the other single-statement option and is worse: it
    makes `assigned` depend on the decision table's join cardinality, which is
    exactly the coupling that produced the defect.

    ## One statement is not the same as one cheap statement

    `0010_queue_progress_index` exists for the `decided` half: every index on
    `screening_decisions` put `review_record_id` ahead of `reviewer_subject_id`,
    so this count was answered by touching the whole STAGE — measured at 664
    shared buffers to return the number 300, with 12 index searches, and a plain
    `Seq Scan` discarding 21,334 rows on an adversarial reviewer's fixture. With
    the index it is one search. The `assigned` half was already served by
    `ix_assignments_queue`, which does lead with the reviewer.

    The four `decided` predicates are each pinned by a test that fails when it
    alone is removed — `reviewer_subject_id` by
    `test_a_leads_own_queue_shows_only_their_own_decisions`, the other three by
    `tests/api/test_the_queue_counts_the_whole_stage.py` — because three of them
    were mutated away with the whole suite still green when this function was
    first written. `review_id` is deliberately NOT pinned: `stage_id` is the
    primary key of `review_stages` and therefore already determines the review,
    so removing `review_id` changes no result. It stays because it matches the
    index's leading column and every other query in this module.

    ## Why it lives in L2

    It reads `screening_decisions`, and this module is the only one permitted to
    (`test_l2_single_read_path`). Nothing here widens L2's disclosure surface: an
    integer count of the CALLER'S OWN decisions, filtered on
    `reviewer_subject_id = actor.subject_id` in SQL exactly as
    :func:`own_decisions_for` is, tells the caller a fact they supplied. The
    counting leak this module's docstring warns about is a count of decisions the
    caller did NOT make — `reviewers_done`, "1 of 2 complete" — and there is
    still no function that can produce one.

    ## ⚠️ This counts chain HEADS, and that is not a copy of `own_decisions_for`

    It used to be. `own_decisions_for` and this function both said
    `supersedes_decision_id IS NULL` so that the page's `my_decision` flags and
    this count would agree — and they agreed on the **first** decision of a
    supersession chain, which was a live defect there and is not one here. The
    other one is fixed: `own_decisions_for` now uses :func:`_is_current`.

    The predicates therefore **diverge on purpose**, and this is the one that
    keeps the head:

    * the question here is "how many of my assigned records have I decided",
      whose unit is the RECORD. Exactly one row per chain carries a NULL —
      `uq_decision_one_current_per_reviewer` is a partial unique index over
      exactly this predicate — so heads count records, and a reviewer who
      changes their mind is still counted once
      (`test_a_corrected_decision_is_counted_once_not_twice`);
    * `_is_current()` would return the identical number, since a linear chain
      has one head and one tail, at the cost of an anti-join per row **and** of
      `ix_decisions_reviewer_progress` (`0010`), which is partial
      `WHERE supersedes_decision_id IS NULL` and cannot serve any other form.

    So: same answer, cheaper, and pinned. What must not come back is the
    assumption that the two sites share a predicate — they no longer do, and
    `test_the_queue_shows_the_corrected_disposition` fails if this one is
    copy-pasted back over `own_decisions_for`.
    """
    subject = actor.subject_id or ""
    if not subject:
        return (0, 0)
    # THE DENOMINATOR MUST MATCH THE QUEUE, or the counter is unreachable.
    #
    # `screening_queue` now filters out records a merge has already removed —
    # they are not work, and a verdict on one is discarded by `derive`. This
    # count did not, so the reviewer was served three records, decided all
    # three, and read "3 of 4": a progress bar that can never fill, on a record
    # they are never shown. That is the same "0 of 60" confusion the counter was
    # added to remove, one step along.
    #
    # `test_the_queue_never_reveals_the_other_reviewer_s_progress` is what caught
    # it — the queue fix alone left `assigned_to_me` at 4 while `decided_by_me`
    # reached 3.
    removed = _records_removed_by_a_merge(session, review_id)
    assigned = (
        select(func.count())
        .select_from(ScreeningAssignment)
        .where(
            ScreeningAssignment.review_id == review_id,
            ScreeningAssignment.stage_id == stage_id,
            ScreeningAssignment.round_number == round_number,
            ScreeningAssignment.reviewer_subject_id == subject,
            *([ScreeningAssignment.review_record_id.notin_(removed)] if removed else []),
        )
        .scalar_subquery()
    )
    decided = (
        select(func.count())
        .select_from(ScreeningDecision)
        .where(
            ScreeningDecision.review_id == review_id,
            ScreeningDecision.stage_id == stage_id,
            ScreeningDecision.round_number == round_number,
            ScreeningDecision.reviewer_subject_id == subject,
            # THE NUMERATOR MUST MATCH THE DENOMINATOR, for the same reason the
            # denominator must match the queue.
            #
            # #56 added `notin_(removed)` to `assigned` above and stopped there,
            # so a decision taken BEFORE the lead merged its record kept being
            # counted after the merge took that record out of the review. It is
            # still a live chain head — `screening_decisions` is append-only and
            # nothing supersedes it — so the numerator counts a record the
            # denominator has dropped. Measured on `bb7ea93`, three records
            # including one duplicate pair, all three screened and then merged:
            #
            #     {"count": 2, "total": 2, "assigned_to_me": 2, "decided_by_me": 3}
            #
            # "3 of 2", on the reviewer's ONLY completion signal. That is worse
            # than the "3 of 4" #56 was fixing: an unreachable denominator at
            # least reads as "there is work left", while a numerator past its
            # denominator says nothing at all about whether the reviewer may stop.
            #
            # The current studio MASKS it — `fr`'s `collectQueue` recomputes
            # `decided_by_me` from the joined items instead of reading the field,
            # so it renders "2 of 2" — and that is a reason to fix it here rather
            # than a reason not to: the defect is in the contract, and every other
            # consumer, including the next version of that one, reads it raw.
            #
            # The two counts are two subqueries of ONE statement precisely
            # because they have to move together. Filtering here rather than
            # deleting or superseding the decision is deliberate: the decision
            # was valid when it was taken, the table is append-only, and
            # rewriting a named human's recorded judgement to tidy a counter
            # would be a far worse defect than the counter
            # (`test_a_decision_taken_before_the_merge_is_not_unwritten`).
            #
            # `ix_decisions_reviewer_progress` still serves this: the four
            # columns it leads with are unchanged and `NOT IN` is a filter over
            # the rows the scan already returns, which is the same heap access
            # the RLS policy forces anyway (`0010` measured that and declined to
            # add `INCLUDE (tenant_id)` for exactly that reason). The list is
            # bound only when there IS a merge, so the common review pays
            # nothing.
            *([ScreeningDecision.review_record_id.notin_(removed)] if removed else []),
            # The chain HEAD, and NOT `_is_current()` — the two predicates now
            # differ and this one is deliberate. See the docstring section
            # below. `uq_decision_one_current_per_reviewer` is a partial unique
            # index over exactly this predicate, so this counts RECORDS and no
            # `DISTINCT` is needed to make it do so; `ix_decisions_reviewer_progress`
            # (`0010`) is partial on it too, so this is also the only form the
            # index can serve.
            ScreeningDecision.supersedes_decision_id.is_(None),
        )
        .scalar_subquery()
    )
    row = session.execute(select(assigned.label("assigned"), decided.label("decided"))).one()
    return (int(row.assigned), int(row.decided))


def own_decision_for(
    session: Session,
    *,
    review_id: uuid.UUID,
    actor: Actor,
    stage_id: uuid.UUID,
    round_number: int,
    review_record_id: uuid.UUID,
) -> DecisionView | None:
    """The CALLER'S OWN current decision on one record, or ``None``.

    This is what L3's queue renders. It is filtered on
    `reviewer_subject_id = actor.subject_id` in SQL as well as being covered by
    L1, because a lead calling the queue would otherwise see every reviewer's
    decision through their own queue — a leak the RLS policy deliberately
    permits (a lead may read across reviewers) and which is wrong *here*
    specifically.
    """
    if not actor.subject_id:
        return None
    row = session.execute(
        select(ScreeningDecision)
        .where(
            ScreeningDecision.review_id == review_id,
            ScreeningDecision.stage_id == stage_id,
            ScreeningDecision.round_number == round_number,
            ScreeningDecision.review_record_id == review_record_id,
            ScreeningDecision.reviewer_subject_id == actor.subject_id,
            _is_current(),
        )
        .limit(1)
    ).scalar_one_or_none()
    return _view(row, actor) if row else None


# --------------------------------------------------------------------------
# Privileged reads. Not blinded, and named so that is obvious at the call site.
# --------------------------------------------------------------------------


def unblinded_current_decisions(
    session: Session, *, review_id: uuid.UUID, stage_id: uuid.UUID | None = None
) -> list[ScreeningDecision]:
    """Every current decision, regardless of who is asking.

    Reachable only from a session bound with `ActorRole.LEAD`, because L1 is
    still in force — this function does not bypass the policy, it simply does
    not add a filter on top of it. Used by conflict detection and by flow
    derivation, both of which must count decisions they are not allowed to
    *display*.

    The distinction matters: `flow` returns aggregate counts and no
    attributions, so counting every decision is safe; returning these rows to a
    caller is not, which is why the return type is the raw model and every
    caller has to convert deliberately.
    """
    stmt = select(ScreeningDecision).where(
        ScreeningDecision.review_id == review_id,
        # The tail of each chain. This is the read the flow ledger and the
        # hash-sealed export derive their buckets from, so `IS NULL` here meant
        # a record a reviewer had corrected out of the inclusion set was sealed
        # into it — permanently, in an append-only artifact that gets cited.
        _is_current(),
    )
    if stage_id is not None:
        stmt = stmt.where(ScreeningDecision.stage_id == stage_id)
    # `decision_id` IS THE TIEBREAK, and without it this ORDER BY is a PARTIAL
    # order. `decided_at` is `server_default=now()`, which is TRANSACTION start
    # at microsecond resolution — not statement time — so every decision written
    # in one transaction carries the identical value and nothing in the query
    # decides their order. Postgres is then free to return them differently on
    # two reads of identical immutable history, which is precisely the property
    # `flow_derivation`'s docstring says a derived projection cannot have and
    # `flow:rebuild` exists to check. `flow:rebuild` could not catch it: it
    # compares a hash from this code against a hash from this code.
    #
    # Measured on 5481931, eight decisions written in one transaction and read
    # back: the ids came out in physical order, matching nothing the query
    # states. It reached an output too — `derive` picked the exclusion reason
    # off the FIRST vote, so a reason disagreement in a single transaction could
    # hash two ways. That particular route is closed now that a reason
    # disagreement is a disagreement, and the partial order is still wrong: the
    # docstring of every caller says these rows arrive ordered, and until this
    # line they only usually did.
    #
    # The same class as the unordered `select(ScreeningConflict)` scan fixed at
    # `flow.py:361`, and fixed the same way: state the full order.
    return list(
        session.execute(stmt.order_by(ScreeningDecision.decided_at, ScreeningDecision.decision_id)).scalars().all()
    )


def decision_by_id(session: Session, decision_id: uuid.UUID) -> ScreeningDecision | None:
    """Fetch one decision, subject to L1. ``None`` when invisible OR absent.

    The two are deliberately indistinguishable — that is L5: a caller must not
    be able to tell "you may not see this" from "this does not exist", because
    the former confirms existence.
    """
    return session.get(ScreeningDecision, decision_id)


def insert_decision(
    session: Session,
    *,
    review_id: uuid.UUID,
    tenant_id: uuid.UUID,
    stage_id: uuid.UUID,
    round_number: int,
    review_record_id: uuid.UUID,
    review_record_version_id: uuid.UUID,
    assignment_id: uuid.UUID,
    reviewer_subject_id: str,
    disposition: str,
    exclusion_reason_code: str | None,
    rationale: str | None,
    supersedes_decision_id: uuid.UUID | None,
) -> DecisionView:
    """The single WRITE path, and the only place a decision row is constructed.

    It takes fields rather than a `ScreeningDecision`, so that L2's grep covers
    writes as well as reads: no other module can name the model at all, which
    means no other module can construct one carrying somebody else's
    `reviewer_subject_id`. L1's ``WITH CHECK`` refuses that independently, but a
    guarantee with two independent enforcers is the point of the layering.

    Returns a `DecisionView`, not the row — the caller must not be handed an ORM
    object it could serialize wholesale into a response.
    """
    row = ScreeningDecision(
        decision_id=uuid.uuid4(),
        review_id=review_id,
        tenant_id=tenant_id,
        stage_id=stage_id,
        round_number=round_number,
        review_record_id=review_record_id,
        review_record_version_id=review_record_version_id,
        assignment_id=assignment_id,
        reviewer_subject_id=reviewer_subject_id,
        disposition=disposition,
        exclusion_reason_code=exclusion_reason_code,
        rationale=rationale,
        supersedes_decision_id=supersedes_decision_id,
    )
    session.add(row)
    session.flush()
    # The writer is always the subject, so attribution here discloses nothing
    # they did not just supply.
    return DecisionView(
        decision_id=row.decision_id,
        review_record_id=row.review_record_id,
        review_record_version_id=row.review_record_version_id,
        stage_id=row.stage_id,
        round_number=row.round_number,
        disposition=row.disposition,
        exclusion_reason_code=row.exclusion_reason_code,
        rationale=row.rationale,
        decided_at=row.decided_at,
        supersedes_decision_id=row.supersedes_decision_id,
    )
