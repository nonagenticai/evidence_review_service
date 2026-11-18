"""Reading the flow inputs out of the database, and the sealed export — `ER-005`, `ER-009`.

`derive` is pure; this module is the part that decides what to feed it. The two
are separated so the conservation property can be tested on generated histories
without a database.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from actor import ROLE_GUC, Actor
from api.problem_details import ForbiddenError, NotFoundError, UnprocessableStateError
from application.services import blinded_reads
from application.services.deduplication import cluster_members, current_resolutions
from application.services.imports import corpus_member_versions, latest_corpus
from application.services.reviews import (
    active_protocol,
    current_state,
    load_review,
    require_review_lead,
    require_review_lead_for,
)
from config import get_settings
from domain.policies.deduplication_engine import MergedGroup, components_without_survivor
from domain.policies.flow_derivation import (
    UNATTRIBUTABLE,
    ConservationError,
    FlowInputs,
    RecordDisposition,
    StageDescriptor,
    StagePosition,
    agreement_key,
    canonical_counts_hash,
    derive,
    settling_position,
)
from log_config import get_logger
from models.records import CorpusVersionMember, ImportRow, ReviewRecordVersion
from models.reviews import ReviewStage
from models.screening import (
    DuplicateCluster,
    FlowProjection,
    FlowRevision,
    InclusionExport,
    ScreeningConflict,
)

logger = get_logger(__name__)

#: The `OUT-001` contract shape. Frozen here as the export's `schema_ref`; the
#: seal is computed against it so a consumer can verify without trusting us.
#:
#: **This used to be an `https://` URL, and that could never have worked** — not
#: "did not work because the document was missing", which is how it read. The
#: released hash profile is derived by validating a manifest whose own
#: `schema_ref` member is the stable-identity grammar
#: `^urn:datacompany:[a-z][a-z0-9-]*:[1-9][0-9]*$`, so `derive_hash_profile` refused
#: the URL on its SHAPE, before any question of whether a document existed. That
#: is exactly why the defect was invisible: it presented as one more resolution
#: failure, indistinguishable from "the schema has not been published yet", and
#: `_seal_manifest`'s `except Exception` turned both into a silent bare hash. The
#: URL was also refused a second time over, inside the instance: this manifest
#: carries its own `schema_ref` as a sealed member, `$ref`-ed to the same
#: grammar, so a URL-shaped value fails structural validation of the document it
#: is meant to identify.
#:
#: `urn:datacompany:inclusion-set-export:1` is registered in `research-contracts`
#: 0.2.0 — hence the floor in `pyproject.toml`, which is not a routine bump but
#: the thing that makes this constant mean anything.
#:
#: **:2 since `research-contracts` 0.3.0, and the floor moved with it.** `:1`
#: sealed the manifest and left the item arrays outside it, attested only by
#: `payload_digest` — an ordinary column of this table that nothing signs. The
#: consequence was pinned HERE, as a passing test, before the contract existed
#: to fix it: rewrite a record in the stored payload, recompute
#: `payload_digest`, leave the count alone, and every check reproduced.
#: `:2` adds `items_hash` INSIDE the manifest, so the item arrays are covered by
#: the same seal the envelope is.
#:
#: `:1` is deliberately NOT retired in `research-contracts`, and this constant is
#: the only thing that moved: :func:`verify_export` resolves each row's OWN
#: `schema_ref`, so rows sealed under `:1` stay verifiable as exactly what they
#: were — an envelope seal, and a `null` where `:2` reports an items check.
EXPORT_SCHEMA_REF = "urn:datacompany:inclusion-set-export:2"

#: The schema every export written before this change declares. Kept as a named
#: constant rather than spelled inline at its one use in :func:`verify_export`,
#: because "this row predates the item seal" is a fact about the contract and
#: not a string comparison anyone should have to recognise.
EXPORT_SCHEMA_REF_V1 = "urn:datacompany:inclusion-set-export:1"
#: The released document a flow-ledger body validates against.
#:
#: THIS WAS A URL, AND THE SWITCH IS THE ORDER THIS CONSTANT ITSELF PRESCRIBED.
#: Its previous docstring argued — correctly — that a URL was the honest spelling
#: while no such document existed: a `urn:datacompany:*` is the shape a consumer
#: resolves, so minting one for a document the registry did not have would have
#: been a worse claim than an unresolvable URL, not a better one. It ended:
#: "when that changes, the schema gets authored first and this constant follows
#: it — never the other way round, which is the mistake `EXPORT_SCHEMA_REF` spent
#: its whole life demonstrating."
#:
#: That is what happened. `urn:datacompany:review-flow-ledger:1` is released in
#: `research-contracts` 0.4.0 — authored FROM this module's own output, not from
#: a specification: its fixture is `flow_derivation.derive` over a hand-built
#: `FlowInputs`, so the conservation identities hold on it. The floor in
#: `pyproject.toml` moves with this line, always; they are one fact written in
#: two files.
#:
#: It is still not a SEAL, and the distinction is worth keeping. Nothing signs a
#: flow ledger: it is a projection recomputed from history on every read, and
#: `compute_content_hash` is not called on it. What the ref now buys is that a
#: CONSUMER can validate what it was handed, and that
#: `tests/integration/test_the_flow_body_satisfies_its_released_contract.py`
#: fails here the moment this body and that document disagree — which is the
#: half that was missing while the ref pointed at nothing.
LEDGER_SCHEMA_REF = "urn:datacompany:review-flow-ledger:1"

#: The closed vocabulary of `inclusion_exports.sealed_with` — HOW the row's
#: `manifest_hash` was produced. Not decoration: the two are different evidence.
#: `content_hash` means the envelope was validated against a declared schema and
#: hashed through the full ordered algorithm; `canonical_sha256` means nothing
#: was validated at all and the bytes were merely canonicalized.
SEALED_WITH_CONTENT_HASH = "content_hash"
SEALED_WITH_CANONICAL_SHA256 = "canonical_sha256"
#: Rows written before the column existed. Their mode was never recorded and is
#: not inferable from the row, so it is stated as unknown rather than guessed —
#: see `0006_er_m006_export_seal_mode.py`. :func:`verify_export` resolves it per
#: row by recomputing.
SEALED_WITH_UNKNOWN = "unknown"


@contextmanager
def _reading_every_reviewers_work(session: Session) -> Generator[None]:
    """Bind `lead` visibility for the duration of one aggregate read.

    The flow is a count, not an attribution: it says how many records are
    included, never who said so. Deriving it therefore has to see every
    reviewer's current decision — but the RLS policy on `screening_decisions`
    keys visibility off `app.actor_role`, so a caller who is not a realm lead
    saw only their own work and the ledger counted everything else as
    unscreened.

    A `SECURITY DEFINER` routine cannot do this: `FORCE ROW LEVEL SECURITY`
    applies the policy to the table owner too, so a definer function owned by
    the migration role is filtered by the same predicate, and exempting it
    would need a `BYPASSRLS` role only a superuser can create.

    Transaction-scoped (`is_local => true`) and restored in `finally`, so the
    elevation cannot outlive the read or leak onto a pooled connection.
    """
    previous = session.execute(text(f"SELECT current_setting('{ROLE_GUC}', true)")).scalar() or ""
    try:
        session.execute(text(f"SELECT set_config('{ROLE_GUC}', 'lead', true)"))  # noqa: S608 - constant name
        yield
    finally:
        session.execute(
            text(f"SELECT set_config('{ROLE_GUC}', :role, true)"),  # noqa: S608 - constant name
            {"role": previous},
        )


def _stage_positions(session: Session, review_id: uuid.UUID) -> dict[uuid.UUID, ReviewStage]:
    """`stage_id -> the stage row`, for every stage this review has.

    ONE statement. Every screening row carries a `stage_id` but no `stage_order`,
    so the ordering the ledger turns on lives only here, and it is read once per
    derivation rather than per row.

    (This used to add "`screening_decisions.stage_id` is not even a foreign key".
    It is one — `screening_decisions_stage_id_fkey`, declared in
    `0005_er_m005_screening.py`, `ON DELETE RESTRICT`. The conclusion stands and
    the stated reason did not, which is worse than saying nothing: it invited the
    next reader to believe a stage_id could name a row that does not exist.)
    """
    stages = {
        stage.stage_id: stage
        for stage in session.execute(select(ReviewStage).where(ReviewStage.review_id == review_id)).scalars().all()
    }

    # THE BACKSTOP for a review whose stages have no total order.
    # `reviews.lock_protocol_version` refuses to create one; this refuses to
    # derive over one already in the table.
    #
    # `StagePosition` breaks an order tie on `stage_key` — deterministic, and
    # deliberately meaningless. Since this module started settling a record at
    # its highest position, a tie means an alphabetical accident chooses which
    # stage settles it, and the measured effect was a unanimous exclusion under
    # the current protocol discarded in favour of an inclusion under the
    # superseded one. Same posture as `_position_of` one function down: a
    # disposition that depends on a guess has no defensible treatment, so the
    # ledger says so instead of answering.
    by_order: dict[int, list[str]] = {}
    for stage in stages.values():
        by_order.setdefault(stage.stage_order, []).append(stage.stage_key)
    tied = sorted((order, sorted(keys)) for order, keys in by_order.items() if len(keys) > 1)
    if tied:
        order, keys = tied[0]
        raise ConservationError(
            f"stages {keys} both sit at position {order} in this review, so the stages have no total order "
            "and which of them settles a record would be decided by an alphabetical tie-break"
        )
    return stages


def _position_of(stages: dict[uuid.UUID, ReviewStage], stage_id: uuid.UUID, round_number: int) -> StagePosition:
    """Place one screening row in the review's stage order.

    Raises rather than defaulting, for the same reason :func:`bucket_for` does.
    A decision whose stage cannot be ordered has no defensible treatment: drop it
    and a record excluded at an unplaceable later stage silently settles from an
    earlier one — into the inclusion set, sealed, in an append-only artifact that
    gets cited. Guess an order and the guess decides the disposition.

    Unreachable through the API — `screening._stage` refuses an assignment or a
    decision whose stage does not belong to the review, and `review_stages` is
    append-only — so this is a claim about the database being intact, not a
    branch anybody is expected to hit.

    "Append-only" was asserted here before it was true: until `0011` the table
    had no trigger at all — the only history table in the schema with none — and
    the runtime role held `UPDATE` and `DELETE`. The sentence is kept because
    `0011` made it a fact rather than a hope.
    """
    stage = stages.get(stage_id)
    if stage is None:
        raise ConservationError(
            f"screening work exists at stage {stage_id} which is not a stage of this review; "
            "the ledger cannot order it against the stages that are"
        )
    return StagePosition(stage_order=stage.stage_order, stage_key=stage.stage_key, round_number=round_number)


def _collect(session: Session, review_id: uuid.UUID) -> FlowInputs:
    """Read the five immutable inputs. No writes, no caches consulted."""
    statuses = list(session.execute(select(ImportRow.status).where(ImportRow.review_id == review_id)).scalars().all())

    record_ids = set(
        session.execute(select(ReviewRecordVersion.review_record_id).where(ReviewRecordVersion.review_id == review_id))
        .scalars()
        .all()
    )

    # Duplicates removed: for every cluster whose CURRENT resolution is a merge,
    # every member except the survivor. Reading the chain tail is what makes a
    # merge -> split reversal show up as records returning to the corpus rather
    # than staying removed.
    # THREE statements for every cluster in the review, not two PER cluster.
    # `_collect` runs on every `/flow` read and twice more inside `rebuild_flow`,
    # and this loop was `2 x clusters` on its own — 600 statements on a review
    # with 300 duplicate clusters, for a response that is a handful of integers.
    removed: dict[uuid.UUID, uuid.UUID] = {}
    cluster_ids = list(
        session.execute(select(DuplicateCluster.cluster_id).where(DuplicateCluster.review_id == review_id))
        .scalars()
        .all()
    )
    resolutions = current_resolutions(session, cluster_ids)
    members_by_cluster = cluster_members(session, cluster_ids)
    merged: list[MergedGroup] = []
    for cluster_id in cluster_ids:
        resolution = resolutions.get(cluster_id)
        if resolution is None or resolution.resolution_type != "merge":
            continue
        if resolution.survivor_record_id is not None:
            merged.append(
                MergedGroup(
                    cluster_id=cluster_id,
                    members=frozenset(members_by_cluster.get(cluster_id, [])),
                    survivor=resolution.survivor_record_id,
                )
            )
        for member in members_by_cluster.get(cluster_id, []):
            if member != resolution.survivor_record_id:
                removed[member] = resolution.resolution_id

    # THE BACKSTOP, not the fix. `deduplication.resolve_cluster` refuses a merge
    # that would empty a duplicate group; this catches the rows written before
    # that refusal existed, and any writer that reaches the table without going
    # through the service.
    #
    # It is fail-CLOSED on purpose. The alternative — returning counts anyway —
    # is a ledger that silently loses a study, and the ledger is what the export
    # gate and the published PRISMA diagram are derived from. A refusal that says
    # which records vanished is recoverable by splitting a cluster; a sealed
    # export that omits a study is not.
    emptied = components_without_survivor(merged)
    if emptied:
        component, conflicting = emptied[0]
        raise ConservationError(
            "every record of a duplicate group is removed, so a study is in no bucket: "
            f"records {sorted(str(r) for r in component)} removed by clusters "
            f"{sorted(str(c) for c in conflicting)}. Split one of those clusters to return a survivor."
        )

    # Current decisions. A PRIVILEGED read — flow returns aggregate counts and
    # no attributions, so counting every reviewer's decision is safe here and
    # only here. The rows never leave this function.
    #
    # That was the INTENT; it was not the behaviour. The read still ran under
    # the caller's `app.actor_role`, and the RLS policy shows a non-lead only
    # their own decisions plus any conflict they have unblinded. So for a
    # caller authorised as review lead by CREATING the review — who holds no
    # realm `lead` role, because no such role exists in the realm — every
    # record the two reviewers AGREED on returned zero visible votes and was
    # counted `not_yet_screened`. The ledger silently under-reported, and the
    # export was refused forever with "1 records are not yet screened" while
    # the database held two decisions for it.
    #
    # Authorisation had been widened to the creator; visibility had not. Both
    # callers are already gated by `require_review_lead` above, so binding the
    # GUC the policy reads is what makes the two agree. Same shape as
    # `screening._maybe_open_conflict`: elevate, read, restore in `finally`,
    # never across a commit.
    #
    # KEYED BY `(stage, round, record)`, NOT BY RECORD.
    #
    # This dict was `decisions[review_record_id]`, and `stage_id` and
    # `round_number` were read off the row and thrown away. `derive` then
    # demanded unanimity over the merged list, so a record INCLUDED unanimously
    # at title/abstract and EXCLUDED unanimously at full text held two distinct
    # dispositions and was filed `not_yet_screened` — permanently, with no
    # conflict to resolve, because `_maybe_open_conflict` is stage keyed and had
    # correctly opened none. See `flow_derivation`'s module docstring.
    stages = _stage_positions(session, review_id)
    decisions: dict[tuple[StagePosition, uuid.UUID], list[tuple[str, str | None]]] = {}
    # WHEN each position was last worked, from the same rows. `decided_at` is
    # persisted and immutable, so this is stored history and not a clock read —
    # see `FlowInputs.settling_position` for what it decides and why a stage
    # order alone cannot decide it once rounds exist.
    worked_at: dict[tuple[StagePosition, uuid.UUID], Any] = {}
    with _reading_every_reviewers_work(session):
        for row in blinded_reads.unblinded_current_decisions(session, review_id=review_id):
            position = _position_of(stages, row.stage_id, row.round_number)
            decisions.setdefault((position, row.review_record_id), []).append(
                (row.disposition, row.exclusion_reason_code)
            )
            key = (position, row.review_record_id)
            if row.decided_at is not None and (key not in worked_at or row.decided_at > worked_at[key]):
                worked_at[key] = row.decided_at

    # TWO statements for every conflict, not one per conflict — and the chain
    # walk is `screening._chain_tail`, so the ledger and the adjudication panel
    # cannot disagree about which adjudication is current. This loop had its own
    # inline copy of that walk, and the copy was subtly different: it had no
    # cycle guard, where `current_resolution`'s does.
    #
    # Imported inside the function, not at module scope: `screening` imports
    # `flow` nowhere, but `flow._unresolved_conflicts` already defers this same
    # import, and matching it keeps the direction obvious to the next reader.
    from application.services.screening import current_adjudications

    conflicts = list(
        session.execute(select(ScreeningConflict).where(ScreeningConflict.review_id == review_id)).scalars().all()
    )
    #
    # AND KEYED BY `(stage, round, record)` FOR A SECOND, INDEPENDENT REASON.
    #
    # `screening_conflicts` is keyed `(review_id, stage_id, round_number,
    # review_record_id)` — `uq_conflict_once` — so a record disputed in two
    # stages has TWO conflicts and, once a lead has worked, two CURRENT
    # adjudications. This comprehension keyed them by `review_record_id` alone
    # over an UNORDERED `select(ScreeningConflict)`, so the two collapsed to
    # whichever row the scan happened to yield last.
    #
    # That is not merely a wrong answer, it is a NON-DETERMINISTIC one: both
    # rows are equally current, nothing in the query orders them, and Postgres
    # is free to return them in either order on either read. Two reads of
    # identical immutable history could therefore produce different
    # `canonical_counts_hash` values — the exact property this module's own
    # docstring says a derived projection cannot have, and the one
    # `flow:rebuild` exists to check. It could not have caught it: it compares a
    # hash from this code against a hash from this code.
    tails = current_adjudications(session, [c.conflict_id for c in conflicts])
    adjudications: dict[tuple[StagePosition, uuid.UUID], tuple[str, str | None]] = {
        (_position_of(stages, conflict.stage_id, conflict.round_number), conflict.review_record_id): (
            tails[conflict.conflict_id].disposition,
            tails[conflict.conflict_id].exclusion_reason_code,
        )
        for conflict in conflicts
        if conflict.conflict_id in tails
    }

    # ADJUDICATIONS DELIBERATELY CONTRIBUTE NO TIME. They contribute a position
    # to `_positions_by_record`, and this once timed them too — which was wrong,
    # and `test_the_earlier_adjudication_does_not_override_the_later_stage` said
    # so on the first full run.
    #
    # The two are different kinds of event. A later ROUND at an earlier stage is
    # a re-screen: new work, which supersedes. A late ADJUDICATION of an earlier
    # stage's conflict is backfill: it settles what that stage should have
    # concluded, and it does not overturn a stage that has since been screened.
    # Reviewers disagreed at title/abstract, the record was screened at full text
    # anyway, and the lead then ruled on the title/abstract disagreement — the
    # lead's ruling answers the older question. Timing it made it the record's
    # newest work and handed a full-text inclusion back to a title/abstract
    # ruling.
    #
    # So a record's position advances on DECISIONS only. A position reached by an
    # adjudication alone is therefore untimed, `settling_position`'s coverage is
    # partial, and it falls back to stage order — which is unreachable through
    # this collector (a conflict needs `reviewers_required` disagreeing decisions
    # at its own position to exist, so the decisions have already timed it) and is
    # the honest answer for a `FlowInputs` built by hand.

    protocol = active_protocol(session, review_id)
    fallback = protocol.reviewers_required if protocol else 2
    return FlowInputs(
        import_row_statuses=statuses,
        # THE REVIEW'S OWN STAGE LIST, for `counts_by_stage` to order and label.
        # `_stage_positions` has already refused a review whose stages have no
        # total order, so sorting these is sorting a total order.
        #
        # It is read for the LABEL and the ORDER and for nothing else: a stage
        # declared here that no decision reached is a row of zeros, and it can
        # never move a record, because `_positions_by_record` is built from
        # decisions and adjudications and from nothing else. Passing the stage
        # list is therefore not the same mistake as reading assignments — see
        # that function's docstring for why an assignment is not evidence.
        stages=tuple(
            StageDescriptor(stage_order=stage.stage_order, stage_key=stage.stage_key, name=stage.name)
            for stage in stages.values()
        ),
        record_ids=record_ids,
        removed_by_merge=removed,
        decisions=decisions,
        adjudications=adjudications,
        worked_at=worked_at,
        reviewers_required=fallback,
        # `stage.reviewers_required or <protocol>` — the SAME expression
        # `screening.record_decision` passes to `_maybe_open_conflict`, so the
        # ledger and conflict detection cannot disagree about how many reviewers
        # a stage needs. They did: the ledger read only the protocol's number,
        # so a stage declaring three reviewers had records settled by two.
        reviewers_required_by_stage={
            stage.stage_key: (stage.reviewers_required or fallback) for stage in stages.values()
        },
    )


def current_flow_revision(session: Session, actor: Actor, review_id: uuid.UUID) -> FlowRevision:
    """The open flow revision, created on first use.

    `01-…:504-505` — reopening a closed review creates a NEW revision and the
    prior close is never rewritten. The revision number is bumped by the number
    of times the review has been reopened, which is a fact in
    `review_state_history` rather than a counter anyone maintains.
    """
    from models.reviews import ReviewStateEvent

    reopens = int(
        session.execute(
            select(ReviewStateEvent).where(
                ReviewStateEvent.review_id == review_id,
                ReviewStateEvent.from_state == "closed",
                ReviewStateEvent.to_state == "active",
            )
        )
        .scalars()
        .all()
        .__len__()
    )
    wanted = reopens + 1

    revision = session.execute(
        select(FlowRevision).where(FlowRevision.review_id == review_id, FlowRevision.revision_number == wanted)
    ).scalar_one_or_none()
    if revision is not None:
        return revision

    # ON CONFLICT DO NOTHING, because THIS INSERT IS REACHED FROM A GET.
    #
    # `GET /flow` calls `compute_flow(persist=False)`, which still has to open
    # the revision on first use, and `actor_scope` commits it. A plain
    # `session.add` made the first two concurrent readers of a review race
    # `uq_flow_revision_number`: one won, the other took a unique violation,
    # `_integrity_handler` dressed it as `409 state_conflict`, and a caller who
    # had only read something was told their request conflicted with the state.
    # There is nothing they could do with that, and nothing they did wrong.
    #
    # Same shape as `screening._maybe_open_conflict` and `create_assignments`:
    # let the unique key elect the winner, then re-select — the loser's
    # `RETURNING` is empty and the row it wants is the one the winner wrote.
    session.execute(
        pg_insert(FlowRevision)
        .values(
            flow_revision_id=uuid.uuid4(),
            review_id=review_id,
            tenant_id=actor.tenant_id,
            revision_number=wanted,
        )
        .on_conflict_do_nothing(constraint="uq_flow_revision_number")
    )
    session.flush()
    revision = session.execute(
        select(FlowRevision).where(FlowRevision.review_id == review_id, FlowRevision.revision_number == wanted)
    ).scalar_one_or_none()
    if revision is None:  # pragma: no cover - the insert above either wrote it or lost to a writer who did
        raise NotFoundError("the flow revision disappeared while being opened")
    return revision


def require_no_owed_decisions(session: Session, actor: Actor, review_id: uuid.UUID, what: str) -> None:
    """Refuse a caller who still owes a screening decision in this review.

    ## The hole this closes

    `routers/flow.py` argued that "lead only" was the mitigation for the ledger's
    counting leak, and named the leak precisely: serving these counts to a
    reviewer hands them `records_screened`, which is how far their partner has
    got. What the argument missed is that **lead-over-this-review is not a role**
    — `require_review_lead` also grants to `reviews.created_by_subject_id`, and
    nothing stops the creator of a review from screening it. Measured on
    `ef028ab` with `reviewers_required=1`, one record, `C` creating the review
    and assigning `[C, B]`, `B` excluding, `C` having decided nothing:

        GET /decisions?review_record_id={r0}  as C -> total 0            (correct)
        GET /flow                             as C -> excluded 1,
            exclusion_reasons {"wrong_design": 1}, settled_exclude 1     (LEAK)

    `C` reads `B`'s disposition and `B`'s exclusion reason on the exact record
    `C` is about to judge — no unblinding, no audience, no audit row. It is the
    same disclosure `blinded_reads.visible_decisions` documents and refuses,
    reached through a route that never calls it.

    ## WHEN IT FIRES: only where there is something to disclose

    The condition is `blinded_reads.an_aggregate_would_count_work_this_caller_may
    _not_read` — *another subject holds a current decision at a position this
    caller still owes* — which is exactly the set `visible_decisions` drops on
    `GET /decisions`, asked as "would it drop anything".

    NOT "does the caller owe anything". That was this function's first shape and
    it refused a caller who owed a position nobody else had touched, where the
    ledger holds only their own work plus work at positions they do not owe —
    every row of which `/decisions` already serves them. It reddened
    `test_the_creator_governs_their_own_review_and_only_theirs`, whose creator is
    the review's sole reviewer and can learn nothing from the counts. Refusing
    where there is nothing to disclose is not a stricter guard; it is a broken
    one, and it puts the rule back on the role.

    ## WHAT IS SUPPRESSED: the whole ledger, and deliberately not part of it

    Once it fires, every count on that route: not the `excluded` bucket, not the
    exclusion reasons, not `counts_by_stage` — all of it. Two reasons, and the
    second is the one that decides it:

    * **A conserved aggregate cannot be partly suppressed.** `derive`'s buckets
      sum to `records_total`; that identity is the ledger's purpose and is
      pinned by `tests/property/test_flow_conservation.py`. Blank `excluded` and
      the reader recovers it by subtracting the buckets that remain. And the
      leak does not need arithmetic at a small denominator: "1 of 1 excluded"
      IS the partner's verdict, spelled as a number.
    * **The honest-looking alternative corrupts the seal.** Deriving from only
      the rows this caller may see would return a conserving ledger that no
      history supports — and `create_export` derives from the same
      `_compute_flow`, so a per-caller derivation is one call site away from
      being hashed into an append-only artifact that gets cited.

      ⚠️ **That sentence was true and this function was not called from there.**
      It named `create_export` as sharing `_compute_flow` and stopped at the
      hypothetical, while the real call site sat ungated one function below:
      `create_export` reaches `_compute_flow` directly rather than through
      `compute_flow`, so it inherited nothing, and `POST /exports` served the
      counts `GET /flow` had just refused — then sealed them into a row that
      cannot be deleted. Gated there now, and on the three READ routes through
      `require_export_audience`, whose docstring carries the decision about
      seals that already exist. A shared derivation is not a shared gate.

    ## WHAT IS STILL EXPOSED, on purpose

    * **That the caller owes something.** Their own assignments and their own
      decisions — `GET /screening/queue` serves both — so the refusal states a
      fact they supplied. It carries no count and no record id all the same: a
      "you owe 3 of 40" would be a denominator to difference against the ledger.
    * **That somebody else has worked where they owe.** The refusal firing at
      all says that much, and it is the one thing here the caller did not
      already supply. It is accepted deliberately: the alternative is a refusal
      that fires uniformly and therefore blinds a caller who owes a position
      nobody has touched, and the disclosure is bounded at "work exists" —
      never which record, which stage, how much, or which way it went. `ER-004.2`
      names DISPOSITIONS and PROGRESS COUNTS as the leak; "a partner has
      started" is neither, and the queue's own `assigned_to_me` already implies
      a partner exists.
    * **The review, its stages and its protocol.** `GET /stages` already lists
      every stage to anybody who can read the review, and a stage list contains
      no work.
    * **The ledger in full, the moment their own verdicts are in.** This is a
      caller property, not a role property — the same rule
      `visible_decisions` states — so it lifts by working, not by being
      granted anything. A lead who is not screening never sees it at all.

    ## Why here and not only in the router

    `require_review_lead_for`'s own docstring, and `rebuild_flow`'s: a check
    written in a router reads as no check at all from the service, and this
    module's neighbours have lost that argument nine times. `flow:audit` is the
    exception and is gated in `routers/flow.py`, because its service lives in
    `flow_audit` and derives through `_collect` rather than through here.

    ## Every route this guard covers, and the one that covers itself

    Enumerated rather than described, because the name of the test that was
    supposed to hold this list was broader than the list —
    `tests/security/test_the_ledger_is_blinded_from_a_reader_who_still_owes.py`
    now asserts the caller set of `_compute_flow` directly:

    * `GET /flow` and `POST /flow:rebuild` — through :func:`compute_flow`;
    * `POST /exports` — through :func:`create_export`, which calls
      `_compute_flow` directly and therefore holds the guard itself;
    * `GET /exports`, `GET /exports/{id}`, `GET /exports/{id}:verify` — through
      :func:`require_export_audience`, because a seal taken earlier by a lead
      who owed nothing is the same disclosure to a caller who owes one;
    * `POST /flow:audit` — in `routers/flow.py`, for the reason above.
    """
    # ELEVATED, because the question is about rows L1 will not show this caller.
    # Under their own binding the second statement returns nothing and the guard
    # answers "nothing to hide" for exactly the callers it exists to refuse —
    # the identical trap `_collect` fell into, recorded in its own comment.
    # Transaction-scoped and restored in `finally`; see the context manager.
    with _reading_every_reviewers_work(session):
        leaks = blinded_reads.an_aggregate_would_count_work_this_caller_may_not_read(
            session, review_id=review_id, actor=actor
        )
    if leaks:
        raise ForbiddenError(
            f"{what} would report every reviewer's work as counts, and you still owe a screening "
            "decision on a record another reviewer has already judged — at a small corpus a bucket "
            "total names their verdict on the record you have yet to judge. Record your own "
            "decisions first; the ledger opens as soon as you owe nothing there."
        )


def compute_flow(session: Session, actor: Actor, review_id: uuid.UUID, *, persist: bool) -> dict[str, Any]:
    """Derive the flow. `persist=True` also refreshes the projection cache.

    Holds :func:`require_no_owed_decisions` itself, so `rebuild_flow` — which
    calls this — is covered by one gate rather than by two that can drift.
    """
    require_no_owed_decisions(session, actor, review_id, "the flow ledger")
    body, _, _ = _compute_flow(session, actor, review_id, persist=persist)
    return body


def _utc_z(value: datetime | None) -> str | None:
    """RFC 3339 in UTC with a trailing ``Z``, which is the released grammar.

    ``datetime.isoformat()`` renders a UTC offset as ``+00:00``. Both spellings
    are valid RFC 3339 and mean the same instant, but ``stable-identity:1``'s
    ``timestamp`` primitive admits only the ``Z`` form, so the two are not
    interchangeable at the contract boundary.

    ``astimezone`` first rather than assuming: the column is ``timestamptz`` and
    psycopg returns it in the session's timezone, which is UTC here but is a
    server setting rather than a guarantee. Formatting without converting would
    stamp a local offset with a ``Z`` and silently move the instant.

    Delegates to `instants.utc_z`, which is now the single implementation.
    Byte-identical for every aware datetime — which is all of them, the columns
    being `timestamptz` — and stricter for a naive one, which it reads as UTC
    rather than as local.
    """
    from instants import utc_z

    return utc_z(value)


def _compute_flow(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    persist: bool,
    inputs: FlowInputs | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FlowRevision]:
    """`compute_flow`, also handing back the derivation and the revision.

    `create_export` needs both, and used to get them by calling `_collect` and
    `current_flow_revision` a SECOND time on the same session in the same
    transaction, against tables that cannot have changed in between. `_collect`
    is itself nested — 2 statements per duplicate cluster and 1 per screening
    conflict on top of a fixed 11 — so on a 5,000-record review with 300 clusters
    and 80 conflicts one `POST /exports` issued roughly 1,500 statements, about
    half of them literally the same reads twice.

    The public `compute_flow` keeps its exact signature and response, which is
    what `test_flow_revision_race.py` pins: a `GET /flow` with `persist=False`
    must still open exactly one revision row and no more.
    """
    inputs = _collect(session, review_id) if inputs is None else inputs
    result = derive(inputs)
    counts = result["counts"]
    digest = canonical_counts_hash(counts)
    revision = current_flow_revision(session, actor, review_id)

    if persist:
        session.add(
            FlowProjection(
                projection_id=uuid.uuid4(),
                review_id=review_id,
                tenant_id=actor.tenant_id,
                flow_revision_id=revision.flow_revision_id,
                counts=counts,
                canonical_counts_hash=digest,
            )
        )
        session.flush()

    cached = session.execute(
        select(FlowProjection)
        .where(FlowProjection.review_id == review_id)
        .order_by(FlowProjection.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    body = {
        "review_id": str(review_id),
        "flow_revision_id": str(revision.flow_revision_id),
        "flow_revision_number": revision.revision_number,
        "counts": counts,
        # BESIDE `counts`, and unversioned. `LEDGER_SCHEMA_REF` is a label —
        # nothing is sealed against this body and no schema declares it — so a
        # PRISMA-compliant per-stage breakdown costs no contract release here,
        # where inside `counts` it would cost a new export version AND a new
        # `canonical_counts_hash` for every review in the table. See
        # `flow_derivation._counts_by_stage`.
        "counts_by_stage": result["counts_by_stage"],
        "canonical_counts_hash": digest,
        "schema_ref": LEDGER_SCHEMA_REF,
        # `ER-005.3` — stale projection must be VISIBLE, not silently served.
        "projection_stale": bool(cached and cached.canonical_counts_hash != digest),
        # `_utc_z`, not `.isoformat()`. The released `timestamp` primitive is
        # trailing-Z only; `datetime.isoformat()` emits `+00:00` and the document
        # refuses it. The contract states the fleet's grammar and this producer
        # conforms to it — widening the released document to accommodate a
        # stringification default would have been the other direction, and the
        # wrong one.
        "projection_computed_at": _utc_z(cached.computed_at) if cached else None,
    }
    return body, result, revision


def included_record_ids(session: Session, review_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """The records this review has actually INCLUDED, derived, never stored.

    `ER-006` needs this: a research object may only be built from a report the
    review included, and "included" is not a column anywhere. It is the terminal
    bucket `flow_derivation.derive` computes from the immutable decision history.

    This exists so that `ER-006` asks the SAME function the PRISMA ledger and the
    sealed export ask, rather than growing a second opinion about what inclusion
    means. The expression below is the one `create_export` already uses on its
    own `result["dispositions"]`; if the two ever disagree, one of them is wrong
    and it will not be silent.

    Deliberately NOT a re-implementation in SQL. A PL/pgSQL trigger could have
    enforced this in the database, which would be stronger — but it would be a
    second implementation of supersession, agreement, adjudication and stage
    ordering, in a language with no tests against generated histories, drifting
    quietly from the one in `flow_derivation`. The weaker check against the
    single authority is worth more than the stronger check against a copy.

    Privileged read: `_collect` reads every reviewer's work, exactly as the
    ledger does. Only record ids leave this function, and a record id is not an
    attribution.
    """
    result = derive(_collect(session, review_id))
    return frozenset(d.review_record_id for d in result["dispositions"] if d.bucket == "included")


def rebuild_flow(session: Session, actor: Actor, review_id: uuid.UUID) -> dict[str, Any]:
    """Recompute from zero and compare (`01-…:510`). **Lead only.**

    The comparison is against the stored projection, which is the thing that
    could have drifted. Returning `matches_previous_projection: false` is a
    finding, not an error — it means the cache was wrong and has now been
    corrected, and the caller should be told rather than have it quietly fixed.

    The gate is held HERE and not only in `routers/flow.py`, which is the point
    `require_review_lead_for`'s own docstring makes: a check written in a router
    looks identical to no check at all when the service function is read, and
    that is precisely how `import_payload`, `seal_corpus_version`, `patch_review`
    and then six more call sites each went a whole review cycle without one.

    This one was not exploitable — the single router that reaches it does check —
    so it is consistency rather than a fix. But it is a WRITE (`persist=True`),
    it is the shape of the defect that has now been found nine times in this
    module's neighbours, and the cost of closing it is one line. The router keeps
    its check too: it loads the review anyway, and a duplicated gate is free
    where `session.get` is identity-mapped.
    """
    require_review_lead_for(session, actor, review_id, "rebuilding the flow ledger")
    before = session.execute(
        select(FlowProjection)
        .where(FlowProjection.review_id == review_id)
        .order_by(FlowProjection.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    result = compute_flow(session, actor, review_id, persist=True)
    result["rebuilt"] = True
    result["previous_canonical_counts_hash"] = before.canonical_counts_hash if before else None
    result["matches_previous_projection"] = (
        before is None or before.canonical_counts_hash == result["canonical_counts_hash"]
    )
    return result


# --------------------------------------------------------------------------
# The seal
# --------------------------------------------------------------------------


def _decided_versions(
    session: Session, review_id: uuid.UUID, record_ids: list[uuid.UUID]
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """`review_record_id -> EVERY version its current decisions were taken against`.

    A privileged read, for the same reason `_collect`'s is: the flow needs to see
    every reviewer's work to count it, and these rows never leave this function —
    only the version ids do, and a version id is not an attribution.

    ## A SET, because dropping a disagreement was a silent pass

    This returned one version per record and OMITTED any record whose reviewers
    named different ones. The caller reads an absent entry as "nothing to
    contradict", so one reviewer citing content the corpus does not hold was
    refused, and two reviewers citing two different things — strictly worse, and
    unambiguously a record nobody agreed on — sealed. The gate went silent in the
    case it exists to catch.

    Reporting the whole set makes the caller's test one expression for both:
    anything that is not exactly `{the corpus's version}` is refused. The old
    docstring reasoned that the drop was safe because it was "only reachable if a
    version appeared mid-stage" — which is precisely what `apply_survivorship`
    does, and what `record_decision` used to stamp decisions with.

    ## PER SETTLING STAGE, for the same reason the ledger is

    This read every current decision on the record and required all of them to
    name one version. Across ONE stage that is the question it means to ask. In
    a two-stage review it is not: a survivorship merge between the stages mints
    v2, the title/abstract decisions cite v1 and the full-text decisions cite
    v2, `len(vs) == 2`, and the record is dropped from the result — which the
    caller reads as "nothing to contradict". The check that exists to refuse an
    export crediting a judgement to content nobody read would have gone silent
    in exactly the review shape where a re-seal is most likely.

    An inclusion is a statement about the stage that INCLUDED the record, so the
    version to compare is the one that stage was decided against — the highest
    `StagePosition` this record has DECISIONS at, ordered by the same
    `_position_of` the ledger uses.

    That is the ledger's settling position for every record this service can
    write, because a position carrying an adjudication carries decisions too (see
    `flow_derivation._positions_by_record`). It is not the same expression, and
    it is deliberately not made so: an adjudication has no
    `review_record_version_id`, so a settling position derived from one would
    have no version to report and the answer here would be an absence — which
    the caller reads as "nothing to contradict", the exact silent-pass this
    section exists to close.
    """
    if not record_ids:
        return {}
    wanted = set(record_ids)
    stages = _stage_positions(session, review_id)
    seen: dict[uuid.UUID, dict[StagePosition, set[uuid.UUID]]] = {}
    when: dict[uuid.UUID, dict[StagePosition, Any]] = {}
    with _reading_every_reviewers_work(session):
        for row in blinded_reads.unblinded_current_decisions(session, review_id=review_id):
            if row.review_record_id in wanted:
                position = _position_of(stages, row.stage_id, row.round_number)
                seen.setdefault(row.review_record_id, {}).setdefault(position, set()).add(row.review_record_version_id)
                if row.decided_at is not None:
                    times = when.setdefault(row.review_record_id, {})
                    if position not in times or row.decided_at > times[position]:
                        times[position] = row.decided_at
    # `settling_position`, not `max(...)`: the same rule the ledger settles by.
    # Two spellings of "which position settles this record" is exactly how this
    # module and conflict detection came to disagree about `reviewers_required`.
    #
    # AND THE SENTINEL WIDENS THE GATE RATHER THAN OPENING IT. `settling_position`
    # answers `UNATTRIBUTABLE` for a record whose positions are partly timed, and
    # the one thing this function must never do with a record it cannot place is
    # leave it out of the result: the caller reads an absent entry as "nothing to
    # contradict", which is the silent pass the SET above was introduced to
    # close. So an unplaceable record reports every version named at ANY of its
    # positions.
    #
    # That is strictly stricter and can only ever refuse more: the union equals
    # `{corpus version}` exactly when every reviewer at every stage named that
    # one version, which is a stronger claim than unanimity at the settling
    # stage alone. It cannot make an export pass that the per-stage answer would
    # have refused.
    #
    # Both sides here are built from the SAME decision rows, and
    # `screening_decisions.decided_at` is `NOT NULL`, so coverage is total and
    # this arm is unreachable from this collector — the same standing as the
    # ledger's. It is written out rather than left to `KeyError` because the
    # failure mode of the missing branch is not an exception, it is a `KeyError`
    # on a lead's `POST /exports` naming a position, which tells them nothing.
    out: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    for rid, by_position in seen.items():
        position = settling_position(set(by_position), when.get(rid, {}))
        if position is UNATTRIBUTABLE:
            out[rid] = frozenset().union(*by_position.values())
            continue
        out[rid] = frozenset(by_position[position])
    return out


def items_hash(included: list[dict[str, Any]], omissions: list[dict[str, Any]]) -> str:
    """The canonical hash of an export's two item arrays — `inclusion-set-export:2`.

    ONE FUNCTION, called by both the writer and the verifier, because the whole
    value of this hash is that two parties compute it the same way. A second
    spelling anywhere would turn a sound export into an accusation.

    **What is hashed is the contract**, not an implementation detail: the object
    ``{"included": [...], "omissions": [...]}`` — those two members and no
    others, the arrays verbatim and in the order they arrived — through the
    released RFC 8785 canonicaliser. The schema's own ``$comment`` states the
    same shape, in the same words, so a consumer never has to read this code to
    reproduce it.

    Array order is part of the digest and is deliberately NOT re-sorted here.
    The producer already emits ``included`` sorted by ``review_record_id`` and
    ``omissions`` as records-then-quarantine, each block sorted by its own id;
    re-sorting on the way into the hash would make this function disagree with
    every consumer that hashes what it received.

    `canonical_sha256`, not `compute_content_hash`: this is a hash of a
    DIFFERENT document carried as evidence inside the manifest, exactly like
    `canonical_counts_hash` beside it. The manifest is still sealed with the
    full ordered algorithm, and it now covers the items transitively. Declaring
    the item arrays as their own released contract would validate them as well
    as bind them — a strictly stronger claim, deliberately deferred rather than
    smuggled in under this name.

    The engine refuses a value it cannot canonicalize (a non-finite or
    unstably-rendered float). That is unreachable from here and is left to
    raise rather than handled: every member of both arrays is a `str`, an `int`,
    a `bool` or `None`, read from typed columns. `discovery-engine` #56 had to
    turn the same raise into a 422 because its digested document echoed
    arbitrary caller JSON; this one does not.
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256({"included": included, "omissions": omissions})


def _omission_row(disposition: RecordDisposition) -> dict[str, Any]:
    """One entry of the sealed export's `omissions` array.

    ONE FUNCTION, so that the shape of an omission has a single definition and a
    test can substitute the previous one verbatim rather than approximate it —
    which is how `test_the_omission_shape_change_mints_a_second_export_across_the_deploy_boundary`
    reproduces a row sealed by the previous release instead of arguing about it.

    ## `ER-009.3` ALSO MEANS *WHERE*

    Four of the five fields on the disposition were written here and
    `settled_at` — the stage the record was settled at, sitting on the same
    object — was dropped. So the sealed export could say that a record was
    excluded for `not_retrievable` and NOT whether that happened at the abstract
    or after the full text was read. Those are different findings in PRISMA, and
    with `counts.exclusion_reasons` merging the stages too, this array was the
    only place a consumer could have recovered the difference.

    `None` for a `duplicate_removed` or `not_yet_screened` record, which settled
    at no stage. Explicitly null rather than absent: the member is part of the
    shape, and "this record settled nowhere" is a fact, not a gap.

    ## LEGAL UNDER `:2` WITHOUT A CONTRACT RELEASE

    The item arrays are deliberately outside the manifest and deliberately
    undeclared — `items_hash`'s own `$comment` says declaring them is
    "deliberately deferred" — so no released rule constrains an omission's
    members.

    ## WHAT IT COSTS, MEASURED, AND WHY THAT IS AN ARGUMENT FOR SHIPPING IT NOW

    Every already-sealed row still verifies: `verify_export` recomputes
    `items_hash` from the stored `export.payload`, never from a fresh
    derivation, and `counts_match` compares array LENGTHS, which two extra
    members do not move.

    But `POST /exports` STOPS REPLAYING ACROSS THE DEPLOY BOUNDARY, and that is
    a real consequence rather than a theoretical one. The replay key is
    `manifest_hash`; `items_hash` is a member of the manifest; the omissions
    cargo changed. So a review sealed under the previous release and re-sealed
    under this one mints a SECOND permanent, undeletable export — `replayed:
    false`, two rows — which is exactly what `test_an_unchanged_review_seals_one_export`
    exists to prevent, and it is not reachable by any test that seals and
    re-seals inside one process.

    The alternative is worse and was rejected: holding `items_hash` stable over
    changed bytes would mean an item seal that no longer describes its cargo,
    which is the entire defect `:2` was released to close.

    So the cost is paid once, at the boundary, and it is paid in proportion to
    how many exports already exist. Measured against production before this
    landed:

        inclusion_exports 0 · screening_decisions 0 · reviews 0 · review_records 0

    Nothing has ever been sealed. The shape change is therefore strictly cheaper
    landed BEFORE the first real inclusion set than after it, and every day it
    waits it gets more expensive — which is the argument for shipping it now
    rather than the argument against shipping it at all.
    """
    return {
        "review_record_id": str(disposition.review_record_id),
        "bucket": disposition.bucket,
        "exclusion_reason_code": disposition.exclusion_reason_code,
        "adjudicated": disposition.adjudicated,
        "settled_at_stage_key": disposition.settled_at.stage_key if disposition.settled_at else None,
        "settled_at_stage_order": disposition.settled_at.stage_order if disposition.settled_at else None,
    }


def unattributable_record_ids(dispositions: list[RecordDisposition]) -> list[str]:
    """The records the ledger could not place at any stage, sorted, as strings.

    Its own function, and a public one, for the same reason `_omission_row` is:
    the refusal below is the only thing standing between an unorderable history
    and a sealed inclusion set, and a refusal that needs a live Postgres to
    exercise is a refusal that gets tested once and then trusted. This is a pure
    function of the derivation's own output, so
    `test_a_partly_timed_history_is_not_ordered_by_a_guess` exercises the
    predicate `create_export` actually branches on, rather than a re-spelling of
    it that could agree with the code while the code was wrong — the failure
    `check_stage_conservation`'s docstring describes for a checker that
    recomputes what it checks.

    Sorted and stringified here rather than at the raise, so the ids in the
    problem-details body are stable across runs and diffable between two
    attempts at the same seal.
    """
    return sorted(str(d.review_record_id) for d in dispositions if d.unattributable)


def create_export(session: Session, actor: Actor, review_id: uuid.UUID) -> dict[str, Any]:
    """The hash-sealed inclusion set — `ER-009`, `ER-005.5`, `DM-AT-28`.

    Two hashes over two different things, and they are never substituted:

    * ``manifest_hash`` — RFC 8785 canonical hash of the metadata envelope,
      through ``compute_content_hash(instance, schema_ref)`` against the
      registered schema, so a consumer verifies against a declared shape rather
      than against our serialization habits;
    * ``payload_digest`` — sha256 of the exact bytes served.

    Refused while any stage is incomplete. `01-…:125` — a close cannot happen
    with conflicts unresolved, and an export of an unfinished review is a
    filtered set presented as a complete one.

    ## ⛔ AND REFUSED TO A CALLER WHO STILL OWES A DECISION

    This function calls `_compute_flow` DIRECTLY rather than the public
    `compute_flow`, for the performance reason `_compute_flow`'s own docstring
    gives — so when `require_no_owed_decisions` was added to `compute_flow` to
    close `GET /flow`, this call site did not inherit it, and its only gate was
    `require_review_lead`: the same creator grant that made `GET /flow`
    reachable to a screening reviewer in the first place. Measured on `5442ad5`
    with `reviewers_required=1`, one record, `C` creating the review and
    assigned to it, `B` excluding, `C` having decided nothing:

        GET  /flow           as C -> 403
        POST /exports        as C -> 201, omissions_count 1
        GET  /exports/{id}   as C -> 200, manifest.counts {"excluded": 1,
                                     "exclusion_reasons": {"wrong_design": 1}}

    Worse than the leak on `GET /flow`, not equal to it: `inclusion_exports` is
    append-only (`trg_inclusion_exports_append_only`) and hash-sealed, so the
    counts stay readable through `GET /exports` forever — including long after
    `C` has decided and the guard would refuse nothing.

    The gate is held HERE and not only in `routers/exports.py`, which is the
    argument `require_review_lead_for` and `rebuild_flow` both make about this
    module and which this defect is the ninth demonstration of.
    """
    subject = require_review_lead(actor, load_review(session, review_id))
    # BEFORE `_collect`, and before every refusal below. Those refusals are
    # themselves disclosures — `not_yet_screened=N`, `unresolved_conflicts`,
    # `record_ids` — so a guard placed after them would refuse the seal and hand
    # over the numbers on the way out.
    require_no_owed_decisions(session, actor, review_id, "sealing the inclusion set")
    state, _ = current_state(session, review_id)
    if state is None:
        raise NotFoundError("no such review")

    # ONE `_collect`, reused. See `_compute_flow`.
    inputs = _collect(session, review_id)
    flow, result, revision = _compute_flow(session, actor, review_id, persist=True, inputs=inputs)
    counts = flow["counts"]

    # THE REFUSAL THAT HAS TO BE READ BEFORE #1, BECAUSE #1's REMEDY DOES NOT
    # WORK ON THESE RECORDS.
    #
    # A record whose positions are partly timed has no settling stage that is not
    # a guess, so `derive` files it `not_yet_screened` — see
    # `FlowInputs.settling_position` and `flow_derivation.UNATTRIBUTABLE`. Left to
    # #1 below, the lead is told "N records are not yet screened", goes and
    # screens them, and coverage stays partial: screening adds a timed position
    # without removing the untimed one, so the same 422 comes back with the same
    # number. An instruction that cannot succeed is worse than no instruction —
    # it costs a reviewer a full pass over the record and leaves the ledger
    # exactly where it was.
    #
    # It is also a DIFFERENT KIND of refusal. #1 is "the review is not finished",
    # a normal state every review passes through. This one is "the review's
    # stored history cannot be ordered", which no `POST /decisions` produced and
    # no `POST /decisions` can clear; the remedy is to repair the row that has no
    # `decided_at` behind it. Naming the records is the whole point — the ledger
    # knows exactly which ones, and a lead who is only told a count has nowhere
    # to start.
    #
    # `GET /flow` still answers, with these records in `not_yet_screened` and in
    # no stage tally. Refusing to SEAL is not refusing to be READ.
    unattributable = unattributable_record_ids(result["dispositions"])
    if unattributable:
        raise UnprocessableStateError(
            f"{len(unattributable)} records reached stages the ledger cannot order: some of the work on "
            "them carries a decision time and some does not, so no stage can be shown to have settled "
            "them. Screening them again will not clear this — it adds a timed position without removing "
            "the untimed one. Sealing now would credit each record to a stage chosen by a guess.",
            unattributable_record_ids=unattributable,
        )

    # Negative control #1: an incomplete stage cannot export.
    if counts["not_yet_screened"] > 0:
        raise UnprocessableStateError(
            f"{counts['not_yet_screened']} records are not yet screened; an export now would present "
            "an incomplete review as a complete inclusion set",
            not_yet_screened=counts["not_yet_screened"],
        )
    unresolved = _unresolved_conflicts(session, review_id, actor=actor)
    if unresolved.total:
        # THIS REFUSAL PREDATES THE FIX AND WAS ITSELF A BLINDING LEAK
        # (`ERS #65`, finding 1's second half — `01-…:918` in the audit).
        #
        # `unresolved_conflicts` named every outstanding conflict REVIEW-WIDE,
        # with no regard for whether the caller — who need only hold lead
        # authority, and a lead may also be an assigned reviewer for as long as
        # their own verdict is outstanding (`blinded_reads.visible_decisions`'s
        # own rule) — still owes a decision at that conflict's position. That
        # made this the one channel `screening.unblind_conflict`'s missing
        # owes-check (fixed alongside this one) did not even need: a lead with
        # `GET /conflicts` reporting `owed_hidden: 1` learned the withheld
        # `conflict_id` for free by calling `POST /exports` and reading the
        # `422`, no unblinding required to LOCATE it.
        #
        # So an id here is disclosed only if the caller does not owe its
        # position — the same predicate `list_conflicts` filters `items` on and
        # `unblind_conflict`/`adjudicate` refuse writes on. A caller who owes
        # none of `unresolved` sees the unchanged, full list; nothing about the
        # ordinary case (a lead who is not also screening) changes. The filter
        # itself now lives in `_unresolved_conflicts` (finding 1's second round:
        # `reviews.transition(closed)` called the unfiltered primitive directly
        # and reintroduced this exact leak at the sibling gate — see that
        # function's docstring), so this call site only reads the result.
        #
        # An owed conflict is not simply dropped: dropping it silently would let
        # `unresolved.total` and `len(unresolved.disclosable)` disagree with no
        # explanation, the same "the panel lies about completeness" defect
        # `list_conflicts.owed_hidden` exists to avoid. So the export is still
        # refused, the TOTAL count in the message is still honest, and
        # `owed_hidden` — same name, same meaning, review-scoped-to-this-caller
        # cardinality and nothing else — reports how many of the total are
        # withheld. A caller who owes exactly one position and reads
        # `owed_hidden: 1` alongside a `not_yet_screened` count they already
        # have from `GET /flow` can still act — record the decision they owe —
        # without this endpoint being the channel that told them where to look.
        #
        # ⚠️ AND `owed_hidden` IS NOW UNREACHABLE FROM HERE. Kept, not deleted.
        #
        # `require_no_owed_decisions` at the top of this function fires on
        # exactly the callers this branch was written for: a conflict exists only
        # where other reviewers hold current decisions at its position, so a
        # caller who OWES that position always satisfies "another subject holds a
        # current decision where you owe one" and is refused 403 before reaching
        # here. Measured — the branch's own test now asserts the 403.
        #
        # It stays because it is not dead in the FUNCTION it belongs to:
        # `_unresolved_conflicts` is shared with `reviews.transition(closed)`,
        # which has no owed-guard in front of it and where the filter is live and
        # tested. Deleting the branch here would leave this call site relying on
        # a guard above it to be exactly as wide as it is today — and narrowing
        # that guard would then reopen `ERS #65` finding 1 silently.
        detail = f"{unresolved.total} conflicts are unresolved"
        if unresolved.owed_hidden:  # pragma: no cover - unreachable behind the owed-guard; see above
            detail += (
                f"; {unresolved.owed_hidden} of them are at a position you are assigned to and have not "
                "decided, so their identity is withheld from you until you record your own decision there"
            )
        raise UnprocessableStateError(
            detail, unresolved_conflicts=unresolved.disclosable, owed_hidden=unresolved.owed_hidden
        )

    corpus = latest_corpus(session, review_id)

    included_ids = [d.review_record_id for d in result["dispositions"] if d.bucket == "included"]

    # THE VERSIONS THE CITED CORPUS CONTAINS — not the tail of the review.
    #
    # This was `current_record_versions(...)`, the max-per-group tail, which was
    # right about the defect it replaced (an unordered dict comprehension, pure
    # last-row-wins) and wrong about which version an export is a statement
    # about. The manifest below cites `corpus_version_id` and
    # `member_manifest_hash`: it says "this is the inclusion set drawn from
    # corpus X". A record named by a version that is not a member of X makes the
    # artifact internally inconsistent, and `verify_export` cannot notice —
    # it re-hashes the STORED payload and has no membership check, so it answers
    # `verified` on it.
    #
    # Unreachable until `apply_survivorship` landed, because the tail and the
    # corpus member were the same row for every record that had ever existed.
    # Measured once both were in: a merge with survivorship mints v2, the export
    # seals v2, and the corpus it cites holds only v1.
    #
    # Screening is the deciding argument, not merely consistency: an assignment
    # is minted from a corpus version and a decision is taken against the record
    # AS THE REVIEWER SAW IT. The inclusion set has to name that, or it credits a
    # judgement to content nobody judged.
    versions = corpus_member_versions(session, corpus.corpus_version_id, included_ids)

    # AND THE CORPUS MUST BE THE ONE THAT WAS SCREENED.
    #
    # `#21` made the export seal the versions its cited corpus contains, and told
    # a lead who tripped its refusal to "re-seal the corpus and re-run the
    # stage". Following that advice moved the defect rather than fixing it:
    # re-sealing mints a corpus holding the survivorship version, `latest_corpus`
    # then names it, and the export seals content NOBODY SCREENED while
    # `:verify` answers `verified`. Measured — screen v1, merge, re-seal, export:
    # `included` carried the merged title, not the one the reviewer saw.
    #
    # `screening_decisions.review_record_version_id` records the version each
    # decision was actually taken against, so the review's own data settles it.
    # (`screening_assignments.corpus_version_id` — added later, in `0009` — is
    # the assignment's corpus, not the decision's version, and is deliberately
    # NOT the anchor here: a reviewer can be reassigned, and what has to be
    # sealed is the version a decision was taken against. This comment used to
    # say the column did not exist at all, which stopped being true in `0009`.)
    #
    # Refusing, not silently re-sealing: which corpus an inclusion set describes
    # is the lead's decision, and the two honest options — export before
    # re-sealing, or re-screen against the new one — are theirs to pick.
    #
    # THE SECOND REMEDY NAMES A NEW ROUND, because the round the lead is reading
    # this in cannot be re-screened. It used to say "re-run the stage against the
    # new corpus", and following that literally is one `POST /assignments` on the
    # SAME round — which `uq_assignment_once` makes a no-op, and which answered
    # `201` echoing the corpus it had not stored. Measured on `a75da1a`:
    # 422 -> 201 (0 created, table still holding the old corpus) -> the same 422.
    # `create_assignments` now refuses that call outright and names the round to
    # use; this sentence has to name the same remedy, or the two halves of the
    # service send the lead to different places.
    #
    # A SET COMPARISON, not an equality. `_decided_versions` used to drop a
    # record whose reviewers named different versions and this read the absence
    # as "nothing to contradict" — so one reviewer citing unread content was
    # refused and two disagreeing reviewers sealed. `{v}` versus the corpus's one
    # version covers both: a single wrong version and a disagreement are the same
    # refusal, and only unanimity on the corpus's own version passes.
    decided_versions = _decided_versions(session, review_id, included_ids)
    unscreened = sorted(
        str(rid)
        for rid, decided in decided_versions.items()
        if rid in versions and decided != {versions[rid].review_record_version_id}
    )
    if unscreened:
        raise UnprocessableStateError(
            f"{len(unscreened)} included records were screened against a version that "
            f"corpus version {corpus.corpus_version_id} does not contain, or their reviewers named "
            "different versions. Export before re-sealing, or screen the new corpus in a NEW ROUND "
            "of the stage (POST /assignments with a higher round_number); sealing this would credit "
            "a decision to content nobody read.",
            record_ids=unscreened,
        )

    # Negative control #3: an included record the cited corpus does not contain
    # is a hole in the inclusion set, and the old code closed it by dropping the
    # record — `if rid in versions`. `included_count` is `len(included_payload)`,
    # so it shrank to match; the record appeared in neither `included` nor
    # `omissions`; and `verify_export` re-hashes the STORED payload, so the
    # truncated set verified. A sealed inclusion set that silently omits a record
    # and then certifies itself is the exact failure this service exists to
    # refuse.
    missing = [rid for rid in included_ids if rid not in versions]
    if missing:
        raise UnprocessableStateError(
            f"{len(missing)} included records are not members of corpus version "
            f"{corpus.corpus_version_id}; refusing to seal an inclusion set that cites a corpus it "
            "does not describe. Re-seal the corpus and re-run the stage.",
            missing_record_ids=sorted(str(rid) for rid in missing),
        )

    included_rows: list[dict[str, Any]] = [
        {
            "review_record_id": str(rid),
            "review_record_version_id": str(versions[rid].review_record_version_id),
            "content_hash": versions[rid].content_hash,
            "record_type": versions[rid].record_type,
            "title": versions[rid].title,
            "publication_year": versions[rid].publication_year,
            "doi": versions[rid].doi,
            "pmid": versions[rid].pmid,
        }
        for rid in included_ids
    ]
    included_payload = sorted(included_rows, key=lambda item: str(item["review_record_id"]))

    # `ER-009.3` — EVERY omission, explicit. An empty list would be an assertion
    # that nothing was left out; it is never NULL, and it is never abbreviated.
    record_omissions: list[dict[str, Any]] = [
        _omission_row(d) for d in result["dispositions"] if d.bucket != "included"
    ]
    quarantine_omissions: list[dict[str, Any]] = [
        {
            "import_row_id": str(row.import_row_id),
            "bucket": "quarantined",
            "quarantine_reason": row.quarantine_reason,
        }
        for row in session.execute(
            select(ImportRow).where(ImportRow.review_id == review_id, ImportRow.status == "quarantined")
        )
        .scalars()
        .all()
    ]
    # Records first, then quarantined rows; each block sorted by its own id, so
    # the list is stable across runs and diffable between exports.
    omissions = sorted(record_omissions, key=lambda item: str(item["review_record_id"])) + sorted(
        quarantine_omissions, key=lambda item: str(item["import_row_id"])
    )

    # `active_protocol` is still read twice — once inside `_collect`, once here.
    # Its second call is a SELECT plus a `session.get` that the identity map
    # serves without SQL, so it is one statement, not the nested re-read that
    # `_collect` was. Left alone deliberately: threading it out would mean
    # widening `FlowInputs`, which is the pure policy contract the conservation
    # property is tested against.
    protocol = active_protocol(session, review_id)

    # TWO ARTIFACTS, not one artifact hashed twice.
    #
    # The `manifest` is the envelope: what this export IS — which review, which
    # locked protocol, which sealed corpus, what the counts are, how many items
    # it carries, and — since `:2` — the canonical hash OF those items. It still
    # excludes the arrays themselves.
    #
    # The `payload` is the export: the manifest plus every included record and
    # every omission.
    #
    # `items_hash` is what makes the exclusion safe. Under `:1` the arrays sat
    # outside the manifest attested only by `payload_digest`, an ordinary column
    # of this table that nothing signs — so rewriting a record in place,
    # recomputing that column and leaving the counts alone reproduced every
    # check the verifier performs. The manifest is sealed; a hash of the items
    # inside it is therefore sealed too, and the substitution moves the digest.
    # It goes in the manifest and NOT beside `payload_digest` for exactly that
    # reason: a sibling column would have inherited the defect it fixes.
    #
    # An earlier version hashed the SAME object twice — RFC 8785 canonical JSON
    # and `json.dumps(sort_keys=True, separators=(",", ":"))` produce identical
    # bytes for ASCII content, so `manifest_hash` and `payload_digest` came out
    # equal and the "two hashes that must never be substituted" property was
    # decorative. `ck_export_hashes_are_distinct` refused the row, which is the
    # constraint doing exactly the job it was added for.
    manifest: dict[str, Any] = {
        "items_hash": items_hash(included_payload, omissions),
        "review_id": str(review_id),
        # `str(...)` like every other identifier in this dict, and load-bearing
        # since `0014`: the manifest is sealed by `json.dumps` below, and a
        # `uuid.UUID` is not JSON-serializable — an export would raise a
        # `TypeError` rather than produce a different hash.
        "tenant_id": str(actor.tenant_id),
        "flow_revision_number": revision.revision_number,
        "protocol_version_id": str(protocol.protocol_version_id) if protocol else None,
        "protocol_content_hash": protocol.content_hash if protocol else None,
        "corpus_version_id": str(corpus.corpus_version_id),
        "member_manifest_hash": corpus.member_manifest_hash,
        "counts": counts,
        "canonical_counts_hash": flow["canonical_counts_hash"],
        "included_count": len(included_payload),
        "omissions_count": len(omissions),
        # Whether THIS DEPLOYMENT is a rehearsal, read from configuration and
        # sealed with everything else in this dict.
        #
        # It was the literal `True`, and it was the only member of this manifest
        # with no fact behind it: `grep -rn "shadow" src/` returned that one line
        # — no setting, no env var, no review state. The schema requires the
        # member in both `:1` and `:2` and says why it is sealed rather than
        # stored beside the row: "an export that silently changed its shadow
        # status after the fact would otherwise reproduce the same hash". So the
        # constant went into `manifest_hash`, and through it into `items_hash`,
        # on an append-only table — the first real reviewer-produced inclusion set
        # would have returned 201, verified true, and permanently declared itself
        # a rehearsal. A consumer branching on `shadow_mode`, which is the entire
        # reason the member exists, discards exactly that export.
        #
        # `settings.shadow_mode` defaults to `False`, so a production pod that
        # declares nothing seals `false` — the honest answer for a real run — and
        # a rehearsal deployment is the one that has to say so. Read here, per
        # export, rather than captured at import: an export states the deployment
        # it was TAKEN on, and `get_settings` is `lru_cache`d anyway, so this
        # costs nothing.
        "shadow_mode": get_settings().shadow_mode,
        "schema_ref": EXPORT_SCHEMA_REF,
    }
    envelope: dict[str, Any] = {
        "manifest": manifest,
        "included": included_payload,
        "omissions": omissions,
    }

    manifest_hash, sealed_with = _seal_manifest(manifest, EXPORT_SCHEMA_REF)
    # sha256 of the EXACT bytes of the WHOLE document. `separators` and
    # `sort_keys` are pinned so a consumer who re-serializes the payload the
    # same way reproduces it — and it is stored alongside the payload so they do
    # not have to.
    payload_bytes = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_digest = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"

    # AN UNCHANGED REVIEW SEALS ONE EXPORT, NOT ONE PER CLICK.
    #
    # Nothing here asked whether this review had already been exported, and the
    # table has no uniqueness beyond its primary key. Measured — three POSTs on
    # an untouched review:
    #
    #     export 1: 201 id=f527e4ec manifest=sha256:cb405894... digest=sha256:ea5c49fd...
    #     export 2: 201 id=758b95ac manifest=sha256:cb405894... digest=sha256:ea5c49fd...
    #     export 3: 201 id=c4a83170 manifest=sha256:cb405894... digest=sha256:ea5c49fd...
    #     all three :verify -> true,  GET /exports count=3
    #
    # Every member of the manifest is a function of immutable state, so the
    # duplicates are byte-identical — and `inclusion_exports` is append-only, so
    # they are permanent. A consumer that cites "the inclusion set" of this
    # review is handed three indistinguishable seals and has to guess. The studio
    # makes it easy to trip (`fr`'s `reviews.ts` has no read for exports at all,
    # so its button cannot know one exists) but the hole is here: any caller mints
    # duplicates just as freely.
    #
    # Replayed, not refused, and matching the house pattern exactly —
    # `imports.seal_corpus_version`, `imports.import_payload` and
    # `deduplication.create_run` all return the existing row with
    # `replayed=True` when the inputs have not moved. It is also what the caller
    # wants: the studio gets an export back and can render it.
    #
    # Keyed on `manifest_hash`, NOT on "this review has any export". A reopen
    # bumps `flow_revision_number`, and re-screening moves the counts, so a
    # genuinely new inclusion set has a different manifest and is minted
    # normally. Only a seal that would add nothing is collapsed.
    previous = (
        session.execute(
            select(InclusionExport).where(
                InclusionExport.review_id == review_id,
                InclusionExport.manifest_hash == manifest_hash,
            )
        )
        .scalars()
        .first()
    )
    if previous is not None:
        return export_summary(previous, replayed=True)

    # AND THE SELECT ABOVE IS NOT WHAT MAKES THAT TRUE.
    #
    # It is a check-then-act, and this service runs at READ COMMITTED, where two
    # transactions both take it before either commits, both see nothing, and both
    # insert. Until `0012` there was no uniqueness on this table beyond its
    # primary key — verified against the live database, `\d inclusion_exports`
    # showed the PK and nothing else — so the collapse above was a courtesy that
    # held exactly when the second caller could already SEE the first.
    #
    # Reproduced with two threads on two sessions, the winner's transaction held
    # open across the loser's whole call
    # (`test_two_concurrent_seals_of_one_manifest_leave_one_export`): two rows for
    # one `manifest_hash`, both answering `verified: true`. And this table is
    # append-only — `trg_inclusion_exports_append_only` raises on DELETE — so the
    # twin is PERMANENT. Nobody, at any privilege this service holds, can remove
    # it. A consumer citing "the inclusion set" of the review is handed two
    # indistinguishable seals and has to guess.
    #
    # The trigger is a worker retry, two leads, two tabs, or a second pod — NOT a
    # double-click in the studio, whose seal button sets `busy` synchronously
    # before its first await and disables itself.
    #
    # ON CONFLICT DO NOTHING, then re-select: `uq_export_manifest_per_review` picks
    # the winner, and the loser's `RETURNING` is empty because its row was skipped.
    # Its re-select then runs as a NEW statement, which under READ COMMITTED takes
    # a fresh snapshot and therefore sees the winner's committed row — the row it
    # wanted. Identical in shape to `current_flow_revision` above and to
    # `screening._maybe_open_conflict`, deliberately: this is one pattern in this
    # codebase, not three.
    #
    # The constraint is what makes it correct and the `DO NOTHING` is what makes
    # it usable. The index alone would turn the loser's insert into an
    # `IntegrityError` and hand a caller a 500, or a `409 state_conflict` through
    # `_integrity_handler`, where the documented answer is `replayed: true`.
    export_id = uuid.uuid4()
    inserted = session.execute(
        pg_insert(InclusionExport)
        .values(
            export_id=export_id,
            review_id=review_id,
            tenant_id=actor.tenant_id,
            flow_revision_id=revision.flow_revision_id,
            corpus_version_id=corpus.corpus_version_id,
            schema_ref=EXPORT_SCHEMA_REF,
            manifest_hash=manifest_hash,
            sealed_with=sealed_with,
            payload_digest=payload_digest,
            payload=envelope,
            omissions=omissions,
            created_by_subject_id=subject,
        )
        .on_conflict_do_nothing(constraint="uq_export_manifest_per_review")
        .returning(InclusionExport.export_id)
    ).scalar_one_or_none()
    session.flush()

    export = session.execute(
        select(InclusionExport).where(
            InclusionExport.review_id == review_id,
            InclusionExport.manifest_hash == manifest_hash,
        )
    ).scalar_one_or_none()
    if export is None:  # pragma: no cover - the insert above either wrote it or lost to a writer who did
        raise NotFoundError("the inclusion export disappeared while being sealed")
    # `replayed` is a fact about THIS call, not about the row: the loser did not
    # seal anything, and telling it that it did is the ambiguity the whole replay
    # contract exists to remove.
    return export_summary(export, replayed=inserted is None)


def seal_mode(schema_ref: str) -> tuple[str, str | None]:
    """Which seal this deployment can actually produce, and why not the good one.

    Returns ``(mode, degradation_reason)`` with the reason ``None`` only when the
    strong seal is available. Silent — the health probe calls this on every
    readiness check, and a probe that logged would drown the one line that
    matters.

    The check is a PROBE of the configuration, run before the instance is
    involved at all, and that is the whole point. `SchemaValidationError` is
    raised both for "this ref is not in the registry" and for "this instance is
    not what the schema says" — the same class, from the same call, for a
    deployment fault and for a data bug. Branching on the exception type is
    therefore impossible, and the previous `except Exception: fall back` treated
    both as the former: an envelope the schema REJECTED was quietly sealed with a
    bare hash and reported as verified.

    So the question is asked separately. `derive_hash_profile` needs only the
    ref: it resolves the document and validates the manifest that governs the
    hash view. If it succeeds, the configuration is sound and any later failure
    is about the instance, so :func:`_seal_manifest` lets that one raise.
    """
    from research_contracts.canonical.errors import CanonicalError
    from research_contracts.canonical.hash_profile import derive_hash_profile

    try:
        derive_hash_profile(schema_ref)
    except CanonicalError as exc:
        return SEALED_WITH_CANONICAL_SHA256, f"{type(exc).__name__}: {exc}"
    return SEALED_WITH_CONTENT_HASH, None


def _seal_manifest(manifest: dict[str, Any], schema_ref: str) -> tuple[str, str]:
    """Hash the manifest, and say HOW — ``(manifest_hash, sealed_with)``.

    `compute_content_hash(instance, schema_ref)` is preferred because it runs the
    full ordered algorithm — structural validation, hash view, non-hashable
    stripping, ordered/set-like array declarations, absent-vs-null — and refuses
    an instance the schema does not accept.

    Two failures used to be one. An unresolvable `schema_ref` is a fact about
    this deployment: nothing is wrong with the export, and refusing it would
    make the service unusable for a reason the caller cannot fix. An instance the
    schema REJECTS is a bug in what we are about to seal, and sealing it anyway
    puts our signature on a document we know is malformed. The old
    `except Exception` collapsed the second into the first, and the docstring
    promised a `sealed_with` field that did not exist anywhere in the tree — so
    the fallback fired on every single export and nothing said so.

    Now only the first degrades, and it is recorded on the row and logged. Step 4
    faults (`UndeclaredArrayError`, `MissingOrderKeyError`) are the residual
    ambiguity — they are schema-annotation gaps that only surface on a particular
    instance — and they raise, which is the fail-safe direction: refusing to seal
    is recoverable, a seal that means less than it claims is not.
    """
    from research_contracts.canonical.engine import canonical_sha256, compute_content_hash

    mode, reason = seal_mode(schema_ref)
    if mode == SEALED_WITH_CANONICAL_SHA256:
        # Once per export, at WARNING, naming the ref — because "the seal is
        # weaker than the contract says" is an operator's problem and there is
        # no other surface on which it would ever be noticed.
        logger.warning(
            "export sealed WITHOUT schema validation; falling back to a bare canonical hash",
            extra={"schema_ref": schema_ref, "sealed_with": mode, "reason": reason},
        )
        return canonical_sha256(manifest), mode
    return compute_content_hash(manifest, schema_ref), mode


def _recompute_manifest_hash(
    manifest: dict[str, Any], schema_ref: str, *, sealed_with: str, expected: str
) -> tuple[str | None, str | None, str | None]:
    """Re-seal in THE MODE THE ROW WAS SEALED IN. ``(hash, mode, unavailable)``.

    Never in today's mode. This was written against the day `EXPORT_SCHEMA_REF`
    would start resolving; **that day has arrived** — it resolves now, against
    `research-contracts>=0.2.0`. Rows sealed before it recorded
    `canonical_sha256` and carry the legacy `https://` ref in their own
    `schema_ref` column, and recomputing them the strong way would fail on that
    ref and report `manifest_matches: false` — which is the verifier's word for
    "this was tampered with". A contract release would have read as a fleet-wide
    forgery of the review record. `sealed_with` is persisted precisely so that
    cannot happen, and this function is where it earns that.

    Measured after the cutover: for `urn:datacompany:inclusion-set-export:1` the two
    modes happen to AGREE, because the derived profile excludes no pointers,
    declares no arrays and hashes its one opaque subtree verbatim, so the hash
    view is the whole document — no stored digest changes value. That agreement
    is a property of this schema version and not of the design, which is exactly
    why the dispatch below is not redundant: a v2 that excluded a single pointer
    would break it silently, and the row's own record of its mode is the only
    thing standing between that and mass false tamper reports. It also never
    holds for an instance the schema REJECTS, where the weak mode still returns a
    number and the strong one refuses.

    `unknown` is the pre-column backfill: the mode was never recorded, so both
    are tried and the one that reproduces the stored hash is reported. Nothing is
    claimed when neither does.
    """
    from research_contracts.canonical.engine import canonical_sha256, compute_content_hash
    from research_contracts.canonical.errors import CanonicalError

    def strong() -> tuple[str | None, str | None]:
        try:
            return compute_content_hash(manifest, schema_ref), None
        except CanonicalError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    if sealed_with == SEALED_WITH_CANONICAL_SHA256:
        return canonical_sha256(manifest), SEALED_WITH_CANONICAL_SHA256, None
    if sealed_with == SEALED_WITH_CONTENT_HASH:
        digest, unavailable = strong()
        # A row sealed strongly that can no longer be verified strongly is NOT a
        # mismatch — the wheel moved, the export did not. Answering `None` keeps
        # the two apart, where a fallback would have called it a match.
        return digest, (SEALED_WITH_CONTENT_HASH if digest is not None else None), unavailable

    # `SEALED_WITH_UNKNOWN`, and anything else the check constraint would have
    # refused — both are "the row does not say", and both are answered the same
    # way rather than trusted.
    #
    # The weak mode is tried FIRST, deliberately. The two can produce identical
    # bytes for an instance the hash view does not touch, and when they do, the
    # row's real mode is genuinely undecidable — so the label that claims less is
    # the one to report. Attributing schema validation to a row that may never
    # have had any is the whole failure this column exists to end.
    weak_digest = canonical_sha256(manifest)
    if weak_digest == expected:
        return weak_digest, SEALED_WITH_CANONICAL_SHA256, None
    strong_digest, unavailable = strong()
    if strong_digest is not None and strong_digest == expected:
        return strong_digest, SEALED_WITH_CONTENT_HASH, None
    return (
        weak_digest,
        None,
        unavailable
        or f"sealed_with is {sealed_with!r} ({SEALED_WITH_UNKNOWN} or worse) and neither mode reproduces it",
    )


def require_export_audience(session: Session, actor: Actor, review_id: uuid.UUID, what: str) -> None:
    """Both gates every route onto a sealed inclusion set has to hold.

    ## The decision this function IS: pre-existing seals are gated on the READ

    Gating `create_export` closes the door a caller who owes a decision can walk
    through themselves. It does nothing about the seals ALREADY IN THE TABLE —
    and those are not an edge case: the ordinary, correct way an export comes to
    exist is a lead who owes nothing sealing the review, which no guard here
    would ever have refused. The row that lead leaves behind carries
    `manifest.counts` verbatim and an `omissions` array naming every record's
    terminal bucket and exclusion reason, and `GET /exports/{id}` serves the
    payload by default.

    So the reads are gated too, and the alternatives were both worse:

    * **Gate only the write.** The leak survives in every review that has ever
      been sealed, which is the population that matters, and it survives
      PERMANENTLY: `trg_inclusion_exports_append_only` refuses DELETE, so the
      row cannot be withdrawn by anyone at any privilege this service holds.
    * **Redact the stored payload on the way out.** The bytes are what
      `payload_digest` and `manifest_hash` attest; serving a subset of them
      under the same `export_id` produces an artifact that fails its own
      `:verify` and teaches a consumer to distrust the seal. `verify_export`'s
      whole design is that a mismatch means tampering.

    Gating the read is not retroactive punishment of an old seal, because the
    condition is a property of the CALLER and not of the row: it fires only
    while another reviewer holds a current decision at a position this caller
    still owes, and it lifts the moment they record their own — the same rule
    `blinded_reads.visible_decisions` states for `GET /decisions`. A lead who is
    not screening is unaffected, which is the ordinary case and the whole reason
    the export routes exist.

    `:verify` is gated on the same terms even though its body is hashes and
    booleans. What it discloses is that a seal EXISTS and reproduces, and an
    export can only be taken once every record is screened and every conflict
    resolved — so on a small review that answer is the partner's progress, which
    is the disclosure `ER-004.2` names and `routers/exports.py` already refuses
    to make through `list_exports`.

    Held here rather than three times in the router for the reason this module
    keeps relearning: a gate a router holds reads as no gate at all from the
    service. The two reads below take their query with them for the same reason.
    """
    require_review_lead(actor, load_review(session, review_id))
    require_no_owed_decisions(session, actor, review_id, what)


def list_exports(session: Session, actor: Actor, review_id: uuid.UUID) -> dict[str, Any]:
    """Every seal this review holds, oldest first. Gated, including the count.

    The EXISTENCE of an export is progress information in its own right — see
    `routers/exports.py`, which is where that argument is written out — so an
    empty list is not a safe degradation of this response and the whole route is
    refused rather than filtered.
    """
    require_export_audience(session, actor, review_id, "reading this review's sealed inclusion sets")
    rows = (
        session.execute(
            select(InclusionExport).where(InclusionExport.review_id == review_id).order_by(InclusionExport.created_at)
        )
        .scalars()
        .all()
    )
    return {"items": [export_summary(e) for e in rows], "count": len(rows)}


def get_export(
    session: Session, actor: Actor, review_id: uuid.UUID, export_id: uuid.UUID, *, with_payload: bool = True
) -> dict[str, Any]:
    """One seal. `with_payload=False` is a smaller body, NOT a weaker gate.

    The summary alone carries `included_count` and `omissions_count`, and at a
    denominator of one those two integers are the other reviewer's verdict
    spelled as a scalar. So the guard is held before the row is loaded, not
    around the payload.
    """
    require_export_audience(session, actor, review_id, "reading a sealed inclusion set")
    export = session.get(InclusionExport, export_id)
    if export is None or export.review_id != review_id:
        raise NotFoundError("no such export for this review")
    return export_summary(export, with_payload=with_payload)


def verify_export(session: Session, actor: Actor, review_id: uuid.UUID, export_id: uuid.UUID) -> dict[str, Any]:
    """Round-trip the seal, never trusting the stored hash (`DM-AT-29`).

    Every hash is recomputed from the stored payload and compared. A single
    tampered byte changes `payload_digest`; a semantically-equivalent
    reserialization does not, which is exactly why both exist. Since
    `inclusion-set-export:2` there is a third — `items_hash`, recomputed from
    the stored arrays and compared against the value inside the SEALED manifest,
    which is what finally binds the envelope to its cargo.

    **`manifest_matches` is not a claim about the schema.** It used to be read as
    one. The verifier recomputed through the same silent fallback the writer
    used, so two bare canonical hashes agreed and it answered `true` — for an
    envelope no schema had ever seen. `schema_validated` is the field that says
    whether anything was checked against a declared shape, and it is `false`
    whenever either end of the round trip was a bare hash.

    ## Three outcomes, because there are three

    `_recompute_manifest_hash` goes to some length to keep "could not check" and
    "did not match" apart — it answers `None` for the first, and its docstring
    says why: a row sealed strongly that can no longer be verified strongly is
    NOT a mismatch, the wheel moved and the export did not. This function then
    collapsed the distinction on the way out. `recomputed is not None and
    recomputed == stored` turns `None` into `False`, and an UNVERIFIABLE row was
    served the same `manifest_matches: false` / `verified: false` as a genuinely
    TAMPERED one. Measured side by side, the only fields that differed were
    `verified_with`, `recomputed_manifest_hash` and `seal_unavailable_reason` —
    none of which is the field a consumer branches on.

    That is the same over-claim as the seal itself, pointed the other way:
    reporting evidence of forgery where the honest answer is "this deployment
    cannot tell". It is not a harmless conservatism either. "Tampered" is an
    accusation about an evidence artefact in a regulated review, and the remedy
    is an investigation; "unverifiable" is a fact about the installed contract
    wheel, and the remedy is to install the right one.

    So the response says which of the three it is:

    * ``outcome: "verified"`` — both hashes reproduced, in the mode the row
      records. `verified` is ``true``.
    * ``outcome: "tampered"`` — something that CAN be checked did not reproduce.
      `verified` is ``false``.
    * ``outcome: "unverifiable"`` — the manifest could not be recomputed in the
      row's own mode, and nothing else contradicts the row. `verified` is
      ``null``, and `seal_unavailable_reason` says why.

    `verified` is a tri-state rather than a fourth boolean because every existing
    consumer reads it as the answer, and `null` is the only value that is neither
    "yes" nor "no" while remaining falsy: a client testing `if (!verified)` keeps
    failing closed, and a client testing `verified === false` — the one that
    would raise a tamper alarm — stops firing on a missing schema.

    `manifest_matches` becomes `null` in the same case, for the same reason and
    deliberately rather than silently: it is the field that carried the false
    claim, and leaving it `false` would keep the lie available to anyone who did
    not adopt `outcome`. Its meaning where a check WAS possible is unchanged, so
    every existing assertion about it still holds.

    Note that `payload_matches` is never `null`: `payload_digest` is a bare
    sha256 over the stored bytes and is always recomputable. Because `manifest`
    is a sub-object OF `payload`, real tampering with the manifest moves the
    payload digest too — so the tamper detection does not depend on the manifest
    branch, and an unverifiable manifest never hides a modified document.

    `items_match` IS `null` for a row sealed under `:1`, and that is a fourth
    reading rather than a fourth outcome: the row is still `verified`, it simply
    attests less than a `:2` row does. What it does not do is accuse. Every
    export written before the item seal existed would otherwise report a failed
    check for having been written on time.

    Gated by :func:`require_export_audience` like the other two reads, which is
    why it takes an `actor` it otherwise has no use for. See that function for
    why a body made of hashes and booleans is still a disclosure.
    """
    require_export_audience(session, actor, review_id, "verifying a sealed inclusion set")
    export = session.get(InclusionExport, export_id)
    if export is None or export.review_id != review_id:
        raise NotFoundError("no such export for this review")

    recomputed_manifest, verified_with, unavailable = _recompute_manifest_hash(
        export.payload.get("manifest", {}),
        export.schema_ref,
        sealed_with=export.sealed_with,
        expected=export.manifest_hash,
    )
    payload_bytes = json.dumps(export.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    recomputed_digest = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"

    # `verified_with`, not `recomputed_manifest is not None`, is what makes the
    # manifest checkable. The `unknown` backfill path returns a digest it could
    # not attribute to a mode — a number with no claim attached — and comparing
    # against it would be exactly the guess this whole column exists to end. An
    # `unknown` row that NEITHER mode reproduces therefore reports
    # `unverifiable`, with `seal_unavailable_reason` saying literally that; the
    # row's mode was never recorded, so "the hash for its mode is wrong" is not a
    # statement anything here is entitled to make.
    manifest_verifiable = verified_with is not None
    manifest_matches = (recomputed_manifest == export.manifest_hash) if manifest_verifiable else None
    payload_matches = recomputed_digest == export.payload_digest

    # The item arrays are OUTSIDE the manifest, and `payload_digest` is an
    # ordinary sibling column that nothing seals. So the two checks above,
    # together, do not bind the manifest to the items: edit `payload["included"]`,
    # recompute `payload_digest`, and both still agree.
    #
    # The manifest does carry `included_count` and `omissions_count`, and it IS
    # sealed. Cross-checking them against the arrays they claim to count turns
    # the crudest form of that tamper — adding or removing records — into a
    # detectable one, using only what `:1` already seals. It does NOT make the
    # seal item-sensitive: rewriting a record IN PLACE passes this check, which
    # is what `items_match` below is for. Reported as its own boolean rather than
    # folded into `payload_matches`, because a reader is entitled to know which
    # check failed.
    manifest_obj = export.payload.get("manifest", {})
    counts_match = manifest_obj.get("included_count") == len(export.payload.get("included", [])) and manifest_obj.get(
        "omissions_count"
    ) == len(export.payload.get("omissions", []))

    # THE ITEM SEAL — `inclusion-set-export:2`.
    #
    # Recomputed from the stored arrays through the same one function that wrote
    # it, and compared against the value inside the SEALED manifest. That is what
    # makes it worth anything: `manifest_matches` above already proved the
    # manifest reproduces its own hash, so an attacker who rewrites a record must
    # also rewrite `items_hash`, which moves `manifest_hash`, which they cannot
    # move without the check above failing. The two together bind the envelope to
    # its cargo.
    #
    # `null`, not `false`, for a row sealed under `:1` — the same tri-state
    # discipline `manifest_matches` follows, and for the same reason. A `:1`
    # export has no `items_hash` because its contract has no such member; that is
    # a fact about the contract it was sealed under, not evidence of tampering,
    # and reporting `false` would accuse every export written before today.
    # `outcome` stays `verified` for such a row: it attests less, and
    # `schema_ref` on the response is what says so.
    #
    # A `:2` row with the member MISSING is a different matter and is `false`:
    # the contract requires it, so its absence is either a forged manifest or an
    # export this service could not have written.
    if export.schema_ref == EXPORT_SCHEMA_REF_V1:
        items_match = None
        recomputed_items = None
    else:
        recomputed_items = items_hash(export.payload.get("included", []), export.payload.get("omissions", []))
        items_match = manifest_obj.get("items_hash") == recomputed_items

    if manifest_matches is False or not payload_matches or not counts_match or items_match is False:
        outcome, verified = "tampered", False
    elif not manifest_verifiable:
        outcome, verified = "unverifiable", None
    else:
        outcome, verified = "verified", True

    return {
        "export_id": str(export_id),
        "schema_ref": export.schema_ref,
        # How the row says it was sealed, and how this run could check it. When
        # they disagree the round trip proved less than the row claims, and both
        # are reported rather than reconciled.
        "sealed_with": export.sealed_with,
        "verified_with": verified_with,
        "schema_validated": manifest_matches is True
        and verified_with == SEALED_WITH_CONTENT_HASH
        and export.sealed_with == SEALED_WITH_CONTENT_HASH,
        "seal_unavailable_reason": unavailable,
        "manifest_hash": export.manifest_hash,
        "recomputed_manifest_hash": recomputed_manifest,
        # The field to branch on. The three booleans below are the evidence it
        # was derived from, and they are kept so a reader can check the work.
        "outcome": outcome,
        "manifest_verifiable": manifest_verifiable,
        "manifest_matches": manifest_matches,
        "payload_digest": export.payload_digest,
        "recomputed_payload_digest": recomputed_digest,
        "payload_matches": payload_matches,
        # Whether the sealed manifest's counts still describe the arrays beside
        # them. False means records were added or removed after sealing.
        "counts_match": counts_match,
        # Whether the sealed manifest's `items_hash` still describes the arrays
        # beside it. False means a record was SUBSTITUTED — the tamper no count
        # can see. `null` means the row was sealed under `:1`, whose contract has
        # no such member; see the derivation above.
        "items_match": items_match,
        "items_hash": manifest_obj.get("items_hash"),
        "recomputed_items_hash": recomputed_items,
        "verified": verified,
    }


def _unresolved_conflict_ids(session: Session, review_id: uuid.UUID) -> list[str]:
    """Conflicts that still need a lead — adjudicated OR resolved by consensus.

    ## Why "no adjudication" is not the same question

    A conflict is opened when the reviewers' CURRENT decisions disagree, and
    `screening_conflicts` is append-only: `0005_er_m005_screening.py:229` says
    *"resolution is an adjudication row, not a status update"*, so the row can
    never be retracted. That was sound while a correction was invisible to
    conflict detection. It is not sound now that `blinded_reads._is_current`
    makes the current decision really current: a reviewer who mis-clicks
    `uncertain` on an agreed record and immediately corrects it back opens a
    conflict that no adjudication will ever be written for, and
    :func:`create_export` refuses the review forever with *"1 conflicts are
    unresolved"* — over a disagreement that does not exist.

    So the question this function asks is the one the export gate actually
    needs: **is there still something for a lead to decide?** Two ways there is
    not, and both are checked here:

    * a lead has adjudicated it (the pre-existing arm, unchanged); or
    * the reviewers' current decisions are UNANIMOUS again. That is consensus,
      which is how a systematic review is meant to resolve most disagreements —
      discussion first, third party only if it fails — and it needs no
      adjudicator by construction.

    ## What this does NOT do

    It does not delete, retract or hide the conflict. The row stays, `opened_at`
    stays, `GET /conflicts` still lists it, and the decision chain that opened
    and closed it is fully readable — the disagreement remains in the audit
    trail. What changes is only whether it still BLOCKS, and that is derived on
    every read rather than stored, which matches the table's own description
    (`# ---- conflicts: derived, immutable`).

    ## The one-vote case is deliberately still unresolved

    Unanimity is computed over the votes that exist. If a reviewer's disagreement
    is "resolved" by there being only one current decision left on the record,
    `len(votes) == 1` would be true for the wrong reason — so a record with fewer
    votes than `reviewers_required` stays unresolved. It is also
    `not_yet_screened` in the ledger, and `create_export` refuses on that first.

    ## ⚠️ Measured: this gate now fires in NO single-stage flow, and that is why

    Run against the commit before this change, one review, two reviewers, one
    stage — `export` is `POST /exports`, `nys` is `counts["not_yet_screened"]`:

        all agree, no conflict            export=201  conflicts=0  nys=0
        A corrects into disagreement      export=422  conflicts=1  nys=1
                                          -> "1 records are not yet screened"
        A corrects back to agreement      export=422  conflicts=1  nys=0
                                          -> "1 conflicts are unresolved"

    `flow_derivation` calls a record screened only when an adjudication exists
    OR `reviewers_required` current decisions agree, so **every** live
    disagreement is already `not_yet_screened` and refused by the gate above
    this one. The ONLY state that ever reached this line was the third row —
    a conflict row outstanding while the reviewers agree — which is precisely
    the state this change now lets through.

    ## ⚠️ And in a MULTI-stage review it fires for a reachable, resolvable state

    The paragraph that used to sit here said the two derivations "are not
    provably identical outside that single-stage shape" because `_collect`
    aggregated votes by `review_record_id` across every stage while a conflict is
    keyed `(stage_id, review_record_id, round_number)`, and called the difference
    a shape neither derivation was designed against. `_collect` is stage keyed
    now, so the mismatch it described is gone — but the backstop is MORE
    load-bearing than before, not less, and the reason has become concrete:

    a record can be settled by its FULL-TEXT stage while a title/abstract
    disagreement on it is still open and unadjudicated. `derive` settles from the
    record's highest decided position and reports `not_yet_screened: 0`; this
    function still sees the stage-one conflict and refuses. That refusal is
    right — an export is a statement that the review's disagreements were
    resolved, and one was not — and, unlike the state this whole change exists to
    remove, it is ESCAPABLE: `GET /conflicts` names the conflict and `:resolve`
    takes its id. The two gates are asking different questions, and this is the
    only one that asks about the audit trail rather than about the counts.

    Do NOT delete it.

    ## This is the UNFILTERED primitive — do not put its output in a response

    This returns every review-wide unresolved id with no regard for who is
    asking. It has exactly one legitimate caller for the raw list:
    `screening.list_conflicts`, which needs full membership to test against a
    row set it has ALREADY filtered its own, broader way (`still_owed` plus the
    unblinding carve-out) — it never re-exposes this function's ids directly,
    only a count. Every other caller wants `_unresolved_conflicts` below, the
    actor-aware wrapper that removes ids the caller owes a decision at before
    the caller ever gets to look at the list — see ERS #65's second finding for
    the disclosure this split exists to make impossible rather than
    not-currently-done: `reviews.transition(closed)` reached this primitive
    directly and put the untouched list straight into a 422 body.
    """
    from application.services.screening import current_adjudications

    conflicts = list(
        session.execute(select(ScreeningConflict).where(ScreeningConflict.review_id == review_id)).scalars().all()
    )
    if not conflicts:
        return []

    protocol = active_protocol(session, review_id)
    fallback = protocol.reviewers_required if protocol else 2
    # PER STAGE, the same expression the ledger uses.
    #
    # `#42` moved `derive` onto `stage.reviewers_required or <protocol>` and said
    # "the two cannot disagree". This gate was not moved with it, and the two did
    # disagree: `screening._maybe_open_conflict` opens a conflict on the STAGE's
    # number, so a stage declaring 2 inside a protocol declaring 3 opened its
    # conflict on two votes and then needed three agreeing ones to clear it.
    # `/flow` reported the review finished while `/exports` refused over a
    # disagreement that had been settled by consensus — the "refuses over a
    # disagreement that does not exist" state this function's own docstring
    # exists to prevent.
    #
    # The opposite direction is unreachable and stays so: a conflict only opens
    # once `len(rows) >= stage.reviewers_required`, so `cast` can never be
    # shorter than the stage's number for a conflict that exists.
    stages = _stage_positions(session, review_id)
    required_at = {stage_id: (stage.reviewers_required or fallback) for stage_id, stage in stages.items()}

    # ONE privileged read for the whole set, not one per conflict. Elevated for
    # the same reason `_collect` is: the export gate must count votes it is not
    # allowed to display, and the rows never leave this function.
    #
    # `agreement_key`, and NOT `row.disposition`. This is the THIRD place the
    # question "did these reviewers agree" is asked, and it is the one with the
    # widest blast radius: the export gate, `reviews.transition(closed)` and the
    # lead's `/conflicts` panel all read it. Measured with the other two
    # derivations moved onto the reason and this one left comparing dispositions
    # — two reviewers excluding one record for two different reasons:
    #
    #     GET /conflicts -> total=1  open=0  items[0].resolved=true
    #                       items[0].adjudication=null
    #     POST :close    -> 200
    #     GET /flow      -> not_yet_screened=1
    #
    # A conflict the panel calls resolved with nothing that resolved it, and a
    # review closed over it. `:close` gates on this function and deliberately
    # NOT on `not_yet_screened` (see `test_a_close_cannot_strand_a_disagreement`),
    # so this list is the ONLY thing standing between a reason disagreement and
    # a concluded review whose exit needs an attested reopen. Two of three is
    # worse than none of three, which is why they are one helper.
    votes: dict[tuple[uuid.UUID, uuid.UUID, int], list[tuple[str, str | None]]] = {}
    with _reading_every_reviewers_work(session):
        for row in blinded_reads.unblinded_current_decisions(session, review_id=review_id):
            votes.setdefault((row.stage_id, row.review_record_id, row.round_number), []).append(
                agreement_key(row.disposition, row.exclusion_reason_code)
            )

    # ONE batched adjudication read for the whole set. This was
    # `current_adjudication(session, conflict.conflict_id)` inside the loop —
    # a statement per conflict, on a helper the export path calls on every seal
    # and, since `list_conflicts` started borrowing it, on the lead's panel too.
    # `test_listing_conflicts_does_not_issue_a_statement_set_per_conflict`
    # measured 35 SELECTs for 16 conflicts against a bound of 12, which is how
    # this surfaced: the guard on the panel caught a cost the export path had
    # been paying silently all along.
    adjudicated = current_adjudications(session, [c.conflict_id for c in conflicts])

    out: list[str] = []
    for conflict in conflicts:
        if conflict.conflict_id in adjudicated:
            continue
        cast = votes.get((conflict.stage_id, conflict.review_record_id, conflict.round_number), [])
        # Unanimous AND complete. `len(set(...)) == 1` alone would also be true
        # of a single surviving vote, which is not consensus — see the docstring.
        # Unanimous ON `agreement_key`, so reviewers who have converged on the
        # same exclusion for the same reason clear the conflict and reviewers who
        # still differ about the reason do not.
        if len(cast) >= required_at.get(conflict.stage_id, fallback) and len(set(cast)) == 1:
            # Consensus. The conflict stays on the record; it simply no longer
            # needs a lead.
            continue
        out.append(str(conflict.conflict_id))
    return out


@dataclass(frozen=True)
class UnresolvedConflicts:
    """What a caller may be told about the review's unresolved conflicts.

    `total` is a cardinality — review-wide, safe to state to any lead, never a
    disclosure by itself. `disclosable` is the subset of ids the caller may
    additionally see BY IDENTITY: `total` with every id at a position the
    caller still owes a decision at removed. `owed_hidden` is always
    `total - len(disclosable)`, computed together so the two can never drift —
    `total` was the count `create_export`'s message quoted before this type
    existed, and it still is; only the id list moved.

    There is deliberately no field carrying the unfiltered id list. A caller
    that wants one has to go around this type and call
    `_unresolved_conflict_ids` by its own, differently-named, unfiltered
    primitive — a visible choice, not a default anyone reaches for by typing
    `.` after this object and picking the first list that appears.
    """

    total: int
    disclosable: list[str]
    owed_hidden: int


def _unresolved_conflicts(session: Session, review_id: uuid.UUID, *, actor: Actor) -> UnresolvedConflicts:
    """The disclosure-safe view of `_unresolved_conflict_ids`, filtered for `actor`.

    ERS #65 finding 1: `create_export` filtered owed ids out of its 422 and
    `reviews.transition(closed)` — the sibling gate, same predicate, same
    conflict rows — did not, so a lead who owed a position could poll `:close`
    for the id `GET /conflicts` and `POST /exports` both withheld. The fix is
    here, not at either call site: both gates ask the exact same question
    ("is this review still short of an owed decision, and which of the
    outstanding ids may THIS caller be shown"), so the filter belongs where the
    question is asked once rather than in each place it gets asked again. A
    THIRD gate that reaches `_unresolved_conflict_ids` directly gets the raw
    list and has to deliberately re-derive this filter to disclose it safely;
    a third gate that reaches this function instead gets the safe view for
    free, with no filter to remember.

    See `_unresolved_conflict_ids` for why `screening.list_conflicts` is not
    routed through here: it needs the raw membership test, not this filter —
    its own row-level filter is broader (it also carves out ids the caller was
    named in an `:unblind` audience for), and it never puts a raw id in a
    response either way, only a count.
    """
    unresolved = _unresolved_conflict_ids(session, review_id)
    if not unresolved:
        return UnresolvedConflicts(total=0, disclosable=[], owed_hidden=0)

    still_owed = blinded_reads.records_the_caller_still_owes(session, review_id=review_id, actor=actor)
    if still_owed:
        unresolved_ids = [uuid.UUID(cid) for cid in unresolved]
        positions = {
            str(row.conflict_id): (row.stage_id, row.round_number, row.review_record_id)
            for row in session.execute(
                select(ScreeningConflict).where(ScreeningConflict.conflict_id.in_(unresolved_ids))
            ).scalars()
        }
        disclosable = [cid for cid in unresolved if positions.get(cid) not in still_owed]
    else:
        disclosable = unresolved

    return UnresolvedConflicts(
        total=len(unresolved), disclosable=disclosable, owed_hidden=len(unresolved) - len(disclosable)
    )


def export_summary(export: InclusionExport, *, with_payload: bool = False, replayed: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        # True when this seal already existed and the request added nothing —
        # same spelling as `corpus_summary` and `run_summary`. A client that
        # ignores it is no worse off than before; one that reads it can tell
        # "I sealed this" from "this was already sealed".
        "replayed": replayed,
        "export_id": str(export.export_id),
        "review_id": str(export.review_id),
        "corpus_version_id": str(export.corpus_version_id),
        "schema_ref": export.schema_ref,
        "manifest_hash": export.manifest_hash,
        # Emitted on EVERY summary, not only on request. A consumer who reads
        # `schema_ref` and assumes the seal was computed against it is making
        # exactly the inference the fallback invalidates, and this is the only
        # place they would find out.
        "sealed_with": export.sealed_with,
        "payload_digest": export.payload_digest,
        "included_count": len(export.payload.get("included", [])),
        "omissions_count": len(export.omissions),
        "created_by_subject_id": export.created_by_subject_id,
        "created_at": export.created_at.isoformat() if export.created_at else None,
    }
    if with_payload:
        body["payload"] = export.payload
        body["omissions"] = export.omissions
    return body


def corpus_member_count(session: Session, corpus_version_id: uuid.UUID) -> int:
    from sqlalchemy import func

    return int(
        session.execute(
            select(func.count())
            .select_from(CorpusVersionMember)
            .where(CorpusVersionMember.corpus_version_id == corpus_version_id)
        ).scalar_one()
    )
