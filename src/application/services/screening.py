"""Assignments, blinded decisions, conflicts, adjudication, unblinding — `ER-004`.

Layers L3, L4 and L5 of the blinding requirement live here; L1 is the RLS policy
in `ER-M005` and L2 is `blinded_reads`.

* **L3 — the queue shape.** `screening_queue` returns only the caller's own
  assignments, each with `my_decision` or `null`. There is deliberately no
  `decisions_so_far`, no `others_pending`, no `n_of_2` and no completion
  percentage: "1 of 2 done" tells reviewer B that reviewer A has decided, which
  is the fact blinding exists to hide. The caller's OWN progress —
  `assigned_to_me` / `decided_by_me` — is not that, and is served: it is a fact
  they supplied. Both halves of it are stage-wide, from one place
  (`blinded_reads.own_progress_counts`), because a ratio between a stage-wide
  numerator and a page-scoped denominator is a lie about the caller's own work.
* **L4 — the cursor.** `(assigned_at, assignment_id)` over the caller's OWN
  assignments. A cursor over `review_records` would expose gaps — records the
  caller cannot see because they are not assigned to them — and the *pattern of
  gaps* reveals the other reviewer's workload.
* **L5 — the error shape.** A decision on an unassigned record is `404` with the
  record identifier absent from the body, never `403`. A 403 confirms the record
  exists in this review, which is itself the disclosure (`01-…:728`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from actor import Actor, require_human
from api.problem_details import (
    NotFoundError,
    StateConflictError,
    UnprocessableStateError,
)
from application.services import blinded_reads, events
from application.services.imports import corpus_member_versions, current_record_versions
from application.services.reviews import active_protocol, require_review_lead_for, require_review_open
from log_config import get_logger
from models.records import CorpusVersion, CorpusVersionMember
from models.reviews import ReviewStage

# `ScreeningDecision` is deliberately NOT imported here. Only
# `application.services.blinded_reads` may name it; `tests/security/
# test_blinding_layers.py::test_l2_single_read_path` enforces that by grep.
from models.screening import (
    ScreeningAdjudication,
    ScreeningAssignment,
    ScreeningConflict,
    ScreeningUnblindingEvent,
)

logger = get_logger(__name__)

#: What a conflict-detail response says before an unblinding is recorded.
#: A literal string rather than an omitted key, so a client cannot mistake
#: "redacted" for "no decisions yet".
REDACTED = "redacted"

#: Assignment rows per INSERT statement.
#:
#: `ScreeningAssignment` binds eight columns per row — `assigned_at` is a server
#: default and is not sent — so 5,000 rows is 40,000 bind parameters, against the
#: wire protocol's ceiling of 65,535 per statement. The headroom is deliberate:
#: at the ceiling, adding one column to the table would turn the largest legal
#: batch into a runtime failure that only appears on the largest reviews.
#:
#: `.values([...])` renders ONE literal multi-VALUES statement — this is not
#: SQLAlchemy's `insertmanyvalues` executemany path, which would re-fragment it —
#: so the statement count is `ceil(rows / 5000)` and nothing else.
ASSIGNMENT_INSERT_CHUNK = 5_000


def _require_review_lead(session: Session, actor: Actor, review_id: uuid.UUID, what: str) -> str:
    """Lead authority over THIS review — the realm role, or having created it.

    The implementation moved to `reviews.require_review_lead_for` when
    `imports` turned out to need the same gate; this name stays because the call
    sites below read better with it. See `reviews.require_review_lead` for why
    creating a review confers governance of it.
    """
    return require_review_lead_for(session, actor, review_id, what)


def _stage(session: Session, review_id: uuid.UUID, stage_id: uuid.UUID) -> ReviewStage:
    stage = session.get(ReviewStage, stage_id)
    if stage is None or stage.review_id != review_id:
        raise NotFoundError("no such stage for this review")
    return stage


def _records_removed_by_a_merge(session: Session, review_id: uuid.UUID) -> set[uuid.UUID]:
    """Records whose CURRENT duplicate resolution is a merge that dropped them.

    The same three statements `flow._collect` builds its removal set from, and
    deliberately the same shape: the queue and the ledger must not disagree about
    which records are still in the review.
    """
    from application.services.deduplication import merged_groups

    return {
        member for group in merged_groups(session, review_id) for member in group.members if member != group.survivor
    }


def _round_lock_key(review_id: uuid.UUID, stage_id: uuid.UUID, round_number: int) -> int:
    """The advisory-lock key for one `(review, stage, round)`.

    Hashed HERE rather than with Postgres' `hashtextextended` so the key is a
    plain bind parameter and does not depend on an internal function's name or
    seed. A collision costs nothing but a little serialisation between two
    unrelated rounds — the lock guards a check-then-act, not a correctness
    invariant of its own.
    """
    digest = hashlib.blake2b(f"{review_id}:{stage_id}:{round_number}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _conflict_lock_key(
    review_id: uuid.UUID, stage_id: uuid.UUID, round_number: int, review_record_id: uuid.UUID
) -> int:
    """The advisory-lock key for one `(review, stage, round, record)`.

    Deliberately a DIFFERENT grain from `_round_lock_key`, and deliberately a
    different key space by construction: `_round_lock_key` hashes three fields
    and this hashes four, so a round key and a record key are never the same
    string. Sharing the round's key would serialise every decision in a round
    behind every other one — the busiest path in the service — to protect a
    check-then-act whose grain is one record.

    Hashed here, in Python, for the same reason `_round_lock_key` is: the key is
    then a plain bind parameter that does not depend on the name or seed of a
    Postgres internal. A collision costs a little needless serialisation between
    two unrelated records and nothing else.
    """
    digest = hashlib.blake2b(
        f"{review_id}:{stage_id}:{round_number}:{review_record_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


def _corpora_already_in_this_round(
    session: Session,
    review_id: uuid.UUID,
    *,
    stage_id: uuid.UUID,
    round_number: int,
) -> dict[uuid.UUID, int]:
    """`corpus_version_id -> assignment count` for one round, under a lock.

    `pg_advisory_xact_lock` and NOT a row lock, for two reasons. The row that
    would naturally carry this — `review_stages` — has had `UPDATE, DELETE`
    revoked from the runtime role by `0011`, and `SELECT ... FOR UPDATE` needs one
    of those privileges; and there is no row anywhere whose grain is
    `(review, stage, round)`, which is the grain that has to be serialised. An
    advisory lock is TRANSACTION-scoped, so it is released by the commit or the
    rollback and is safe behind PgBouncer's transaction pooling, where a
    session-scoped lock would leak onto whoever got the connection next.
    """
    session.execute(select(func.pg_advisory_xact_lock(_round_lock_key(review_id, stage_id, round_number))))
    rows = session.execute(
        select(ScreeningAssignment.corpus_version_id, func.count())
        .where(
            ScreeningAssignment.review_id == review_id,
            ScreeningAssignment.stage_id == stage_id,
            ScreeningAssignment.round_number == round_number,
        )
        .group_by(ScreeningAssignment.corpus_version_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def _next_free_round(session: Session, review_id: uuid.UUID, stage_id: uuid.UUID) -> int:
    """The lowest round of this stage that holds no assignments yet.

    Read only on the refusal path, so the happy path pays nothing for it. It is
    what makes that refusal's remedy carriable from the response body instead of
    guessable from the prose.
    """
    highest = session.execute(
        select(func.max(ScreeningAssignment.round_number)).where(
            ScreeningAssignment.review_id == review_id,
            ScreeningAssignment.stage_id == stage_id,
        )
    ).scalar()
    return int(highest or 0) + 1


def create_assignments(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    stage_id: uuid.UUID,
    round_number: int,
    reviewer_subject_ids: list[str],
    corpus_version_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Assign the corpus to every named reviewer — `ER-004.1`.

    "Balanced" assignment is not implemented, and its absence is the point for
    this slice: dual screening means *both* reviewers see *every eligible*
    record, so splitting it between them would silently turn a dual review into
    two single reviews.

    ## Stage order N > 1 scopes to what advanced (`ERS #42`)

    Every corpus member is assigned at stage order 1 — there is no earlier stage
    to have settled anything, so there is nothing to scope by, and this is
    still the whole corpus, unchanged from before this section existed. At
    stage order N > 1, only records that settled `included` at stage N-1
    (`flow_derivation.records_settled_included_at`, over
    `flow._collect`/`derive`'s own dispositions — the one place "included" is
    decided, not a second one built here) are assigned. A record excluded, or
    still `not_yet_screened`, at stage N-1 is not offered a stage-N queue slot.

    This was previously not true: every stage opened over the whole corpus, so
    a full-text stage offered records that were excluded at title/abstract and
    could never be retrieved — screening them was pure busywork on records the
    review had already dropped, and (see `flow_derivation`'s module docstring)
    the resulting stray decisions were exactly what made a record's settling
    STAGE ambiguous. Scoping the write here, rather than the read
    (`screening_queue`) or the ledger, is deliberate: `record_decision` already
    answers `404` for an unassigned record, so once assignment stops minting
    those rows the write path is closed with no new check and no blinding leak
    (a `404` still names no record). And because the insert is
    `ON CONFLICT DO NOTHING`, calling this again after more records settle
    `included` at N-1 tops up the round rather than erroring or duplicating —
    the one remedy a lead needs for "the prior stage wasn't finished yet when I
    first assigned this one."

    ## The caller-supplied corpus was never checked against the review

    `corpus_version_id` comes off the request body (`routers/screening.py:43`)
    and was used as given. Row-level security is scoped to the TENANT, not to the
    review, so a sibling review's corpus version is fully visible here — and
    `ER-004`'s blinding says nothing about it, because the disclosure is not a
    decision fact.

    Measured on `393ae61`, inside ONE tenant: the lead of review A posted review
    B's `corpus_version_id` and got `201`. The assignments are append-only and
    point at B's `review_record_id`s, so `GET /screening/queue` in review A then
    served B's titles, abstracts and DOIs — `screening_queue` reads the record
    version straight off the assignment and has no reason to doubt it. Every
    decision taken on them is written into A's ledger against a record A's corpus
    manifest does not contain.

    The check is the same shape as `deduplication.resolution_chain`, which found
    the identical hole on `cluster_id`: `404`, not `403`, and a body that names
    neither the corpus version nor the review it belongs to.

    ## One statement per (record, reviewer) did not survive a real corpus

    The insert was a nested loop issuing one `INSERT ... ON CONFLICT DO NOTHING
    RETURNING` per pair. Measured on `393ae61`: 200 records and two reviewers
    produced **400** `INSERT INTO screening_assignments` statements, counted at
    the DBAPI. It scales as `records x reviewers`, so a realistic first pass of
    3,000-8,000 records at two or three reviewers is 6,000-24,000 round trips,
    issued serially inside one transaction, through PgBouncer, on a synchronous
    route with no job behind it and nothing to resume from. The gateway gives up
    long before the transaction does, and the caller cannot tell a timeout from a
    failure — the work is still running, and a retry is safe only because of the
    unique key.

    ## A round names one corpus for its whole life

    `(stage, round)` is bound to the corpus its first assignment named, and a
    later call naming a different one is `409`, not a silent no-op reported as
    `201`. See the comment at the guard for the measurement; the short version is
    that `uq_assignment_once` makes re-pointing impossible and the response used
    to claim it had happened. The remedy the refusal names — a new round — is
    also what `create_export`'s G3 refusal now prescribes.

    It is now one multi-VALUES statement per `ASSIGNMENT_INSERT_CHUNK` rows, so
    the statement count depends on the chunk size and not on the corpus.
    `test_assignment_throughput.py` asserts that, at the DBAPI, because it is
    invisible from the response: the old and new forms return byte-identical
    bodies and differ only in how many times they went to the database.
    """
    _require_review_lead(session, actor, review_id, "assigning reviewers")
    require_review_open(session, review_id, "assigning reviewers")
    stage = _stage(session, review_id, stage_id)
    #: Populated only for `stage.stage_order > 1` — see the guard below. Declared
    #: here so the response always has an answer for "were any of the reviewers
    #: I named left out", `set()` (never assigned) reading the same as `set()`
    #: (nobody owed anything), rather than a `NameError` on the stage-1 path.
    owing: set[str] = set()

    protocol = active_protocol(session, review_id)
    if protocol is None:
        raise UnprocessableStateError(
            "the review is not activated against a locked protocol; assignments would have no criteria"
        )
    required = stage.reviewers_required or protocol.reviewers_required
    if len(set(reviewer_subject_ids)) < required:
        raise UnprocessableStateError(
            f"this stage requires {required} reviewers; {len(set(reviewer_subject_ids))} were named"
        )

    if corpus_version_id is None:
        from application.services.imports import latest_corpus

        corpus_version_id = latest_corpus(session, review_id).corpus_version_id
    else:
        corpus = session.get(CorpusVersion, corpus_version_id)
        if corpus is None or corpus.review_id != review_id:
            # Not `403`, and nothing about the other review in the body. A `403`
            # would confirm that this corpus version exists somewhere in the
            # tenant, and naming it back would confirm which id was probed —
            # both of which are the disclosure this refusal exists to prevent.
            raise NotFoundError("no such corpus version for this review")

    # A ROUND IS BOUND TO ONE CORPUS, AND CANNOT BE RE-POINTED AT ANOTHER.
    #
    # `uq_assignment_once` is `(review_id, stage_id, round_number,
    # review_record_id, reviewer_subject_id)` and deliberately EXCLUDES
    # `corpus_version_id` — widening it would let one reviewer hold two live
    # assignments at one position, and `record_decision`'s `scalar_one_or_none()`
    # would raise on the pair. The insert below is
    # `.on_conflict_do_nothing(constraint="uq_assignment_once")`, so an existing
    # assignment can never be moved onto a different corpus. The route said `201`
    # anyway and echoed back the corpus id it had just failed to store.
    #
    # `create_export`'s G3 refusal prescribed exactly this call ("re-run the stage
    # against the new corpus"), so the instruction a stranded lead is handed was
    # the one call guaranteed not to work. Measured on `a75da1a` against the local
    # database — 3-record corpus v1, screened, then a survivorship merge and a
    # re-seal to v2:
    #
    #   POST /assignments {round_number: 1, corpus: v2} -> 201
    #       corpus_version_id     v2      <- reported
    #       assignments_created   0
    #       the table held        v1 x3   <- stored
    #   GET  /screening/queue -> the pre-merge text; POST /exports -> the same 422.
    #
    # And the MIXED case is worse than the headline, which is what decided the
    # shape of this guard. The same call naming ONE MORE reviewer partially
    # succeeds, because the added reviewer's rows do not conflict:
    #
    #   POST /assignments {round_number: 1, reviewers: [a, b], corpus: v2} -> 201
    #       assignments_created   3
    #       the table held        v1 x3  AND  v2 x3
    #
    # One round, two corpora: two blinded reviewers reading different text at the
    # same position, with the response reporting a single `corpus_version_id` as
    # though the round were homogeneous. No value of that field describes that
    # round honestly, so reporting the STORED corpus instead of the requested one
    # is necessary and not sufficient — the state has to be unreachable.
    #
    # Refusing rather than repairing: `screening_assignments` is append-only by
    # trigger ("assignment inputs are immutable"), so there is no repair to make.
    # A round is a screening pass over a sealed set; a different sealed set is a
    # different pass, and the remedy is a new round — which is measured to work
    # end-to-end (round 2 against v2 -> queue serves v2 -> decisions cite v2 ->
    # `POST /exports` 201), so the refusal below sends the lead somewhere real.
    existing = _corpora_already_in_this_round(session, review_id, stage_id=stage_id, round_number=round_number)
    foreign = sorted(str(cv) for cv in existing if cv != corpus_version_id)
    if foreign:
        next_round = _next_free_round(session, review_id, stage_id)
        raise StateConflictError(
            f"round {round_number} of this stage is already screening corpus version "
            f"{', '.join(foreign)} ({sum(existing.values())} assignments). Assignments are "
            f"append-only, so this round cannot be re-pointed at corpus version {corpus_version_id}; "
            f"screen it as round {next_round} instead.",
            round_number=round_number,
            bound_corpus_version_ids=foreign,
            requested_corpus_version_id=str(corpus_version_id),
            existing_assignments=sum(existing.values()),
            next_round_number=next_round,
        )

    members = (
        session.execute(
            select(CorpusVersionMember.review_record_id).where(
                CorpusVersionMember.corpus_version_id == corpus_version_id
            )
        )
        .scalars()
        .all()
    )
    if not members:
        raise UnprocessableStateError("the sealed corpus has no members to assign")

    if stage.stage_order > 1:
        # SCOPE TO WHAT ADVANCED — see the docstring's "Stage order N > 1 scopes
        # to what advanced". Imported inside the function, the same shape
        # `flow` and `screening` already use to reach each other at call time
        # (`flow._unresolved_conflicts` above, `screening.current_adjudications`
        # in `flow.py`): the two modules import each other only lazily, so
        # there is no cycle at import time.
        #
        # `_collect` runs its privileged read of every reviewer's decisions —
        # the same one `flow.get_flow` uses. Reading it here is safe: the rows
        # never leave this function, and only `_collect`'s caller — a lead,
        # already gated by `_require_review_lead` above — triggers the read.
        #
        # ⚠️ "ONLY AGGREGATE DISPOSITIONS LEAVE THIS BLOCK" USED TO SIT HERE, AND
        # IT WAS FALSE (`ERS #65`, finding 2). `eligible`/`members` — the record
        # membership of the stage-N queue itself — leaves this block, becomes the
        # round's assignment rows below, and IS a per-record disposition: a
        # record's presence means it settled `included` at N-1, its absence
        # means it did not. Handing that queue to a reviewer who has cast no
        # vote of their own at N-1 discloses the other reviewers' outcome, by
        # record identity and title, before the recipient forms an opinion. See
        # the guard immediately below, which is the fix.
        from application.services.flow import _collect
        from domain.policies.flow_derivation import derive, records_settled_included_at

        inputs = _collect(session, review_id)
        dispositions = derive(inputs)["dispositions"]
        eligible = records_settled_included_at(dispositions, stage.stage_order - 1)
        members = [record_id for record_id in members if record_id in eligible]

        # A REVIEWER WHO STILL OWES STAGE N-1 IS NOT ASSIGNED AT STAGE N.
        #
        # Excluded from THIS insert only, not from the call: `create_assignments`
        # is `ON CONFLICT DO NOTHING` (see the module-level comment on
        # `ASSIGNMENT_INSERT_CHUNK` and the docstring above), so re-calling it
        # once the reviewer discharges their N-1 debt tops them up onto the same
        # round rather than erroring or duplicating — already the documented
        # remedy for "the prior stage wasn't finished when I first assigned this
        # one", and now also the remedy here. This also matches how a dual
        # screen actually runs: a reviewer finishes title/abstract before they
        # are ever handed full text.
        #
        # Scoped to the stage immediately below this one, not the whole review:
        # a reviewer who owes a DIFFERENT, unrelated stage has not been shown
        # anything this round's membership could leak.
        previous_stage_ids = (
            session.execute(
                select(ReviewStage.stage_id).where(
                    ReviewStage.review_id == review_id,
                    ReviewStage.stage_order == stage.stage_order - 1,
                )
            )
            .scalars()
            .all()
        )
        owing = blinded_reads.reviewer_subjects_owing_any_of(
            session,
            review_id=review_id,
            stage_ids=previous_stage_ids,
            reviewer_subject_ids=reviewer_subject_ids,
        )
        if owing:
            # ERS #65 finding 3: dropping these silently from `reviewer_subject_ids`
            # made a `201` with `reviewers: ["re-r1"]` indistinguishable from "you
            # asked for one reviewer" when the caller had actually named two and
            # one was excluded for owing stage N-1 — the "panel lies about
            # completeness" defect `owed_hidden` (`flow._unresolved_conflicts`)
            # exists to prevent, one endpoint over. `owing` is kept (not
            # discarded after the filter below) so the response can name it.
            #
            # Naming the SUBJECT, not merely a count, is safe here in a way
            # `owed_hidden` is NOT: `owed_hidden` counts conflicts the caller did
            # not already know existed, where an id would be new information.
            # Every id in `owing` was supplied by the caller in THIS request's
            # own `reviewer_subject_ids` — there is nothing here they did not
            # already know, only a REASON for why their own request did not do
            # what they asked for one of the names in it.
            reviewer_subject_ids = [r for r in reviewer_subject_ids if r not in owing]

    reviewers = sorted(set(reviewer_subject_ids))
    rows = [
        {
            "assignment_id": uuid.uuid4(),
            "review_id": review_id,
            "tenant_id": actor.tenant_id,
            "stage_id": stage_id,
            "round_number": round_number,
            "review_record_id": record_id,
            # ER-M009. Resolved above, used above to choose these very records,
            # and — until 0009 — reported in the response and then dropped. The
            # queue reads content through it, so storing it is what makes "the
            # reviewer saw the corpus that was sealed" a property of the data.
            "corpus_version_id": corpus_version_id,
            "reviewer_subject_id": reviewer,
            "assigned_by_subject_id": actor.subject_id or "",
        }
        for record_id in members
        for reviewer in reviewers
    ]

    created = 0
    for start in range(0, len(rows), ASSIGNMENT_INSERT_CHUNK):
        # ON CONFLICT DO NOTHING: re-assigning is a no-op rather than an error,
        # so a retried assignment call is safe. The unique key makes that exact.
        # It also — unlike `DO UPDATE`, which raises "cannot affect row a second
        # time" — tolerates a duplicate within ONE statement, so batching
        # introduces no new failure mode. Nothing generates one anyway:
        # `reviewers` is a sorted set and `corpus_version_members` holds each
        # record once.
        #
        # RETURNING, not `rowcount`: psycopg reports -1 for an `ON CONFLICT DO
        # NOTHING` insert, and summing that produced a negative
        # `assignments_created` — a count that was not merely wrong but wrong in
        # a direction no reader would question, because "-8" obviously means
        # "the counter is broken" only if somebody looks. `len(result.all())` is
        # the multi-row form of exactly that count: RETURNING emits a row per
        # INSERT that actually happened and nothing for one that conflicted.
        result = session.execute(
            pg_insert(ScreeningAssignment)
            .values(rows[start : start + ASSIGNMENT_INSERT_CHUNK])
            .on_conflict_do_nothing(constraint="uq_assignment_once")
            .returning(ScreeningAssignment.assignment_id)
        )
        created += len(result.all())
    session.flush()
    return {
        "review_id": str(review_id),
        "stage_id": str(stage_id),
        "round_number": round_number,
        # THE CORPUS THE ROUND HOLDS, not the one the request named.
        #
        # Taken from the read above where the round already had assignments, and
        # from the rows just written where it did not — and those rows are known
        # to have landed, because an empty round conflicts with nothing and
        # `members` is non-empty by the refusal two blocks up. So this field is a
        # statement about `screening_assignments` in every `201` path, which is
        # the property the echo lacked: it reported v2 for a round holding v1.
        #
        # ⚠️ AND NO TEST CAN TELL THE TWO APART TODAY, which is worth saying
        # rather than leaving for the next reader to discover. The guard above
        # makes `existing` a subset of `{corpus_version_id}`, so on every path
        # that reaches here the read and the echo return the SAME value — I
        # replaced this expression with `str(corpus_version_id)` and all nine
        # tests still passed. It is written as a read anyway, so the field states
        # what the database holds rather than what the request asked for if the
        # guard is ever bypassed or removed. Defence in depth, not an
        # independently verified behaviour, and it should not be counted as one.
        "corpus_version_id": str(next(iter(existing), corpus_version_id)),
        "reviewers": sorted(set(reviewer_subject_ids)),
        # ERS #65 finding 3. Every id here is one the caller already named in
        # `reviewer_subject_ids` — see the comment at the `owing` filter above
        # for why naming it back is not a new disclosure — so a `201` with
        # `reviewers` short of the request now says WHY, instead of silently
        # returning fewer names than were sent and leaving the caller to notice
        # the gap and guess. Always present, `[]` on the ordinary path and on
        # every stage-1 call (`owing` is never populated there — see its
        # declaration above), so a client can check this list rather than diff
        # the request against `reviewers` itself.
        "reviewers_excluded_owing_prior_stage": sorted(owing),
        # `records` is the eligible-set size — `len(members)` after the
        # "settled `included` at N-1" scoping above on a stage-N>1 call, the
        # whole corpus on stage 1 — reported to whichever actor is calling,
        # whether or not they are among the reviewers this round was scoped
        # for. ERS #65 finding 2: a lead who owes every N-1 verdict and knows
        # their own corpus size can read `records` as "N of my corpus
        # advanced" — the aggregate OUTCOME of partners' verdicts, shrunk to a
        # single number, with no record identity in it.
        #
        # Decided, not patched: this is the exact residual `owed_hidden`
        # (`flow._unresolved_conflicts`, `screening.list_conflicts`) already
        # accepts, and it is REDUNDANT with a disclosure this same caller can
        # already reach one call away. `GET /flow`'s `counts_by_stage`
        # (`api/routers/flow.py`, **lead only** — "PRISMA numbers ... belong to
        # the lead", not gated on whether the lead also owes a decision)
        # reports `settled_include` at stage N-1 review-wide, which is the
        # identical tally `records_settled_included_at` computes above to
        # build `eligible`. Filtering `records` to whatever this caller
        # "hasn't earned" would hide a number the SAME caller can read from a
        # route that requires nothing but the lead authority already checked
        # at the top of this function — buying an inconsistent field (its
        # meaning would depend on who asks) for a residual that costs nothing
        # to leave alone. Nothing PER-RECORD leaves this response either way;
        # only the count does, and the count was never secret from this caller.
        "records": len(members),
        # `assignments_created: 0` alone cannot distinguish "every row was
        # already there" from "the insert never ran", and that ambiguity is what
        # let the re-pointing call above read as a success: a lead who saw `0`
        # had nothing else to look at. This is what the round held BEFORE the
        # call, so `0 created / 12 existing` reads as "already done".
        "assignments_existing": sum(existing.values()),
        "assignments_created": created,
    }


def screening_queue(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    stage_id: uuid.UUID,
    round_number: int = 1,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """L3 + L4. The caller's own assignments, and nothing about anyone else's."""
    subject = require_human(actor)
    _stage(session, review_id, stage_id)

    # RECORDS A MERGE HAS ALREADY REMOVED ARE NOT WORK.
    #
    # Assignments are append-only and are minted before deduplication is
    # resolved, so a record the lead later merges away stays assigned. The queue
    # kept serving it. Measured against the deployed image, a 3-record corpus
    # with one duplicate pair merged:
    #
    #     flow duplicates_removed : 1
    #     queue length            : 3      <- the merged-away record is here
    #     decision on it          : 201    <- accepted
    #     ledger afterwards       : records_screened 0, included 0
    #
    # So the reviewer read it, judged it, and the ledger discarded the verdict —
    # `derive` buckets the record `duplicate_removed` whatever they said. Silent
    # on both sides: nothing told them the record was gone, and nothing told them
    # their decision would not count. On a 93-paper corpus that is real wasted
    # effort and a progress counter that counts work which cannot land.
    #
    # Filtered on the CURRENT resolution, so a merge later reversed by a split
    # returns the record to the queue rather than stranding it — the same reason
    # `flow._collect` reads the chain tail.
    #
    # This narrows the ASSIGNMENTS, not the page, and that is the whole of the
    # second fix. It used to be a Python filter applied to the rows AFTER
    # `LIMIT limit + 1` had been taken, which meant the lookahead row the page
    # size was measured against could itself be a removed record: the filter shrank
    # the window, `has_more = len(rows) > limit` then read false, and `next_cursor`
    # came back null while the reviewer still held assignments they had never been
    # shown. The cursor is opaque base64 over an internal key, so no client could
    # synthesise the next one and re-requesting page one returned page one — the
    # rest of their queue was unreachable through the only route that serves it.
    # Measured on `bb7ea93`, six live assignments after six pairs were merged,
    # `?limit=1`: `count=1 next_cursor=None assigned_to_me=6`, and at `?limit=5`
    # the reviewer reached one record of six.
    #
    # SIZE IT HONESTLY: it fires when a round holds MORE assignments than the page
    # limit AND a removed record falls in the window. The studio asks for 200
    # (`fr` `frontend/src/lib/api/reviews.ts` `QUEUE_PAGE_LIMIT`), so a review of
    # 200 records or fewer never sees it through that client. Above the limit it
    # is a hard block, measured on a 220-record round:
    #
    #     collectQueue(limit=200): 192 of assigned_to_me=210, 1 page, next_cursor=None
    #     POST /exports          : 422 "18 records are not yet screened"
    #     POST /flow:rebuild     : does not help
    #
    # `POST :close` is NOT blocked — it gates on unresolved conflicts, not on
    # `not_yet_screened` — so the review can be closed while its export cannot be
    # sealed. And there IS an escape: splitting every merge returns the records to
    # the queue. It re-introduces the #56 defect the merge filter exists to fix, so
    # it is recoverable-but-terrible rather than unrecoverable.
    #
    # Pushed into the predicate rather than fixed by over-fetching in a loop
    # because the set is already in hand: `_records_removed_by_a_merge` reads the
    # whole review's merged groups into Python whatever we do with them, and
    # `blinded_reads.own_progress_counts` — called once more in this same request,
    # a few lines below — already binds this identical set as a `NOT IN` list. So
    # this adds no round trip, no new class of cost and no second answer to "which
    # records are still in the review"; it moves an existing filter to the one
    # place where `LIMIT` can see it. An over-fetch loop would have been a second
    # paging mechanism to keep correct beside the cursor, and its termination
    # condition is exactly the thing that was wrong here.
    removed = _records_removed_by_a_merge(session, review_id)

    stmt = (
        select(ScreeningAssignment)
        .where(
            ScreeningAssignment.review_id == review_id,
            ScreeningAssignment.stage_id == stage_id,
            ScreeningAssignment.round_number == round_number,
            # The caller's OWN assignments. A lead reading the queue sees their
            # own too, not everyone's — the queue is a work list, and a lead's
            # cross-reviewer read belongs on the conflicts route.
            ScreeningAssignment.reviewer_subject_id == subject,
            # Spelled conditionally for the same reason `own_progress_counts`
            # spells it conditionally: `NOT IN ()` over an empty list is not a
            # tautology every backend agrees about, and the overwhelmingly common
            # case is a review with no merges at all.
            *([ScreeningAssignment.review_record_id.notin_(removed)] if removed else []),
        )
        .order_by(ScreeningAssignment.assigned_at, ScreeningAssignment.assignment_id)
        .limit(limit + 1)
    )
    if cursor:
        # L4: the cursor is over the caller's own assignments, so it cannot
        # expose a gap where another reviewer's record would have been.
        try:
            after, last_id = _decode_cursor(cursor)
            # Compared as a datetime, not as text. A row-value comparison
            # against a string makes Postgres compare `timestamptz > text`,
            # which it refuses outright.
            stmt = stmt.where(
                tuple_(ScreeningAssignment.assigned_at, ScreeningAssignment.assignment_id)
                > tuple_(literal(after), literal(last_id))
            )
        except (ValueError, TypeError, binascii.Error) as exc:
            raise UnprocessableStateError("malformed cursor") from exc

    rows = list(session.execute(stmt).scalars().all())

    # L4 IS UNAFFECTED BY THE REMOVAL FILTER, whichever side of `LIMIT` it sits.
    #
    # The cursor is over the caller's OWN assignments, and removal is a property
    # of the RECORD, not of who is assigned to it: every reviewer loses exactly
    # the same rows. So a gap in one reviewer's key sequence reveals nothing about
    # another reviewer's work, and the fact that the cursor now steps over removed
    # assignments rather than being handed them and discarding them changes
    # nothing a caller can observe about anybody else.
    #
    # `limit + 1` is now taken over POST-filter rows, so this reads what it says.
    has_more = len(rows) > limit
    rows = rows[:limit]

    # Two batched reads for the page, not two per item. The per-item form cost
    # 2N+2 statements — 102 at the default page of 50, 402 at the maximum of 200
    # — on the screen a reviewer spends the whole review inside.
    page_record_ids = [a.review_record_id for a in rows]
    # ER-M009. THROUGH the corpus each assignment was minted from, never the
    # review's tail. `current_record_versions` here meant that a survivorship
    # merge changed what an already-assigned reviewer was shown, mid-stage,
    # with no event and nothing in the payload to indicate it — and the
    # decision they then recorded named content the sealed corpus does not
    # contain. `create_export` refuses that at seal time, which is after the
    # human judgement it invalidates has already been made.
    #
    # Grouped because a page is only guaranteed to share a stage and a round,
    # not a corpus. In practice every row shares one and this is a single
    # statement, exactly as before — the grouping is what keeps it correct if
    # that ever stops being true.
    by_corpus: dict[uuid.UUID, list[uuid.UUID]] = {}
    for assignment in rows:
        by_corpus.setdefault(assignment.corpus_version_id, []).append(assignment.review_record_id)
    versions: dict[uuid.UUID, Any] = {}
    for corpus_version_id, record_ids in by_corpus.items():
        versions.update(corpus_member_versions(session, corpus_version_id, record_ids))
    own_decisions = blinded_reads.own_decisions_for(
        session,
        review_id=review_id,
        actor=actor,
        stage_id=stage_id,
        round_number=round_number,
        review_record_ids=page_record_ids,
    )

    items: list[dict[str, Any]] = []
    for assignment in rows:
        version = versions.get(assignment.review_record_id)
        own = own_decisions.get(assignment.review_record_id)
        items.append(
            {
                "assignment_id": str(assignment.assignment_id),
                "review_record_id": str(assignment.review_record_id),
                "review_record_version_id": str(version.review_record_version_id) if version else None,
                "title": version.title if version else None,
                "abstract": version.abstract if version else None,
                "publication_year": version.publication_year if version else None,
                "authors": version.authors if version else None,
                "container_title": version.container_title if version else None,
                "doi": version.doi if version else None,
                "record_type": version.record_type if version else None,
                # The ONLY decision fact in this payload, and it is the
                # caller's own. No `decisions_so_far`, no `others_pending`, no
                # `n_of_2`, no percentage.
                "my_decision": own.to_public() if own else None,
            }
        )

    # Counts of the caller's OWN work only — facts the caller already knows —
    # and BOTH over the whole stage. See `blinded_reads.own_progress_counts` for
    # why they are one statement and why they must move together.
    assigned_to_me, decided_by_me = blinded_reads.own_progress_counts(
        session,
        review_id=review_id,
        actor=actor,
        stage_id=stage_id,
        round_number=round_number,
    )

    return {
        "review_id": str(review_id),
        "stage_id": str(stage_id),
        "round_number": round_number,
        "items": items,
        #: THIS PAGE. `count` is `len(items)` and nothing else; the stage-wide
        #: figure is `total`. The two were indistinguishable while every counter
        #: beside them was page-scoped, which is how `decided_by_me` stayed a
        #: page count for as long as it did.
        "count": len(items),
        #: THE STAGE. Same number as `assigned_to_me`, carried under the name
        #: `/conflicts` already uses for a stage-wide denominator, so a client
        #: holding one page cannot render a ratio out of `len(items)`.
        "total": assigned_to_me,
        "assigned_to_me": assigned_to_me,
        #: THE STAGE, since this fix. It was the number of decided records ON
        #: THIS PAGE, rendered by the studio against a stage-wide
        #: `assigned_to_me`: a reviewer with 60 assignments who had decided
        #: everything past record 50 read "0 of 60" and could not tell whether
        #: they were finished. The per-item `my_decision` is still the page's
        #: business, because that one IS about the page.
        "decided_by_me": decided_by_me,
        "next_cursor": (_encode_cursor(rows[-1].assigned_at, rows[-1].assignment_id) if has_more and rows else None),
    }


def _encode_cursor(assigned_at: datetime, assignment_id: uuid.UUID) -> str:
    """Opaque, URL-safe, and still an ASSIGNMENT key (L4).

    Base64url rather than the raw `"<iso>|<uuid>"` pair: an ISO-8601 timestamp
    ends in `+00:00`, and `+` in a query string decodes to a space, so the plain
    form parsed back as an invalid datetime and the second page of every queue
    returned 422. Opacity is a side benefit — the value the caller round-trips
    is not something they should be constructing by hand.
    """
    raw = f"{assigned_at.isoformat()}|{assignment_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    assigned_at_raw, assignment_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
    return datetime.fromisoformat(assigned_at_raw), uuid.UUID(assignment_id)


def record_decision(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    stage_id: uuid.UUID,
    round_number: int,
    review_record_id: uuid.UUID,
    disposition: str,
    exclusion_reason_code: str | None = None,
    rationale: str | None = None,
    supersedes_decision_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """`ER-004.3` — an append-only decision, attributed to the authenticated human.

    The reviewer is derived from the credential and never from the body. There
    is no parameter for it, which is the strongest form of "reject body actor".
    """
    subject = require_human(actor)
    require_review_open(session, review_id, "recording a decision")
    stage = _stage(session, review_id, stage_id)

    assignment = session.execute(
        select(ScreeningAssignment).where(
            ScreeningAssignment.review_id == review_id,
            ScreeningAssignment.stage_id == stage_id,
            ScreeningAssignment.round_number == round_number,
            ScreeningAssignment.review_record_id == review_record_id,
            ScreeningAssignment.reviewer_subject_id == subject,
        )
    ).scalar_one_or_none()
    if assignment is None:
        # L5. 404, and the record identifier is NOT echoed: a body containing
        # the id the caller sent would confirm the record exists here.
        raise NotFoundError("no such assignment")

    # A VERDICT THAT PROVABLY CANNOT LAND IS REFUSED, NOT ACCEPTED AND DISCARDED.
    #
    # This had no removal guard at all. A decision on a record a merge had
    # already taken out of the review returned 201, and `derive` then bucketed
    # that record `duplicate_removed` whatever the reviewer said — measured on
    # `bb7ea93` and written down in `_records_removed_by_a_merge`'s own comment:
    # `decision on it -> 201`, `ledger afterwards -> records_screened 0`. Silent
    # in both directions. The reviewer was told their judgement was recorded and
    # nothing ever counted it.
    #
    # REFUSED rather than accepted, which is the choice worth stating. The
    # alternative is a write whose only possible outcomes are "ignored" and
    # "misleads somebody later": it cannot reach the ledger, it cannot reach the
    # export, and it inflates `decided_by_me` — the defect immediately above this
    # one — for as long as it exists. An error the caller can read is strictly
    # better than a 201 that means nothing, and there is nothing to lose by
    # refusing, because the record is not in the review any more and no verdict on
    # it is wanted.
    #
    # 422, not 404. The assignment above proved the caller holds this record, they
    # can see it in `GET /records`, and `GET /decisions` will show them their own
    # earlier decision on it — so a 404 is a claim they can immediately disprove,
    # and the one thing it would hide is not secret from them. What is wrong is the
    # STATE of the review, which is what `unprocessable_state` names throughout
    # this module, and the detail says a merge is what removed it so the caller can
    # tell this apart from "you have no assignment".
    #
    # Only the CURRENT resolution counts, exactly as in the queue: a merge later
    # reversed by a split makes the record decidable again rather than stranding
    # it. The shared helper is used rather than a cheaper single-record probe so
    # that the write, the queue and the ledger cannot drift into three answers to
    # "which records are still in the review" — the same argument that helper's
    # docstring already makes for the queue and `flow._collect`. It costs one
    # statement on a review that has never been deduplicated and three on one that
    # has, per decision POST.
    #
    # Decisions already written are NOT touched. They were valid when they were
    # taken, `screening_decisions` is append-only, and unwriting a named human's
    # judgement to tidy a counter would be a far worse defect than the counter.
    if review_record_id in _records_removed_by_a_merge(session, review_id):
        raise UnprocessableStateError(
            "this record was removed from the review by a duplicate merge, so a decision on it "
            "cannot be counted: the flow ledger buckets it as duplicate_removed whatever the "
            "verdict says. Screen the surviving record of the cluster instead, or ask the lead to "
            "split the merge if it was wrong.",
            review_record_id=str(review_record_id),
        )

    protocol = active_protocol(session, review_id)
    if protocol is None:
        raise UnprocessableStateError("the review is not activated against a locked protocol")
    if disposition == "exclude":
        vocabulary = list(protocol.exclusion_reason_vocabulary or [])
        if exclusion_reason_code not in vocabulary:
            # Judged against the vocabulary of the protocol this decision is
            # being taken under, not against whatever the vocabulary is now.
            raise UnprocessableStateError(
                "exclusion reason is not in the locked protocol's closed vocabulary",
                allowed=vocabulary,
            )

    # THROUGH THE ASSIGNMENT'S CORPUS, never the review's tail.
    #
    # This was `ORDER BY version_number DESC LIMIT 1`, while `screening_queue`
    # serves `corpus_member_versions(assignment.corpus_version_id, ...)` — the
    # version the corpus sealed. The two disagree the moment `apply_survivorship`
    # mints a v2, which is a lead action taken between the assignment and the
    # decision and invisible to the reviewer. So the reviewer read v1 and the
    # service recorded a judgement against v2, content they were never shown.
    #
    # `create_export` then refused the seal with "the corpus was re-sealed after
    # screening" — naming a cause that had not occurred, on the corpus the
    # reviewer was assigned from and the export itself cites. Its two remedies
    # were "export before re-sealing", which is impossible when nothing was
    # re-sealed, and "re-run the stage against the new corpus", which asks for a
    # judgement on content nobody has read.
    #
    # An absent member is a fact about the corpus, not a read that went wrong —
    # `corpus_version_members` is immutable and checked by a deferred trigger at
    # commit — so it is refused rather than fallen back to the tail. A fallback
    # here would restore the defect for exactly the records the corpus does not
    # contain, which are the ones it matters most for.
    version = corpus_member_versions(session, assignment.corpus_version_id, [review_record_id]).get(review_record_id)
    if version is None:
        raise UnprocessableStateError(
            "this record is not a member of the corpus version this assignment was minted from, "
            "so there is no version of it that you were shown to attribute a decision to",
            corpus_version_id=str(assignment.corpus_version_id),
        )

    previous = blinded_reads.own_decision_for(
        session,
        review_id=review_id,
        actor=actor,
        stage_id=stage_id,
        round_number=round_number,
        review_record_id=review_record_id,
    )
    if previous is not None and supersedes_decision_id is None:
        raise StateConflictError(
            "you have already decided this record; a correction must supersede the previous decision",
            current_decision_id=str(previous.decision_id),
        )
    # ⛔ THE ONLY CONTROL THAT MAKES A SUPERSESSION CHAIN BELONG TO ONE REVIEWER.
    #
    # `previous` is `own_decision_for`, filtered on `reviewer_subject_id` in SQL,
    # so this refuses a request naming somebody else's decision. Nothing in the
    # schema does: `uq_decision_supersession_linear` makes a chain LINEAR and the
    # self-referencing FK makes the named row EXIST, and neither says whose row
    # it is. `blinded_reads._is_current`'s "why RLS cannot starve it" argument
    # rests on this line — a cross-reviewer supersession would let the anti-join
    # hide a decision the caller may read behind a successor they may not, which
    # is the ledger reading a corrected-away disposition as current.
    if supersedes_decision_id is not None:
        if previous is None or previous.decision_id != supersedes_decision_id:
            raise StateConflictError("the decision being superseded is not your current decision")

    # Constructed inside `blinded_reads`, never here. That module is the only
    # one permitted to name the model at all (L2), which is what stops a future
    # handler from writing a decision attributed to another reviewer.
    decision = blinded_reads.insert_decision(
        session,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        stage_id=stage_id,
        round_number=round_number,
        review_record_id=review_record_id,
        review_record_version_id=version.review_record_version_id,
        assignment_id=assignment.assignment_id,
        reviewer_subject_id=subject,
        disposition=disposition,
        exclusion_reason_code=exclusion_reason_code,
        rationale=rationale,
        supersedes_decision_id=supersedes_decision_id,
    )

    _maybe_open_conflict(
        session,
        actor,
        review_id=review_id,
        stage_id=stage_id,
        round_number=round_number,
        review_record_id=review_record_id,
        reviewers_required=stage.reviewers_required or protocol.reviewers_required,
    )

    # L5 again: the log line carries the decision id and nothing else. No
    # disposition, no subject, no record title.
    logger.info("screening decision recorded", extra={"decision_id": str(decision.decision_id)})

    events.emit_decision_recorded(
        session,
        decision,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        stage_id=stage_id,
        round_number=round_number,
    )

    return {
        "decision_id": str(decision.decision_id),
        "review_record_id": str(review_record_id),
        # The version this judgement is attributed to. Leaks nothing: the queue
        # already served this exact id to this exact reviewer for this record one
        # request earlier, and a version id is not an attribution — it says what
        # was read, never who decided what. Returning it is what makes "the
        # decision cites the content the reviewer saw" checkable from outside the
        # service, rather than a claim that can only be verified by reading rows.
        "review_record_version_id": str(decision.review_record_version_id),
        "disposition": decision.disposition,
        "exclusion_reason_code": decision.exclusion_reason_code,
        "supersedes_decision_id": str(supersedes_decision_id) if supersedes_decision_id else None,
        "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
        # NOTHING about the conflict. An earlier version of this response carried
        # `conflict_opened`, which reads like a harmless progress flag and is
        # not: with two reviewers and a shared record, "a conflict opened" tells
        # the second reviewer that the first one disagreed — which, from a
        # binary disposition, IS the first reviewer's disposition. The conflict
        # is visible on the lead's `/conflicts` route, where it belongs.
    }


def _maybe_open_conflict(
    session: Session,
    actor: Actor,
    *,
    review_id: uuid.UUID,
    stage_id: uuid.UUID,
    round_number: int,
    review_record_id: uuid.UUID,
    reviewers_required: int,
) -> uuid.UUID | None:
    """`ER-004.4` — exactly one conflict per disagreement, in the same transaction.

    ## Why this function elevates, and how narrowly

    Conflict detection has to compare BOTH reviewers' dispositions, and it runs
    inside the transaction of the reviewer who just decided — whose session is
    blinded by L1 and can therefore see exactly one of the two rows. Without
    elevation the count never reaches `reviewers_required` and no conflict is
    ever created: the blinding would have blinded the system from itself.

    So the role GUC is raised to `lead` for the duration of ONE aggregate query
    and lowered again in a ``finally``. Three properties keep this from being a
    hole:

    * it is `SET LOCAL` (``set_config(..., true)``), so it dies with the
      transaction and cannot leak across a PgBouncer-pooled connection;
    * the rows never leave this function — only a count and a set of
      dispositions do, and neither is returned to the caller;
    * the caller's response says nothing about the conflict at all, so even the
      *existence* of a disagreement is not disclosed to the reviewer who
      triggered it.

    The alternative — a `SECURITY DEFINER` routine — does not work here:
    `FORCE ROW LEVEL SECURITY` applies the policy to the table owner too, so a
    definer function owned by the migration role is filtered by the same
    predicate, and exempting it would need a `BYPASSRLS` role that only a
    superuser can create.

    ## Why the FIRST statement is an advisory lock

    Everything below is a check-then-act — read every current decision on this
    record, compare them, insert a conflict — and the sessions that run it are
    READ COMMITTED, which is the Postgres default and is not overridden
    anywhere. So each statement sees a snapshot taken when THAT statement began,
    and an uncommitted decision from a concurrent reviewer is not in it.

    Two reviewers deciding the same record at the same instant therefore each
    counted ONE decision, each fell short of `reviewers_required`, and each
    returned `None`. Measured on `edc79c7`, `include` and `exclude` on a barrier:

        GET /conflicts: {"items": [], "count": 0, "open": 0, "total": 0, "total_open": 0}
        GET /flow:      {..."records_screened": 0, "not_yet_screened": 1...}
        POST /exports -> 422 "1 records are not yet screened"

    The same disagreement taken SEQUENTIALLY opens one conflict, so the race is
    the whole of the difference. `uq_conflict_once` cannot catch it: the unique
    key stops two INSERTs of the same conflict row, and here neither transaction
    ever reaches an INSERT.

    The state it leaves is recoverable, and saying otherwise would overstate it.
    Both decisions are written and `GET /decisions` shows both to the lead;
    either reviewer superseding their own decision re-runs this function against
    a snapshot that now contains both rows and opens the conflict. What it is
    not is self-correcting: nothing re-runs on its own, and until somebody acts
    the record is `not_yet_screened` with no `conflict_id` to resolve.

    So the loser BLOCKS here until the winner's transaction ends, and its next
    statement — the read below — then takes a fresh READ COMMITTED snapshot that
    contains both decisions. `pg_advisory_xact_lock`, at `(review, stage, round,
    record)` grain, for the reasons `_corpora_already_in_this_round` already
    writes out at length: there is no row at that grain to lock, `0011` revoked
    the `UPDATE, DELETE` that `SELECT ... FOR UPDATE` would need, and a
    transaction-scoped advisory lock is released by the commit or the rollback
    and so is safe behind PgBouncer's transaction pooling.

    Held for the rest of the caller's transaction rather than released after the
    read, which is not a lock held longer than needed: releasing it before the
    INSERT would put the check and the act back on either side of an unlocked
    gap, which is the defect.

    `INSERT ... ON CONFLICT DO NOTHING` on `uq_conflict_once` is kept behind the
    lock. It is no longer what makes this safe, but it is what keeps a lock-key
    collision, or any future caller that reaches the INSERT by another path,
    from turning into a `UniqueViolation` at commit.
    """
    from actor import ROLE_GUC

    session.execute(
        select(func.pg_advisory_xact_lock(_conflict_lock_key(review_id, stage_id, round_number, review_record_id)))
    )

    caller_role = str(actor.role)
    try:
        session.execute(
            text(f"SELECT set_config('{ROLE_GUC}', 'lead', true)")  # noqa: S608 - constant name
        )
        rows = [
            d
            for d in blinded_reads.unblinded_current_decisions(session, review_id=review_id, stage_id=stage_id)
            if d.round_number == round_number and d.review_record_id == review_record_id
        ]
    finally:
        session.execute(
            text(f"SELECT set_config('{ROLE_GUC}', :role, true)"),  # noqa: S608
            {"role": caller_role},
        )

    if len(rows) < reviewers_required:
        return None
    # `agreement_key`, and NOT `d.disposition`. Two reviewers who both `exclude`
    # a record for two DIFFERENT reasons are not in agreement about it, and
    # comparing dispositions alone said they were: no conflict opened, and the
    # ledger sealed whichever reason was recorded first. The helper lives in
    # `flow_derivation` because `derive` and `flow._unresolved_conflicts` have
    # to ask the identical question — a conflict this function opens that either
    # of those does not recognise is a record no lead can clear.
    #
    # Imported at call time, matching every other crossing in this pair: nothing
    # in `domain.policies` imports back into `application`, so there is no cycle
    # to fear here, but the deferred form is what the module already uses.
    from domain.policies.flow_derivation import agreement_key

    if len({agreement_key(d.disposition, d.exclusion_reason_code) for d in rows}) == 1:
        # Agreement opens no conflict (`01-…:484`).
        return None

    result = session.execute(
        pg_insert(ScreeningConflict)
        .values(
            conflict_id=uuid.uuid4(),
            review_id=review_id,
            tenant_id=actor.tenant_id,
            stage_id=stage_id,
            round_number=round_number,
            review_record_id=review_record_id,
        )
        .on_conflict_do_nothing(constraint="uq_conflict_once")
        .returning(ScreeningConflict.conflict_id)
    ).scalar_one_or_none()
    if result is not None:
        # Only THIS branch is a newly opened conflict. `on_conflict_do_nothing`
        # returns nothing when the row already existed, and the `existing`
        # lookup below is a re-read of a conflict that was announced when it was
        # first opened -- announcing it again would tell a consumer a second
        # conflict opened on the same record.
        events.emit_conflict_opened(
            session,
            result,
            review_id=review_id,
            tenant_id=actor.tenant_id,
            stage_id=stage_id,
            round_number=round_number,
        )
        return result
    existing = session.execute(
        select(ScreeningConflict.conflict_id).where(
            ScreeningConflict.review_id == review_id,
            ScreeningConflict.stage_id == stage_id,
            ScreeningConflict.round_number == round_number,
            ScreeningConflict.review_record_id == review_record_id,
        )
    ).scalar_one_or_none()
    return existing


def _is_unblinded(session: Session, conflict_id: uuid.UUID) -> bool:
    return (
        session.execute(
            select(ScreeningUnblindingEvent.unblinding_event_id)
            .where(ScreeningUnblindingEvent.conflict_id == conflict_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _chain_tail(rows: Sequence[ScreeningAdjudication]) -> ScreeningAdjudication | None:
    """Walk one conflict's supersession chain to its TAIL. Pure; no I/O.

    It STARTS at the head — the row with `supersedes_adjudication_id IS NULL` —
    and returns the row nothing supersedes. The docstring used to say "to its
    head", which names the wrong end of the exact confusion that made
    `supersedes_decision_id IS NULL` a defect on the decisions table (see
    `blinded_reads._is_current`). Adjudications never had that defect, precisely
    because this walks; only the sentence describing it was wrong.
    """
    if not rows:
        return None
    by_supersedes = {r.supersedes_adjudication_id: r for r in rows}
    node = by_supersedes.get(None)
    if node is None:
        return None
    while node.adjudication_id in by_supersedes:
        node = by_supersedes[node.adjudication_id]
    return node


def current_adjudication(session: Session, conflict_id: uuid.UUID) -> ScreeningAdjudication | None:
    """Tail of the adjudication supersession chain for one conflict."""
    rows = (
        session.execute(select(ScreeningAdjudication).where(ScreeningAdjudication.conflict_id == conflict_id))
        .scalars()
        .all()
    )
    return _chain_tail(rows)


def current_adjudications(
    session: Session, conflict_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ScreeningAdjudication]:
    """:func:`current_adjudication` for many conflicts, in ONE statement.

    The chain walk was already pure Python; only the fetch was per-conflict.
    """
    if not conflict_ids:
        return {}
    grouped: dict[uuid.UUID, list[ScreeningAdjudication]] = {}
    for row in (
        session.execute(select(ScreeningAdjudication).where(ScreeningAdjudication.conflict_id.in_(conflict_ids)))
        .scalars()
        .all()
    ):
        grouped.setdefault(row.conflict_id, []).append(row)
    tails = {cid: _chain_tail(rows) for cid, rows in grouped.items()}
    return {cid: tail for cid, tail in tails.items() if tail is not None}


def _unblinding_audiences(session: Session, review_id: uuid.UUID) -> dict[uuid.UUID, set[str]]:
    """:func:`_is_unblinded` for a whole review, WITH each event's audience, in ONE statement.

    This replaces a page-scoped `conflict_id IN (...)` existence read, and it
    replaces it for two reasons rather than one.

    It has to be REVIEW-WIDE because `list_conflicts` needs the audience before
    it knows what its page is: whether a conflict the caller owes appears at all
    now depends on whether that caller was named in an unblinding, so the
    question is asked of every conflict in the review, not of the page's slice.
    The same argument the `all_conflicts` read already makes.

    And it has to carry `disclosed_to_subject_ids` because "an unblinding exists"
    is not the fact anything here turns on. `0008` made the audience explicit
    precisely so that widening blinding is something somebody chose; a lead who
    unblinds a conflict to THEMSELVES has authorised nothing about a second lead
    who is still screening the record. Keying the carve-out on the existence of
    any event would let one lead's unblinding disclose the conflict to a
    different lead who owes a verdict on it — the leak, reintroduced by a third
    party.

    The statement count is unchanged: one read here for one read before.
    """
    rows = session.execute(
        select(ScreeningUnblindingEvent.conflict_id, ScreeningUnblindingEvent.disclosed_to_subject_ids).where(
            ScreeningUnblindingEvent.review_id == review_id
        )
    ).all()
    audiences: dict[uuid.UUID, set[str]] = {}
    for conflict_id, disclosed_to in rows:
        # UNION across events, not last-write-wins. Unblinding is append-only and
        # a second `:unblind` naming one more person does not revoke the first.
        audiences.setdefault(conflict_id, set()).update(disclosed_to or ())
    return audiences


def list_conflicts(session: Session, actor: Actor, review_id: uuid.UUID, *, limit: int = 200) -> dict[str, Any]:
    """**Lead only.** Conflicts, with dispositions redacted until an unblinding.

    ## Why the existence of a conflict is itself blinded

    This used to be open to any reviewer in the tenant, justified by "both
    reviewers already know they disagreed". That sentence was never true of the
    thing it was authorising, in two separate ways.

    It is not true of the SECOND reviewer at the moment they decide. A conflict
    opens inside `record_decision`, and the response deliberately says nothing
    about it — the comment there spells out why: "a conflict opened tells the
    second reviewer that the first one disagreed — which, from a binary
    disposition, IS the first reviewer's disposition". Stripping that field from
    one response and then serving the same fact from `GET /conflicts` one URL
    later is not blinding, it is a locked front door next to an open side one.
    Measured, before the fix: reviewer A decides `include`, polls `/conflicts`
    and sees `count: 0`; reviewer B decides `exclude`; A polls again and sees a
    conflict naming the record and its title. A has just read B's disposition.

    And it is not true AT ALL of a third party. `reviewers_required` may be up to
    ten, and any reviewer in the tenant could call this: reviewer C, who has
    screened nothing, was served A-vs-B's conflict, the record identifier and the
    record TITLE. There is no sense in which C "already knew".

    The count was a progress meter on top of that. It is monotone in the other
    reviewers' completed work — one conflict after one disagreement, four after
    four — which is exactly the aggregate leak `ER-004.2` names for counts and
    the reason `/flow` is lead-only. A reviewer could sit on this endpoint and
    watch their partner work.

    ## Why lead-only rather than a per-participant filter

    Filtering to conflicts the caller took part in would still answer "did
    somebody disagree with me on this record", which is the disposition itself.
    The only reading that leaks nothing is one restricted to the actor who is
    allowed to see across reviewers anyway — the same gate `/flow`, the export
    routes, `:unblind` and `:resolve` already use. The single consumer is the
    lead's adjudication panel (`fr`'s `ScreeningPanel.svelte` renders this list
    solely to drive "Unblind & adjudicate", which is lead-only regardless), so
    nothing legitimate loses a view it was using.

    The redaction below stays, and is now the LEAD's gate: holding lead authority
    is not the same as having opened the governed door, and `who said what` still
    waits for a recorded unblinding.

    ## A LEAD WHO ALSO SCREENS IS NOT TOLD THE CONFLICT EXISTS

    `visible_decisions`, and the two field-level gates below it, withhold
    `dispositions` and `adjudication` at a position the caller still owes a
    verdict on. That was necessary and it was NOT sufficient, and the reason is
    visible without leaving this function: the row was still emitted, and it
    carries `review_record_id`, `title`, `stage_id`, `round_number`, `opened_at`
    and `resolved`. Those five say *this record is disputed* — which is the whole
    of the leak, because a conflict opens only when the current decisions
    disagree. At `reviewers_required = 2` a lead assigned to that record reads
    one other reviewer's disposition off it: the disagreement is with them, so
    the other vote is the negation of the one they are about to cast.

    The counters leak it a second time, and this one needs no row at all. `total`,
    `total_open` and `open` moved `0 -> 1` the instant the second reviewer
    disagreed, so a lead-who-screens polling this route watches their partner's
    verdict arrive as an integer. That is the same aggregate leak the section
    above refuses for a plain reviewer — "a reviewer could sit on this endpoint
    and watch their partner work" — reached by a caller who holds the role the
    endpoint is gated on. Redacting every remaining FIELD cannot close it: the
    number is not in a field.

    So the conflict is dropped from `items` and from every counter for as long as
    the caller owes a decision at its position, and reinstated the moment they
    record one. The write side already agreed with this and was the only half
    enforced: `:resolve` refuses a lead who owes the position with `409`
    (`tests/security/test_one_stage_decided_is_not_every_stage_decided.py::
    test_a_lead_who_owes_the_stage_cannot_adjudicate_it`), so nothing is being
    withheld that this caller could have acted on.

    ## THE ONE CARVE-OUT: AN UNBLINDING THAT NAMES THE CALLER

    Hiding is the default, not the whole rule. A conflict the caller owes but was
    NAMED IN an unblinding's `disclosed_to_subject_ids` is served — as a row and
    in every counter — with `dispositions` and `adjudication` still `redacted`.

    An earlier draft of this change hid those too, and RETRACTED
    `test_an_unblinded_conflict_the_caller_owes_reads_redacted_not_empty` on the
    grounds that it encoded the superseded field-level rule. It does not. That
    test's rule is "an empty list is not a redaction", and an ABSENT ROW is the
    same confusion one step further along: a lead who records an `:unblind` — an
    audited authorisation, with a reason, by someone holding lead authority over
    this review — and then finds nothing in their panel cannot tell "you are
    still blinded to the contents" from "the unblinding failed". That is a defect
    of its own, and it makes `:unblind` a control with no observable effect.

    The two rules are simultaneously true because they govern different things.
    The debt governs the CONTENTS: what each reviewer said, and how a lead ruled.
    The unblinding governs whether the caller may be told the conflict is THERE.
    So the carve-out restores the row and leaves both fields `redacted`, and
    `owes_this_conflict` below is deliberately untouched.

    It is keyed on the AUDIENCE and never on "an unblinding exists". A lead who
    unblinds a conflict to themselves has authorised nothing about a second lead
    who is still screening that record, and letting their event open the row for
    that second lead would reintroduce the leak through a third party.

    `_conflicted_at_full_text` — the helper five tests in that file read the owed
    `conflict_id` out of the LEAD's own panel with — does have to move: the
    scenario it builds is deliberately a lead who has NOT yet reported at full
    text and has no unblinding, so the id is no longer theirs to read. It reads
    it from a lead who owes nothing instead. `REDACTED` is unchanged everywhere
    and still means what it meant.

    ## `owed_hidden`, and what it does and does not disclose

    Withholding silently would make the panel lie in the other direction: `fr`
    compares `total_open` against `items.filter((c) => !c.resolved).length` to
    tell the reader the page under-represents the review, and a lead whose
    counters have been quietly reduced has no way to tell "no disagreements" from
    "disagreements you may not be told about". So the count of withheld conflicts
    is reported as `owed_hidden`, review-wide, alongside the review-wide `total`
    it was subtracted from: `total + owed_hidden` is every conflict the review
    holds.

    It is a CARDINALITY and never an identity — no record, no stage, no title, no
    timestamp — and it is a fact about work the caller has been assigned and has
    not done. The residual is real and worth stating rather than glossing: a
    caller who owes exactly ONE position and reads `owed_hidden: 1` has located
    the disputed record, because there is only one candidate. That is strictly
    less than the field-level rule disclosed (which named the record whatever the
    caller owed), it shrinks as the caller's outstanding queue grows, and it is
    the price of not lying to the lead about the completeness of their own panel.
    Suppressing the marker as well would buy that last case at the cost of a
    counter no reader can reconcile against the export gate.
    """
    _require_review_lead(session, actor, review_id, "listing conflicts")

    # READ FIRST, because it no longer decides only which FIELDS are redacted —
    # it decides which conflicts exist for this caller at all. A lead who is
    # ALSO an assigned reviewer is a reviewer, on the records they have not
    # decided. See `blinded_reads.visible_decisions`.
    still_owed = blinded_reads.records_the_caller_still_owes(session, review_id=review_id, actor=actor)

    # EVERY CONFLICT ROW, IN ONE STATEMENT, and then paged in Python.
    #
    # This was `ORDER BY opened_at LIMIT limit + 1`, which bounded the read as
    # well as the response. Dropping the SQL bound adds no unbounded read to this
    # request: `_unresolved_conflicts`, called unconditionally 20 lines below,
    # already issues `select(ScreeningConflict).where(review_id == ...)` with no
    # limit at all and materialises every row of the review as ORM objects
    # (`flow.py`, in `_collect`). The `limit + 1` was therefore bounding one of
    # two reads of the same table in the same transaction, and the *response* —
    # which is what `limit` is for — is still bounded, below, by the slice.
    #
    # It has to be the whole set rather than a page, because `total`, `total_open`
    # and `owed_hidden` are REVIEW-WIDE and the owed-position filter is a property
    # of each conflict's `(stage, round, record)`. A page cannot answer "how many
    # of the review's conflicts are being withheld from you" and a `NOT IN` over
    # `still_owed` cannot be pushed into SQL at a sane cost: that set is one tuple
    # per assignment the caller has not discharged, which on a first pass is the
    # whole corpus.
    #
    # The statement count is unchanged and still constant in the number of
    # conflicts — one full read here replaces one page read plus one `COUNT(*)`.
    # `test_read_screen_throughput.py::
    # test_listing_conflicts_does_not_issue_a_statement_set_per_conflict` is the
    # assertion that says so, and its size-invariance half is the one that
    # matters.
    all_conflicts = list(
        session.execute(
            select(ScreeningConflict)
            .where(ScreeningConflict.review_id == review_id)
            .order_by(ScreeningConflict.opened_at)
        )
        .scalars()
        .all()
    )
    # Read for the WHOLE review rather than the page, because it is now an input
    # to which conflicts the page contains. See `_unblinding_audiences`.
    audiences = _unblinding_audiences(session, review_id)
    #: THE CARVE-OUT. Conflicts this caller was NAMED IN an unblinding for.
    #:
    #: An `:unblind` naming the caller is a recorded, audited authorisation by a
    #: review lead, with a `reason`, and `#51`'s write side is built on it. Hiding
    #: the row afterwards would make that authorisation a NO-OP the lead cannot
    #: distinguish from a failure: they open the door, the panel still says
    #: nothing is there, and the only reading available to them is that the
    #: unblinding did not take. That is a defect of its own, and it is the one
    #: `test_an_unblinded_conflict_the_caller_owes_reads_redacted_not_empty`
    #: names — an empty list is not a redaction, and neither is an absent row.
    #:
    #: The membership test is the AUDIENCE, never the mere existence of an event:
    #: see `_unblinding_audiences` for why a third party's unblinding must not
    #: open this. `actor.subject_id` is the same value `bind_actor` binds the
    #: RLS GUC to, so this agrees with L1 rather than second-guessing it.
    unblinded_to_caller = {
        cid for cid, audience in audiences.items() if actor.subject_id and actor.subject_id in audience
    }
    # THE ONE PLACE THE OWED FILTER IS APPLIED. Everything downstream — the page,
    # `total`, `total_open`, `has_more` and the per-item fields — is computed from
    # `visible`, so a counter cannot move over a row the caller cannot see.
    #
    # The carve-out restores the ROW, and nothing else: `owes_this_conflict`
    # below is unchanged, so a conflict readmitted here still renders
    # `dispositions: "redacted"` and `adjudication: "redacted"`. The debt still
    # withholds the CONTENTS; the unblinding only settles whether the caller may
    # be told the conflict is there. Both rules hold at once.
    visible = [
        c
        for c in all_conflicts
        if (c.stage_id, c.round_number, c.review_record_id) not in still_owed or c.conflict_id in unblinded_to_caller
    ]
    #: Cardinality only. See the docstring for what it deliberately does not say.
    owed_hidden = len(all_conflicts) - len(visible)

    total_conflicts = len(visible)
    # The slice, and the truncation flag, over the VISIBLE list. Taken after the
    # filter rather than before it, so `has_more` cannot report "there is more"
    # about rows that do not exist for this caller — the mirror of the defect
    # `screening_queue` fixed by moving its merge filter to the other side of
    # `LIMIT`. Unlike the queue this route has no cursor, so a shrunken page
    # strands nothing: the only knob is `limit`, and it is applied to a list this
    # caller may see in full.
    has_more = len(visible) > limit
    conflicts = visible[:limit]

    # THE SAME RULE THE EXPORT GATE USES, not a second one.
    #
    # This was `len(all) - len(adjudicated)`, which counts a conflict as open
    # unless a lead has ruled on it — and misses the other way a disagreement
    # ends. When one reviewer supersedes their own decision and the two agree,
    # `flow._unresolved_conflicts` treats the conflict as settled, so the export
    # seals and (since the close gate) `:close` succeeds, while this panel still
    # reported it open. Measured: `/conflicts total=1 open=1` beside
    # `POST /exports -> 201`. The lead's screen showed an outstanding
    # disagreement with nothing left to do about it.
    #
    # Imported inside the function, the same shape `flow` uses to reach
    # `current_adjudications` here and `reviews.transition` uses to reach this
    # helper: the modules import each other only at call time, so there is no
    # cycle at import. The earlier comment said borrowing it "would close an
    # import cycle" and preferred a local count; the count was the bug, and the
    # deferred import is what every other crossing in this pair already does.
    #
    # `_unresolved_conflict_ids` — the UNFILTERED primitive, not the actor-aware
    # `_unresolved_conflicts` wrapper `create_export` and `reviews.transition`
    # use to keep an id out of a response the caller owes a decision at. This
    # site never puts an id in the response either way, only `total_open`
    # below, and it needs full review-wide membership to test against `visible`
    # — a row set already filtered by THIS function's own, broader rule
    # (`still_owed` plus the `:unblind` audience carve-out a few lines up). The
    # wrapper's narrower owed-only filter would wrongly drop a carved-out row
    # from this membership test, undercounting `total_open` for a conflict the
    # lead was explicitly authorised to see.
    from application.services.flow import _unresolved_conflict_ids

    unresolved = set(_unresolved_conflict_ids(session, review_id))
    # REVIEW-WIDE, AND OVER `visible`. `len(unresolved)` counted the whole
    # review, including the conflicts this caller is not being shown, which is
    # the counter that moved `0 -> 1` on their partner's verdict. Intersected
    # rather than recomputed, so this is still the export gate's definition of
    # outstanding and not a second one — the filter is about the CALLER, the
    # predicate is unchanged.
    total_open = len([c for c in visible if str(c.conflict_id) in unresolved])

    # Three batched reads for the whole list, not three per conflict. This is the
    # lead's adjudication panel and it had NO `limit` at all, so the old
    # `1 + 3C..4C` was unbounded in the number of conflicts.
    conflict_ids = [c.conflict_id for c in conflicts]
    adjudications = current_adjudications(session, conflict_ids)
    versions = current_record_versions(session, review_id, [c.review_record_id for c in conflicts])

    items: list[dict[str, Any]] = []
    for conflict in conflicts:
        # UNCHANGED MEANING: "has this conflict been unblinded at all". The field
        # reports the conflict's governance state, which is what `fr`'s panel
        # renders "Unblind & adjudicate" off; who may read WHAT is the audience,
        # and that is `unblinded_to_caller` above and the RLS policy below it.
        unblinded = conflict.conflict_id in audiences
        # AN EMPTY LIST IS NOT A REDACTION.
        #
        # `visible_decisions` removes other reviewers' rows at a position the
        # caller still owes, whatever their role and whatever the unblinding
        # says — that is the rule L1 cannot express. Unblinded AND owed therefore
        # rendered as `[]`, which is exactly the confusion the `REDACTED` literal
        # two lines down exists to prevent: a client cannot tell "you may not see
        # this" from "there is nothing here". Measured: a lead who unblinded a
        # full-text conflict while still owing their own full-text verdict was
        # served `dispositions: []` for a disagreement between two reviewers.
        #
        # `REDACTED` rather than a new marker: the reason differs, the fact the
        # caller is told does not, and a third literal is a value the studio has
        # never heard of.
        #
        # ALWAYS FALSE SINCE THE OWED FILTER ABOVE, and kept deliberately. Every
        # row in `conflicts` came out of `visible`, so no owed position can reach
        # here — but this is the field-level half of a two-layer rule, and it is
        # the layer that does not depend on the paging above staying correct. If
        # a future caller reaches this loop by another path, or the filter is
        # narrowed, the fields fall back to `redacted` instead of falling open.
        # Same argument as the `ON CONFLICT DO NOTHING` kept behind the advisory
        # lock in `_maybe_open_conflict`: no longer what makes it safe, still what
        # stops a second route from being unsafe.
        owes_this_conflict = (conflict.stage_id, conflict.round_number, conflict.review_record_id) in still_owed
        adjudication = adjudications.get(conflict.conflict_id)
        version = versions.get(conflict.review_record_id)
        items.append(
            {
                "conflict_id": str(conflict.conflict_id),
                "review_record_id": str(conflict.review_record_id),
                "stage_id": str(conflict.stage_id),
                "round_number": conflict.round_number,
                "title": version.title if version else None,
                "opened_at": conflict.opened_at.isoformat() if conflict.opened_at else None,
                "unblinded": unblinded,
                # `redacted`, not an omitted key: a client must be able to tell
                # "you may not see this" from "there is nothing here".
                "dispositions": (
                    [
                        d.to_public()
                        for d in blinded_reads.visible_decisions(
                            session,
                            review_id=review_id,
                            actor=actor,
                            stage_id=conflict.stage_id,
                            round_number=conflict.round_number,
                            review_record_id=conflict.review_record_id,
                            # Computed ONCE above. Letting each call recompute it
                            # would put two statements back per unblinded
                            # conflict, which is the shape this route was just
                            # flattened out of.
                            still_owed=still_owed,
                        )
                    ]
                    if unblinded and not owes_this_conflict
                    else REDACTED
                ),
                # NOT OUTSTANDING — from the same set `total_open` is counted
                # from, so a row cannot render as open while the header says none
                # are. `fr`'s panel compares the two directly
                # (`total_open > conflicts.filter((c) => !c.resolved).length`) to
                # tell the reader that the page under-represents the review, so
                # they have to move together or that banner lies.
                #
                # This was `adjudication is not None`, which is a different
                # question: HOW it stopped being outstanding. That answer is
                # still here, in `adjudication` — non-null only when a lead
                # ruled, null when the reviewers simply came to agree. A consensus
                # -settled conflict used to render with an "Unblind & adjudicate"
                # prompt for a disagreement that no longer existed.
                "resolved": str(conflict.conflict_id) not in unresolved,
                # THE SAME PREDICATE AS `dispositions`, and it was missed.
                #
                # `_adjudication_summary` carries `disposition`,
                # `exclusion_reason_code`, `rationale` and
                # `adjudicator_subject_id` — the settled outcome in full. It was
                # rendered whenever an adjudication existed, two lines below the
                # redaction that exists to stop exactly this. Measured: a lead who
                # is an assigned reviewer at `(ft, r1, rec)`, has recorded
                # nothing there, and is NOT in the unblinding's
                # `disclosed_to_subject_ids`, read
                #
                #     "dispositions": "redacted",
                #     "adjudication": {"disposition": "exclude",
                #                      "exclusion_reason_code": "wrong_design",
                #                      "rationale": "reviewer B was right", ...}
                #
                # while `GET /decisions` returned `count: 0` and their queue said
                # `my_decision: null`. The blinded door was shut and the window
                # beside it was open.
                #
                # Gated on `owes_this_conflict` ALONE, deliberately not on
                # `unblinded`. Unblinding governs who may see WHICH REVIEWER said
                # what; a lead's ruling is not a reviewer attribution, and other
                # leads need it for the audit trail — `#51`'s control asserts a
                # non-screening lead can still read it. The rule here is the
                # caller property: you may not be told the outcome at a position
                # your own verdict is still outstanding at.
                #
                # `REDACTED` and not `None`: `None` already means "no lead has
                # ruled", and collapsing the two would tell an owing reviewer
                # that the disagreement is unsettled when it is settled against
                # them. `fr` reads this field nowhere today (`Conflict.adjudication`
                # is declared in `reviews.ts` and has zero read sites), so the
                # widened type breaks no consumer.
                #
                # `resolved` above is deliberately NOT redacted. It says a
                # disagreement was settled, never which way, and the existence of
                # the conflict is already disclosed to this caller; `fr`'s
                # truncation banner compares it against `total_open` and would
                # misreport without it.
                "adjudication": (
                    _adjudication_summary(adjudication)
                    if adjudication and not owes_this_conflict
                    else (REDACTED if adjudication else None)
                ),
            }
        )
    return {
        "items": items,
        # `count` and `open` describe THIS PAGE, and `has_more` is what stops
        # them being read as the whole review. The `limit` shipped in `13b315f`
        # without either: measured on six conflicts at `limit=2`, the panel read
        # `{"count": 2, "open": 2}`, the lead resolved those two, and it then
        # read `{"open": 0}` while `POST /exports` refused with 422 —
        # `_unresolved_conflicts` is unbounded, so the export gate was right and
        # the panel was lying, with nothing on screen to explain it.
        #
        # That is the same silent-truncation class `925cacf` removed from the
        # export, reintroduced one route over. `total` is exact rather than a
        # flag alone, because "there are more" does not tell a lead HOW MANY are
        # unresolved, which is the number the export gate turns on.
        "count": len(items),
        "open": len([i for i in items if not i["resolved"]]),
        "has_more": has_more,
        # REVIEW-WIDE, AND CALLER-SCOPED SINCE THE OWED FILTER. Every one of the
        # four counters above and below now describes the conflicts this caller
        # may be told about, and nothing else. That is what stops `total_open`
        # from being a live readout of the other reviewers' verdicts to a lead who
        # is still screening — redacting the fields left the integer, and the
        # integer was the disclosure.
        "total": total_conflicts,
        "total_open": total_open,
        #: HOW MANY CONFLICTS ARE BEING WITHHELD FROM THIS CALLER, and nothing
        #: about which. `0` for a lead who is not screening — the ordinary case,
        #: and the one every existing consumer sees. `total + owed_hidden` is the
        #: number of conflicts the review holds, which is what lets a lead
        #: reconcile this panel against a `POST /exports` that refuses on
        #: conflicts they cannot see. Without it the withholding is silent and
        #: "no open disagreements" is indistinguishable from "none you may know
        #: about" — see the docstring for the residual this deliberately accepts.
        "owed_hidden": owed_hidden,
    }


def unblind_conflict(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    reason: str,
    disclose_to: list[str] | None = None,
) -> dict[str, Any]:
    """The ONLY door through L1's third clause — and it leaves a row.

    ## The audience is part of the act (`ER-004.2`, `0008`)

    The clause used to key on the CONFLICT alone, so one unblinding opened both
    dispositions to every reviewer in the tenant. Measured over HTTP: a third
    reviewer assigned to the same record and still pending read
    `{"items": [], "code": 0}` before and both dispositions after, without having
    formed an opinion of their own. That is a blinding break.

    `disclose_to` is the lead naming who the disclosure is for. It is stored on
    the event and read by the policy, so it is not a note ABOUT the decision —
    it is the decision, attributable to the person who took it, which is what an
    HTA audit trail has to be able to show.

    The unblinding actor is always in the audience. Adjudication needs to read
    what it is adjudicating, and a lead's own panel refusing them the rows they
    just opened would be a trap rather than a safeguard.

    The DEFAULT is the actor alone. Not the two conflicting reviewers: they are a
    reasonable audience for a panel discussion and a bad default, because
    widening blinding must be something somebody chose. A lead who wants them in
    names them.
    """
    subject = _require_review_lead(session, actor, review_id, "unblinding a conflict")
    require_review_open(session, review_id, "unblinding a conflict")
    conflict = session.get(ScreeningConflict, conflict_id)
    if conflict is None or conflict.review_id != review_id:
        raise NotFoundError("no such conflict for this review")

    # YOU MAY NOT UNBLIND WHAT YOU MAY NOT SEE — the same rule `adjudicate`
    # enforces below, and it was missing here (`ERS #65`, finding 1).
    #
    # `disclose_to` defaults to the caller alone (see the docstring below), so
    # with no guard at all a reviewer this conflict is currently withholding
    # decisions FROM could name themselves as the audience and read it: a
    # 200 from THIS call is what makes `list_conflicts`' owed-position filter a
    # no-op, because `unblinded_to_caller` there readmits any conflict naming
    # the caller regardless of whether they still owe it. Measured over HTTP
    # (`ERS #65`): a lead with `owed_hidden: 1` on `GET /conflicts` calls
    # `POST /exports`, reads the withheld `conflict_id` off its `422` (the
    # companion leak fixed at `flow.create_export`), self-`:unblind`s it, and
    # `GET /conflicts` then serves the row — `review_record_id`, `title`,
    # `total_open: 1`, `owed_hidden: 0` — to a lead who has cast no verdict of
    # their own at that position.
    #
    # `records_the_caller_still_owes` — not a second, ad-hoc check — is the
    # same set `adjudicate` and `list_conflicts` already gate on, so a position
    # cannot be "owed" for one write and "clear" for this one. `StateConflictError`
    # / `409`, mirroring `adjudicate`'s shape exactly: this is the same
    # underlying rule ("you may not act on a disagreement you have not voted
    # in yourself") applied one step earlier, before the contents are even
    # opened rather than only at the ruling. Recording the caller's own
    # decision clears this the same way it clears `adjudicate`'s refusal.
    if (
        conflict.stage_id,
        conflict.round_number,
        conflict.review_record_id,
    ) in blinded_reads.records_the_caller_still_owes(session, review_id=review_id, actor=actor):
        raise StateConflictError(
            "you are assigned to this record at this stage and have not recorded your own decision, so "
            "unblinding it would let you read the dispositions it is currently withholding from you. "
            "Record your own decision first, or ask a lead who is not screening this record to unblind it."
        )

    # STRIPPED AND REFUSED, not merely trimmed. `bind_actor` binds
    # `app.actor_subject_id` to `actor.subject_id or ""`, so a `""` in the
    # audience matches every caller presenting no subject at all — and `0008`'s
    # `ck_unblinding_audience_not_empty` bounds the list's cardinality, not the
    # emptiness of its elements. The request model refuses it too; this is the
    # half that holds for any caller reaching the service directly.
    named = [s.strip() for s in (disclose_to or [])]
    if any(not s for s in named):
        raise UnprocessableStateError(
            "a disclosure audience cannot contain an empty subject id: an empty string matches every "
            "caller that presents no subject, which discloses the record to anyone in the tenant",
            rejected=[s for s in (disclose_to or []) if not s.strip()],
        )
    audience = sorted({subject, *named})

    event = ScreeningUnblindingEvent(
        unblinding_event_id=uuid.uuid4(),
        conflict_id=conflict_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        actor_subject_id=subject,
        reason=reason,
        disclosed_to_subject_ids=audience,
    )
    session.add(event)
    session.flush()
    return {
        "unblinding_event_id": str(event.unblinding_event_id),
        "conflict_id": str(conflict_id),
        "actor_subject_id": subject,
        "reason": reason,
        "disclosed_to_subject_ids": audience,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
    }


def adjudicate(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    conflict_id: uuid.UUID,
    *,
    disposition: str,
    rationale: str,
    exclusion_reason_code: str | None = None,
    supersedes_adjudication_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """`ER-004.5` — attributable adjudication under separation of duty.

    The separation-of-duty check is a database trigger, not a check here: a lead
    who took part in the conflict is refused even if this function is bypassed.
    """
    subject = _require_review_lead(session, actor, review_id, "adjudicating a conflict")
    require_review_open(session, review_id, "adjudicating a conflict")
    conflict = session.get(ScreeningConflict, conflict_id)
    if conflict is None or conflict.review_id != review_id:
        raise NotFoundError("no such conflict for this review")

    # YOU MAY NOT RULE ON WHAT YOU MAY NOT SEE.
    #
    # `visible_decisions` withholds other reviewers' rows at a position the
    # caller still owes — the rule L1 cannot express, and the reason a screening
    # lead is a reviewer for as long as their own verdict is outstanding. Nothing
    # applied that to the WRITE. Measured: a lead assigned to a full-text record
    # and having decided nothing there unblinded the conflict, was served
    # `dispositions: []`, and `:resolve` still answered 200. They ruled blind, and
    # their ruling is what the ledger settles the record on.
    #
    # The separation-of-duty trigger does not cover this: it refuses a lead who
    # VOTED in the conflict, and this caller has not voted at all — which is
    # precisely the state that makes the ruling uninformed rather than partial.
    #
    # BOTH DOORS ARE CLOSED FOR A SCREENING LEAD, and that is the design rather
    # than an oversight. Deciding first does not help: the separation-of-duty
    # trigger then refuses them as "an adjudicator who recorded a decision in
    # this conflict". So a lead assigned to a record cannot rule on a
    # disagreement about it, blind OR informed. The two exits are a lead who is
    # not screening that record, and consensus — one reviewer superseding their
    # own decision until the votes agree, which needs no lead at all and is why
    # `_unresolved_conflicts` has a consensus arm.
    #
    # A lead who is not screening the record owes nothing and is unaffected,
    # which is the ordinary adjudication path.
    if (
        conflict.stage_id,
        conflict.round_number,
        conflict.review_record_id,
    ) in blinded_reads.records_the_caller_still_owes(session, review_id=review_id, actor=actor):
        raise StateConflictError(
            "you are assigned to this record at this stage and have not recorded your own decision, so "
            "the dispositions you would be ruling on are withheld from you. Recording one does NOT open "
            "this door — separation of duty then refuses you as an adjudicator who took part. Either a "
            "lead who is not screening this record adjudicates it, or the reviewers reach consensus by "
            "superseding their own decisions, which needs no lead at all."
        )

    protocol = active_protocol(session, review_id)
    if disposition == "exclude" and protocol is not None:
        vocabulary = list(protocol.exclusion_reason_vocabulary or [])
        if exclusion_reason_code not in vocabulary:
            raise UnprocessableStateError(
                "exclusion reason is not in the locked protocol's closed vocabulary", allowed=vocabulary
            )

    existing = current_adjudication(session, conflict_id)
    if existing is not None and supersedes_adjudication_id is None:
        raise StateConflictError(
            "this conflict is already adjudicated; a revision must supersede the previous adjudication",
            current_adjudication_id=str(existing.adjudication_id),
        )
    if supersedes_adjudication_id is not None and (
        existing is None or existing.adjudication_id != supersedes_adjudication_id
    ):
        raise StateConflictError("the adjudication being superseded is not the current one")

    adjudication = ScreeningAdjudication(
        adjudication_id=uuid.uuid4(),
        conflict_id=conflict_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        disposition=disposition,
        exclusion_reason_code=exclusion_reason_code,
        rationale=rationale,
        adjudicator_subject_id=subject,
        adjudicator_display_name=actor.display_name,
        supersedes_adjudication_id=supersedes_adjudication_id,
    )
    session.add(adjudication)
    session.flush()
    events.emit_conflict_resolved(session, adjudication)
    return _adjudication_summary(adjudication)


def _adjudication_summary(adjudication: ScreeningAdjudication) -> dict[str, Any]:
    return {
        "adjudication_id": str(adjudication.adjudication_id),
        "conflict_id": str(adjudication.conflict_id),
        "disposition": adjudication.disposition,
        "exclusion_reason_code": adjudication.exclusion_reason_code,
        "rationale": adjudication.rationale,
        "adjudicator_subject_id": adjudication.adjudicator_subject_id,
        # Always present, `None` when unknown. A key that appears only sometimes
        # makes a reader branch on absence as though it meant something; here it
        # would only ever mean "this row predates the column", which is not a
        # fact the caller should have to infer from a missing key.
        #
        # This sits in `_adjudication_summary` and NOWHERE else, so it inherits
        # the redaction this payload already has. It must never be added to
        # `blinded_reads.to_public()`: reviewer attribution there is guarded by
        # role alone, and a reviewer's NAME on that path is a blinding widening.
        "adjudicator_display_name": adjudication.adjudicator_display_name,
        "supersedes_adjudication_id": (
            str(adjudication.supersedes_adjudication_id) if adjudication.supersedes_adjudication_id else None
        ),
        "decided_at": adjudication.decided_at.isoformat() if adjudication.decided_at else None,
    }
