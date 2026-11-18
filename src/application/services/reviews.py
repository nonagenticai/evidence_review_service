"""Review lifecycle, protocol locking and stage derivation — `ER-001`.

The lifecycle rules live in the database (see `ER-M002`). This module reads the
current state, pre-checks the transition so the caller gets a typed `409` rather
than a raw `restrict_violation`, and writes the history row. The database check
is not redundant with the pre-check: the pre-check is for the message, the
trigger is for the guarantee, and only one of them survives a second writer.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from actor import Actor, ActorRole, require_human
from api.problem_details import (
    ForbiddenError,
    IdempotencyConflictError,
    NotFoundError,
    PreconditionFailedError,
    StateConflictError,
    UnprocessableStateError,
)
from application.services import events
from models.reviews import (
    LEGAL_TRANSITIONS,
    Review,
    ReviewProtocolVersion,
    ReviewStage,
    ReviewStateEvent,
)

#: **A label, and only a label.** :data:`~.flow.LEDGER_SCHEMA_REF` used to be the
#: sibling example of that status and is no longer: its document was authored in
#: `research-contracts` 0.4.0 and the constant followed it to a URN. Read it
#: anyway — it is now the worked example of the ORDER this constant is still
#: waiting on. Author `review-protocol` first; only then switch this line.
#:
#: The docstring this replaces said "the released schema a protocol version's
#: content validates against". Nothing validates against it. There is no
#: `review-protocol` document in `research-contracts` 0.3.0 — the registry lists
#: twenty `$id`s and none of them is one, in any spelling — and this constant is
#: never passed to `compute_content_hash`, `derive_hash_profile` or the seal
#: path. `grep -rn PROTOCOL_SCHEMA_REF src/` matches this line and exactly one
#: column write. :func:`content_hash` below is a bare RFC 8785 digest of an
#: undeclared object, which is a weaker claim than "validated against a released
#: schema" and is the true one.
#:
#: This is NOT inert, because it is handed to API clients verbatim on every
#: protocol read (`api/routers/reviews.py`, `_protocol_summary`). A consumer who
#: believes the old docstring fetches the URL, gets nothing — the host has no
#: route anywhere in gitops — and cannot tell whether that means "unvalidated"
#: or "our CDN is down". The URL form is what tells them it is a documentation
#: pointer rather than something to resolve.
#:
#: When the schema is authored: author it FIRST, then switch this to a URN and
#: hash through `compute_content_hash`. Never the other way round — minting
#: `urn:datacompany:review-protocol:1` for a document the registry does not have
#: would be a worse claim than an unresolvable URL, not a better one.
#: `test_a_schema_ref_that_resolves_is_a_promise` pins this and goes RED the day
#: any of the three becomes resolvable.
PROTOCOL_SCHEMA_REF = "https://contracts.nonagentic.ai/evidence/review-protocol.v1.schema.json"


def content_hash(payload: Any) -> str:
    """RFC 8785 canonical hash, from the published contract wheel.

    `research_contracts` is the single implementation of what a canonical hash
    IS. A `json.dumps(sort_keys=True)` here would be a second answer to that
    question, and the two would disagree on exactly the inputs that matter —
    unicode escapes, float formatting, and absent-vs-null.
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(payload)


def current_state(session: Session, review_id: uuid.UUID) -> tuple[str | None, int]:
    """The tail of the append-only history: ``(state, revision)``.

    ``(None, 0)`` when the review has no history, which for a tenant-bound
    session also covers "belongs to another tenant" — RLS makes those
    indistinguishable on purpose.
    """
    row = session.execute(
        select(ReviewStateEvent.to_state, ReviewStateEvent.revision_number)
        .where(ReviewStateEvent.review_id == review_id)
        .order_by(ReviewStateEvent.revision_number.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else (None, 0)


def load_review(session: Session, review_id: uuid.UUID) -> Review:
    review = session.get(Review, review_id)
    if review is None:
        raise NotFoundError("no such review")
    return review


def create_review(
    session: Session,
    actor: Actor,
    *,
    project_id: str,
    external_key: str,
    title: str,
) -> Review:
    subject = require_human(actor)
    existing = session.execute(
        select(Review).where(
            Review.tenant_id == actor.tenant_id,
            Review.project_id == project_id,
            Review.external_key == external_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.created_by_subject_id != subject:
            # SOMEBODY ELSE'S REVIEW IS NOT AN IDEMPOTENT REPLAY OF YOURS.
            #
            # `(tenant_id, project_id, external_key)` is unique, and returning
            # the existing row on a collision was written as idempotence. It is
            # idempotence only when the caller is the one who created it. For
            # anybody else the same code did two indefensible things at once:
            #
            # * it answered `201 Created` with a review the caller had not
            #   created and does not govern — and `require_review_lead` grants on
            #   `created_by_subject_id`, so the SQUATTER keeps lead authority
            #   over it. Measured: a reviewer creates `external_key="k"`, the
            #   lead then "creates" the same key, gets a 201, and it is the
            #   reviewer who can read `/flow` and `/exports` on it;
            # * it SILENTLY DISCARDED the submitted title. The caller was told
            #   their review exists with the name they chose; it exists with
            #   somebody else's name, and nothing in the 201 says so.
            #
            # 409 is the honest answer, and it is actionable in a way the
            # original comment assumed it could not be: the caller picks another
            # `external_key`, or asks the owner. `IdempotencyConflictError`
            # rather than `StateConflictError` because nothing about the
            # review's LIFECYCLE is in conflict — the key is taken, which is
            # precisely a failed idempotent create.
            #
            # The body names the two fields the caller supplied and nothing
            # else. Not the owner's subject id, not the `review_id`: the caller
            # learns that the key is unavailable to them, which is the minimum
            # needed to act, and nothing about whose it is.
            raise IdempotencyConflictError(
                "a review with this external_key already exists in this project and was created by another subject",
                project_id=project_id,
                external_key=external_key,
            )
        # A retried create BY THE SAME SUBJECT is the same review. The title is
        # not re-applied — this is a replay, not an edit, and `PATCH /reviews`
        # is where a rename belongs (with `expected_revision`, and refused after
        # activation). Re-applying it here would make a retried request able to
        # rename a review that has since been corrected.
        return existing

    review = Review(
        review_id=uuid.uuid4(),
        tenant_id=actor.tenant_id,
        project_id=project_id,
        external_key=external_key,
        title=title,
        created_by_subject_id=subject,
    )
    session.add(review)
    session.flush()
    session.add(
        ReviewStateEvent(
            review_id=review.review_id,
            tenant_id=actor.tenant_id,
            revision_number=1,
            from_state=None,
            to_state="draft",
            rationale="review created",
            actor_subject_id=subject,
        )
    )
    session.flush()
    events.emit_review_created(session, review)
    return review


def patch_review(
    session: Session,
    actor: Actor,
    review: Review,
    *,
    title: str | None = None,
    expected_revision: int | None = None,
) -> Review:
    """Correct a review's editable fields — PRE-ACTIVATION ONLY (`§8.2`).

    Without this the first typo was permanent: `Review` is written once, there
    is no DELETE either, and the only workaround was to abandon the review and
    create a second one under a different `external_key` — leaving the typo in
    every listing forever.

    The pre-activation fence is the point. Once a review is active, decisions
    are being taken against it and its identity is cited by protocol versions,
    corpus versions and every screening decision; renaming it then would rewrite
    the label on evidence that has already been used. So this is refused after
    activation rather than allowed with an audit note.

    ## The fence had no gate

    This asked `require_human` and stopped, which is a check that the caller is a
    person — not a check that they are *this* person, so any reviewer
    authenticated into the tenant could rename any pre-activation review anybody
    had created. There is no audit row for a title change either —
    `reviews.title` is an in-place `UPDATE` and the state history records
    transitions, not edits — so the correction was both unauthorised and
    unattributable.

    ## The sentence that used to be here was false

    This paragraph used to justify the fix by asserting that "every other write
    on this resource (`:activate`, `:pause`, `:close`, the exports, the ledger)
    is `require_review_lead`". The exports and the ledger were; the LIFECYCLE was
    not. `transition`, `activate`, `create_protocol_version`,
    `lock_protocol_version` and both deduplication writes all asked
    `require_human` and stopped, exactly as this function did — measured on
    `393ae61`: a reviewer who had not created the review got `201` from
    `POST /protocol-versions`, `200` from `:lock`, `:activate`, `:pause` and
    `:close`, `201` from `POST /deduplication-runs` and `200` from a cluster
    `:resolve` with `resolution_type=merge`.

    So the docstring was not describing a neighbourhood the rename was missing
    from; it was describing a neighbourhood that did not exist, and reading it
    was enough to conclude the rest of the resource was covered. It is recorded
    rather than deleted because that is the failure mode worth remembering: a
    guard's own prose was the evidence nobody re-checked. All of those writes
    now hold the gate, each in the SERVICE function rather than in its router —
    see :func:`require_review_lead_for`.

    The gate is `require_review_lead` rather than `require_lead` for the reason
    that function documents: the person who created a review governs it whether
    or not a realm role was ever granted, and a creator who cannot fix their own
    typo is the case this method exists to solve.
    """
    require_review_lead(actor, review)
    state, revision = current_state(session, review.review_id)
    if state is None:
        raise NotFoundError("no such review")
    if state not in ("draft", "configured"):
        raise StateConflictError(
            f"a review can only be corrected before activation; this one is {state}",
            current_state=state,
            allowed_from=["draft", "configured"],
        )
    if expected_revision is not None and expected_revision != revision:
        raise PreconditionFailedError(
            f"review is at revision {revision}; caller expected {expected_revision}",
            current_revision=revision,
        )

    if title is not None and title.strip():
        session.execute(
            text("UPDATE reviews SET title = :t WHERE review_id = :r"),
            {"t": title.strip(), "r": str(review.review_id)},
        )
        session.flush()
        session.expire(review)
    return session.get(Review, review.review_id) or review


def create_protocol_version(
    session: Session,
    actor: Actor,
    review: Review,
    *,
    inclusion_criteria: dict,
    exclusion_criteria: dict,
    stage_definitions: list[dict],
    exclusion_reason_vocabulary: list[str],
    reviewers_required: int,
) -> ReviewProtocolVersion:
    """Write a new, unlocked protocol version — **lead only**.

    `require_human` was the whole check here, which let any reviewer in the
    tenant re-protocol somebody else's review. That is not an additive act: a new
    version supersedes the previous one through `supersedes_version_id`, and
    `activate` names ONE protocol version as the criteria every decision in the
    review is judged against — so an unauthorised version is a set of inclusion
    and exclusion criteria offered up for the next activation to bind decisions
    to.
    """
    subject = require_review_lead_for(session, actor, review.review_id, "creating a protocol version")
    require_review_open(session, review.review_id, "creating a protocol version")
    if not stage_definitions:
        raise UnprocessableStateError("a protocol needs at least one stage")
    if not exclusion_reason_vocabulary:
        # An empty vocabulary is not "no constraint", it is "no reason may ever
        # be given", which makes every exclusion unattributable to a criterion.
        raise UnprocessableStateError(
            "the exclusion-reason vocabulary is closed and must be non-empty; an exclusion has to name a reason from it"
        )

    previous = session.execute(
        select(ReviewProtocolVersion)
        .where(ReviewProtocolVersion.review_id == review.review_id)
        .order_by(ReviewProtocolVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()

    content = {
        "inclusion_criteria": inclusion_criteria,
        "exclusion_criteria": exclusion_criteria,
        "stage_definitions": stage_definitions,
        "exclusion_reason_vocabulary": sorted(exclusion_reason_vocabulary),
        "reviewers_required": reviewers_required,
    }
    version = ReviewProtocolVersion(
        protocol_version_id=uuid.uuid4(),
        review_id=review.review_id,
        tenant_id=actor.tenant_id,
        version_number=(previous.version_number + 1) if previous else 1,
        supersedes_version_id=previous.protocol_version_id if previous else None,
        schema_ref=PROTOCOL_SCHEMA_REF,
        content_hash=content_hash(content),
        inclusion_criteria=inclusion_criteria,
        exclusion_criteria=exclusion_criteria,
        stage_definitions=stage_definitions,
        exclusion_reason_vocabulary=sorted(exclusion_reason_vocabulary),
        reviewers_required=reviewers_required,
        created_by_subject_id=subject,
    )
    session.add(version)
    session.flush()
    return version


#: The correction window, as one statement. Mirrors `review_stages_append_only()`
#: (`0013`) clause for clause, and the trigger — not this — is the guarantee.
#: Kept in step deliberately: this read exists so the caller gets a typed 409 with
#: a message naming the reason, rather than a raw `restrict_violation` from a
#: `DELETE` the database was always going to refuse.
#:
#: Written as SQL rather than as ORM selects for one reason: `ScreeningDecision`
#: may be referenced only by `application.services.blinded_reads` (blinding layer
#: L2, `tests/security/test_blinding_layers.py`), and this is a row-existence
#: count that reads no decision content at all.
#:
#: The `screening_decisions` clause can UNDER-report — its `screening_decisions_blinded`
#: policy narrows to the caller's own rows for a non-lead — and that is safe
#: rather than merely tolerated: `assignment_id` is `NOT NULL` with a foreign key
#: to `screening_assignments`, so a decision cannot exist without an assignment,
#: and the assignments clause reads through the tenant policy alone.
_CORRECTION_WINDOW_SQL = text(
    """
    SELECT NOT (
         EXISTS (SELECT 1 FROM screening_assignments a WHERE a.review_id = :r)
      OR EXISTS (SELECT 1 FROM screening_decisions d WHERE d.review_id = :r)
      OR EXISTS (SELECT 1 FROM screening_conflicts c WHERE c.review_id = :r)
      OR EXISTS (SELECT 1 FROM inclusion_exports e WHERE e.review_id = :r)
      OR EXISTS (SELECT 1 FROM review_state_history h WHERE h.review_id = :r AND h.to_state = 'active')
    )
    """
)


def _stage_set_is_still_correctable(session: Session, review_id: uuid.UUID) -> bool:
    """True while nothing has been written that the stage set gives meaning to.

    An activation is included even though it writes no row that references a
    stage: `:activate` names ONE protocol version as the criteria every decision
    in the review is judged against, and it is recorded in the append-only
    `review_state_history`. Re-minting the stages afterwards would change what
    that recorded activation activated, with nothing saying so.

    A sealed export is included for the case the other clauses miss. An export of
    a review whose corpus is empty passes both of `create_export`'s negative
    controls — nothing is unscreened and no conflict is open — so an append-only
    artefact can exist for a review holding no assignment and no decision.
    """
    return bool(session.execute(_CORRECTION_WINDOW_SQL, {"r": str(review_id)}).scalar())


def lock_protocol_version(
    session: Session, actor: Actor, protocol_version: ReviewProtocolVersion
) -> ReviewProtocolVersion:
    """Freeze the protocol and materialise its stages — **lead only**.

    Stages are created at lock time rather than at activation because an
    assignment references a stage, and a stage that could still change shape
    after decisions exist against it would make those decisions uninterpretable.

    This is the most consequential write in the module and it had the weakest
    gate. It is IRREVERSIBLE — the immutability trigger permits the `UPDATE` only
    while `locked_at IS NULL`, so this is the last write the row ever takes, and
    there is no unlock — and it materialises the `ReviewStage` rows every
    assignment and every decision will then reference. A reviewer who could reach
    it could freeze somebody else's half-written protocol permanently, and the
    only remedy is a whole new protocol version plus a re-activation.

    The PROTOCOL VERSION row stays irreversible. The STAGE SET does not, and the
    difference is the correction window below: while the review has never been
    activated and holds no assignment, decision, conflict or sealed export,
    locking a later version replaces the stages the earlier one minted. Nothing
    references them at that point, so nothing is retroactively falsified — see
    `_stage_set_is_still_correctable` and migration `0013`.
    """
    subject = require_review_lead_for(session, actor, protocol_version.review_id, "locking a protocol version")
    require_review_open(session, protocol_version.review_id, "locking a protocol version")
    if protocol_version.locked_at is not None:
        raise StateConflictError("protocol version is already locked")

    # The lock is an UPDATE, which the immutability trigger permits only while
    # `locked_at IS NULL` — so this is the last write this row will ever take.
    session.execute(
        text(
            "UPDATE review_protocol_versions SET locked_at = now(), locked_by_subject_id = :s "
            "WHERE protocol_version_id = :pv"
        ),
        {"s": subject, "pv": str(protocol_version.protocol_version_id)},
    )

    # THE REVIEW'S STAGES MUST HAVE A TOTAL ORDER.
    #
    # `stage_order` is numbered per protocol version, `enumerate(..., start=1)`,
    # while `review_stages` is unique on `(review_id, stage_key)` alone. So
    # locking a SECOND protocol version whose stage keys differ minted stages
    # ordered 1, 2 beside the existing 1, 2 — 201 on create, 200 here, and the
    # `:activate` that follows returns 409 without undoing any of it.
    #
    # Since `#42` the ledger settles a record at the highest `StagePosition` it
    # has decisions at, and `StagePosition` breaks an order tie on `stage_key`,
    # alphabetically. `flow_derivation`'s own docstring says that tie-break
    # "carries NO meaning" and that ordering by the protocol's `version_number`
    # is "the change to make" if two protocol versions are ever screened against
    # in one review. Measured effect until that change exists: a unanimous
    # exclusion under the current protocol discarded in favour of an inclusion
    # under the superseded one, sealed at 201 with an empty `omissions` array.
    #
    # So this refuses rather than guesses, for the same reason `_position_of`
    # does — and it refuses here, at the write, because the derivation-time
    # backstop can only refuse to answer for a review that is already corrupt.
    #
    # ## The guard was over-broad, and the over-breadth was total
    #
    # `incoming_orders` is `{1..N}` and `existing_orders` always contains 1, so
    # `existing_orders & incoming_orders` is non-empty for EVERY second protocol
    # version of a review, whatever its stage keys are. Measured: a stage named
    # 'Titel/abstrakt' locked by mistake, the typo fixed and re-locked seconds
    # later, before a single record had been imported — 409, `stage_orders: [1]`,
    # and `:activate` on the corrected version 422. A protocol sharing NO stage
    # key at all with the locked one got the same 409. So a typo caught
    # immediately ended the review, and the refusal's own advice — "start a new
    # review" — is not available to the caller: `uq_reviews_project_external_key`
    # is `(tenant_id, project_id, external_key)`, so the replacement cannot carry
    # the identity the abandoned review already claimed.
    #
    # The hazard above is untouched. What is added is the window in which
    # re-minting cannot express it: while the review holds no assignment, no
    # decision, no conflict and no sealed export, and has never been activated,
    # the previous stage rows are DELETED and the new set minted in their place.
    # The review therefore never holds two stage sets at once, so no tie can
    # exist for `StagePosition` to break alphabetically — and nothing references
    # the discarded rows, because the only foreign keys into `review_stages` are
    # the three the precondition asserts empty.
    #
    # The precondition is enforced by `review_stages_append_only()` (`0013`),
    # which is the authority; this read is for the MESSAGE, the same split the
    # module docstring states for every other lifecycle pre-check. It is also why
    # `_stage_set_is_still_correctable` reads the tables rather than trusting
    # this function to be the only writer.
    existing_orders = set(
        session.execute(select(ReviewStage.stage_order).where(ReviewStage.review_id == protocol_version.review_id))
        .scalars()
        .all()
    )
    incoming_orders = set(range(1, len(protocol_version.stage_definitions) + 1))
    collisions = sorted(existing_orders & incoming_orders)
    if collisions and not _stage_set_is_still_correctable(session, protocol_version.review_id):
        raise StateConflictError(
            "this review already has stages at these positions, and the ledger orders every decision in "
            "the review by stage position — locking a second stage set would leave two stages sharing a "
            "position and an alphabetical accident deciding which one settles a record. The stage set "
            "can only be corrected while the review has never been activated and holds no assignment, "
            "decision, conflict or sealed export; this one does.",
            stage_orders=collisions,
        )
    if existing_orders:
        # EVERY stage of the review, not only the colliding positions. Deleting
        # just the collisions leaves a longer previous set's trailing stages
        # behind — two stage sets in one review, which is the state this guard
        # exists to prevent, reached with a 200 on the way in.
        #
        # Emitted as a bulk `DELETE` and flushed before the inserts below:
        # `uq_review_stage_key` is `(review_id, stage_key)`, so a re-lock that
        # reuses a key would otherwise insert against a row not yet removed.
        session.execute(text("DELETE FROM review_stages WHERE review_id = :r"), {"r": str(protocol_version.review_id)})
        session.flush()

    for order, definition in enumerate(protocol_version.stage_definitions, start=1):
        key = str(definition.get("key") or definition.get("stage_key") or f"stage-{order}")
        session.add(
            ReviewStage(
                stage_id=uuid.uuid4(),
                review_id=protocol_version.review_id,
                tenant_id=actor.tenant_id,
                protocol_version_id=protocol_version.protocol_version_id,
                stage_key=key,
                stage_order=order,
                name=str(definition.get("name") or key),
                reviewers_required=int(definition.get("reviewers_required") or protocol_version.reviewers_required),
            )
        )
    session.flush()
    session.expire(protocol_version)
    # Re-read so the caller sees `locked_at` as the database set it, rather than
    # the stale in-session value. It cannot be missing — the UPDATE above just
    # succeeded against it — but returning `| None` silently would push that
    # assumption onto every caller instead of stating it here.
    locked = session.get(ReviewProtocolVersion, protocol_version.protocol_version_id)
    if locked is None:  # pragma: no cover - the row was updated in this transaction
        raise NotFoundError("the protocol version disappeared while being locked")
    return locked


def transition(
    session: Session,
    actor: Actor,
    review: Review,
    *,
    to_state: str,
    rationale: str,
    expected_revision: int | None = None,
    protocol_version_id: uuid.UUID | None = None,
    approval_attestation_ref: str | None = None,
) -> ReviewStateEvent:
    """Append one state event — **lead only**. Backs every lifecycle command.

    `:activate`, `:pause`, `:resume`, `:close`, `:reopen` and `:archive` all
    funnel through here, so this one `require_human` was the entire authority
    story for the review lifecycle. Measured on `393ae61`: a reviewer who had not
    created the review closed it, and could have archived or reopened it.

    Closing and archiving are what make that more than an inconvenience —
    `archived` has no outgoing edge in `LEGAL_TRANSITIONS`, so a stranger could
    put a review beyond every subsequent write permanently. A reopen is worse in
    the other direction: it reactivates a review whose decisions were already
    exported, and the attestation it demands is supplied by the same caller.
    """
    subject = require_review_lead_for(session, actor, review.review_id, "a lifecycle transition")
    state, revision = current_state(session, review.review_id)
    if state is None:
        raise NotFoundError("no such review")

    # Strong-ETag semantics (`01-…:698`): a stale expected version is 412, and a
    # state the lifecycle forbids is a typed 409. Two different failures with two
    # different remedies, so they get two different statuses.
    if expected_revision is not None and expected_revision != revision:
        raise PreconditionFailedError(
            f"review is at revision {revision}; caller expected {expected_revision}",
            current_revision=revision,
        )
    if (state, to_state) not in LEGAL_TRANSITIONS:
        raise StateConflictError(f"illegal transition {state} -> {to_state}", current_state=state)
    if state == "closed" and to_state == "active" and not (approval_attestation_ref or "").strip():
        raise UnprocessableStateError(
            "a reopen is always attested; supply the FR approval attestation bound to the closed revision"
        )

    if to_state == "closed":
        # `01-...:125` — a close cannot happen with disagreements unresolved.
        # The export gate has always enforced this; `:close` never did, so a
        # review with an open, unadjudicated conflict answered 200 and closed
        # over it. Measured on 21685a6: `/conflicts` reporting `total=1 open=1`,
        # `POST /exports` refusing, and `POST :close` returning 200.
        #
        # It became sharper than a missing gate once a concluded review stopped
        # accepting writes (`require_review_open`): closing over an open conflict
        # STRANDS it. `:resolve` is a write, so the only way back to the
        # adjudication is an attested reopen — a lead can put a review into a
        # state whose exit needs an approval reference, by clicking close.
        #
        # Imported here rather than at module scope: `flow` imports this module,
        # so the dependency only runs in this direction inside the call.
        #
        # `_unresolved_conflicts` — NOT `_unresolved_conflict_ids` — because this
        # 422 puts ids into the response body, and only the actor-aware wrapper
        # filters those against what `actor` still owes a decision at. This was
        # the second half of ERS #65 finding 1: this call site used to reach the
        # unfiltered primitive directly, so a lead owing a position could poll
        # `:close` for the exact `conflict_id` `POST /exports` (fixed first,
        # same finding) withheld from them. See `flow._unresolved_conflicts`'s
        # docstring for why the filter lives there now and not here.
        from application.services.flow import _unresolved_conflicts

        unresolved = _unresolved_conflicts(session, review.review_id, actor=actor)
        if unresolved.total:
            detail = (
                f"{unresolved.total} disagreements are unresolved, and closing over them would strand them: "
                "a closed review takes no further writes, so adjudicating afterwards needs an attested "
                "reopen. Resolve them first, or reopen later and accept the attestation."
            )
            if unresolved.owed_hidden:
                detail += (
                    f" {unresolved.owed_hidden} of them are at a position you are assigned to and have not "
                    "decided, so their identity is withheld from you until you record your own decision there."
                )
            raise UnprocessableStateError(
                detail, unresolved_conflicts=unresolved.disclosable, owed_hidden=unresolved.owed_hidden
            )

    if to_state == "active" and protocol_version_id is None:
        # `ck_state_active_requires_protocol` holds for EVERY transition into
        # `active`, not only the first — an active review always names the
        # locked protocol its decisions are judged against. A resume or a reopen
        # therefore carries the protocol forward rather than being exempt: an
        # exemption would let a reopened review run against nothing, which is
        # exactly the `DM-AT-02` failure the constraint exists to catch.
        carried = active_protocol(session, review.review_id)
        if carried is None:
            raise UnprocessableStateError("this review has never been activated against a locked protocol")
        protocol_version_id = carried.protocol_version_id

    event = ReviewStateEvent(
        state_event_id=uuid.uuid4(),
        review_id=review.review_id,
        tenant_id=actor.tenant_id,
        revision_number=revision + 1,
        from_state=state,
        to_state=to_state,
        rationale=rationale,
        actor_subject_id=subject,
        protocol_version_id=protocol_version_id,
        approval_attestation_ref=approval_attestation_ref,
    )
    session.add(event)
    session.flush()
    events.emit_review_lifecycle(session, event, from_state=state, to_state=to_state)
    return event


def activate(
    session: Session,
    actor: Actor,
    review: Review,
    *,
    protocol_version_id: uuid.UUID,
    rationale: str,
    expected_revision: int | None = None,
) -> ReviewStateEvent:
    """`draft`/`configured` -> `active`, against a locked protocol. **Lead only.**

    The database refuses an unlocked or foreign protocol version anyway; this
    reads it first so the caller learns *which* precondition failed.

    The gate is held here as well as in `transition`, which is a real duplicate
    rather than defensive noise: everything below this line is an existence probe
    on `protocol_version_id`, answering 404 for "not this review's" and 422 for
    "this review's, but unlocked". Gating only in `transition` would leave a
    non-lead able to tell those two apart across the whole id space before being
    refused. `session.get` on the review is identity-mapped by the time
    `transition` re-checks, so the second call costs no query.
    """
    require_review_lead_for(session, actor, review.review_id, "activating a review")
    protocol = session.get(ReviewProtocolVersion, protocol_version_id)
    if protocol is None or protocol.review_id != review.review_id:
        raise NotFoundError("no such protocol version for this review")
    if protocol.locked_at is None:
        raise UnprocessableStateError("activation requires a LOCKED protocol version (DM-AT-02); lock it first")

    state, _ = current_state(session, review.review_id)
    if state == "draft":
        # `draft -> active` is not a legal edge. Walk the declared path rather
        # than adding a shortcut edge, so the history shows configuration
        # happening.
        transition(
            session,
            actor,
            review,
            to_state="configured",
            rationale=f"protocol {protocol.version_number} locked",
            expected_revision=expected_revision,
        )
        expected_revision = None

    return transition(
        session,
        actor,
        review,
        to_state="active",
        rationale=rationale,
        expected_revision=expected_revision,
        protocol_version_id=protocol_version_id,
    )


def active_protocol(session: Session, review_id: uuid.UUID) -> ReviewProtocolVersion | None:
    """The protocol version the review was most recently activated against."""
    row = session.execute(
        select(ReviewStateEvent.protocol_version_id)
        .where(
            ReviewStateEvent.review_id == review_id,
            ReviewStateEvent.to_state == "active",
            ReviewStateEvent.protocol_version_id.is_not(None),
        )
        .order_by(ReviewStateEvent.revision_number.desc())
        .limit(1)
    ).first()
    return session.get(ReviewProtocolVersion, row[0]) if row else None


#: The states in which a review's history may still be added to. `closed` and
#: `archived` are deliberately absent; see :func:`require_review_open`.
OPEN_STATES = frozenset({"draft", "configured", "active", "paused"})


def require_review_open(session: Session, review_id: uuid.UUID, what: str) -> str:
    """Refuse a write that would add to a CONCLUDED review's history.

    Measured on `b3f4af1`, driving the real API: after `:close`, **six** write
    endpoints answered 201 — import records, seal a corpus version, start a
    deduplication run, create assignments, record a decision, create a protocol
    version. After `:archive`, five of the six still did.

    That matters because an export is published FROM a closed review. The sealed
    artifact is immutable and stays correct, and then `GET /flow` on the same
    review reports different counts from the export taken out of it — with
    nothing anywhere saying which is current, and `:verify` answering `verified`
    for an inclusion set the live ledger contradicts.

    ## The one refusal that already existed was an accident

    `record_decision` answered 409 on the archived review in that measurement,
    which reads like a guard and is not: the reviewer had already decided that
    record in the closed round, so it was the "a correction must supersede the
    previous decision" conflict. A fresh reviewer, or a fresh record, sailed
    through. A check that fires for an unrelated reason is worse than no check —
    it makes the hole look covered.

    ## Closed is recoverable; that is what makes refusing here honest

    `closed -> active` is a legal transition and `:reopen` is a route, so the
    lead's remedy is one call and the message names it. `archived` has no
    outgoing edge, and refusing there is the whole meaning of the state.

    ## `paused` is deliberately still open, and it was measured

    A pause is a suspension, not a conclusion: `paused -> active` is legal, no
    export is published from a pause, and a reviewer part-way through a queue
    when a lead pauses would otherwise have their next decision refused with
    their work half recorded. Whether a pause should stop reviewers writing is a
    workflow decision for the people running the review, not a correctness
    defect, so it is left alone rather than quietly changed under a bug fix.
    """
    state, _ = current_state(session, review_id)
    if state is None:
        raise NotFoundError("no such review")
    if state in OPEN_STATES:
        return state
    remedy = (
        # Not just "reopen it": `transition` refuses a reopen without an approval
        # attestation bound to the closed revision, so a lead who followed the
        # short advice would land on a 422 nothing had warned them about.
        "reopen it first, which needs an approval attestation bound to the closed revision"
        if state == "closed"
        else "an archived review cannot be reopened"
    )
    raise StateConflictError(
        f"this review is {state}, so {what} would add to the history of a review that has concluded — "
        f"and an inclusion set may already have been published from it. To continue working on it, {remedy}.",
        review_state=state,
    )


def require_lead(actor: Actor) -> str:
    """Adjudication, unblinding and export are lead-only.

    Not an admin flag — a lead is still refused as adjudicator on a conflict
    they took part in (`01-…:659`), which is enforced in the database.

    Prefer :func:`require_review_lead` where a review is in scope: this form
    can only see the caller's realm role, and a person who created a review
    governs it whether or not anybody granted them that role.
    """
    subject = require_human(actor)
    if actor.role is not ActorRole.LEAD:
        raise ForbiddenError("this operation requires the review lead role")
    return subject


def require_review_lead(actor: Actor, review: Review) -> str:
    """Lead authority OVER ONE REVIEW: the realm role, or having created it.

    The realm role alone was not enough to use the product. A person who opens
    the studio, creates a review and locks its protocol could not then assign
    reviewers to it — `POST /assignments` returned 403 — because nothing had
    granted them `lead` in Keycloak. The review was theirs, fully configured,
    and unusable. That is the first thing anybody trying the stage does, and it
    stopped there.

    So the creator governs their own review. The grant is deliberately narrow:

    * it is per-review, derived from `reviews.created_by_subject_id`, and
      confers nothing on any other review;
    * it is not a role, so it cannot be inherited, delegated or widened;
    * it does not weaken separation of duty. A creator who also screened a
      record is still refused as its adjudicator by the database trigger
      (`01-…:659`), which compares subjects and knows nothing about roles.

    Tenant isolation is unaffected: `review` was loaded through an RLS-bound
    session, so a review from another tenant is never in scope here.
    """
    subject = require_human(actor)
    if actor.role is ActorRole.LEAD:
        return subject
    if review.created_by_subject_id == subject:
        return subject
    raise ForbiddenError("this operation requires the review lead role, or having created this review")


def require_review_lead_for(session: Session, actor: Actor, review_id: uuid.UUID, what: str) -> str:
    """:func:`require_review_lead`, from a review id, with an operation name.

    The id form exists so a SERVICE function can hold the gate itself instead of
    trusting its router to have held it. That distinction is not academic here:
    every route in this service already calls `load_review`, which proves only
    that the review is visible to the caller's tenant — so a check written in a
    router looks identical to no check at all when read from the service, and
    `import_payload`, `seal_corpus_version` and `patch_review` each went a whole
    review cycle without one.

    `what` only shapes the message. A caller should learn which operation was
    refused, not merely that something was.
    """
    try:
        return require_review_lead(actor, load_review(session, review_id))
    except ForbiddenError as exc:
        raise ForbiddenError(f"{what} requires the review lead role, or having created this review") from exc


def review_summaries(session: Session, reviews: Sequence[Review]) -> list[dict[str, Any]]:
    """`review_summary` for a whole page, in 2 statements instead of `1 + 3N`.

    `list_reviews` is the first screen a demo opens, and it called
    `review_summary` per row. Each of those issued three: `current_state` (the
    history tail), `active_protocol`'s id lookup, and the `session.get` that
    follows it — the identity map does not collapse the last one, because each
    review has a distinct protocol. At the default page of 50 that is **151
    statements**; the route's `limit` allows 200, which is 601.

    `selectinload` cannot fix it. `state` and `revision` are not a relationship —
    they are the tail of `review_state_history`, derived rather than stored,
    which is the whole reason there is no `state` column to disagree with.

    So: one `DISTINCT ON` over the page's review ids for the tail, and one
    `IN` read for the protocols it names. Both are independent of N, which
    `tests/api/test_review_listing_throughput.py` asserts by running two page
    sizes and requiring the statement count to be IDENTICAL — a bound that only
    got smaller would still scale.
    """
    if not reviews:
        return []

    review_ids = [r.review_id for r in reviews]

    # `DISTINCT ON (review_id) ... ORDER BY review_id, revision_number DESC` is
    # the set-wise form of `current_state` + `active_protocol` at once: the tail
    # row carries both the state and the revision, and the most recent row whose
    # `to_state = 'active'` carries the protocol. They are different rows, so
    # this is two `DISTINCT ON`s over the same table rather than one.
    tails = {
        row.review_id: (row.to_state, row.revision_number)
        for row in session.execute(
            select(
                ReviewStateEvent.review_id,
                ReviewStateEvent.to_state,
                ReviewStateEvent.revision_number,
            )
            .where(ReviewStateEvent.review_id.in_(review_ids))
            .distinct(ReviewStateEvent.review_id)
            .order_by(ReviewStateEvent.review_id, ReviewStateEvent.revision_number.desc())
        ).all()
    }

    activations = {
        row.review_id: row.protocol_version_id
        for row in session.execute(
            select(ReviewStateEvent.review_id, ReviewStateEvent.protocol_version_id)
            .where(
                ReviewStateEvent.review_id.in_(review_ids),
                ReviewStateEvent.to_state == "active",
                ReviewStateEvent.protocol_version_id.is_not(None),
            )
            .distinct(ReviewStateEvent.review_id)
            .order_by(ReviewStateEvent.review_id, ReviewStateEvent.revision_number.desc())
        ).all()
    }

    protocols: dict[uuid.UUID, ReviewProtocolVersion] = {}
    if activations:
        protocols = {
            p.protocol_version_id: p
            for p in session.execute(
                select(ReviewProtocolVersion).where(
                    ReviewProtocolVersion.protocol_version_id.in_(set(activations.values()))
                )
            )
            .scalars()
            .all()
        }

    return [
        _summary_body(
            review,
            *tails.get(review.review_id, (None, 0)),
            protocols.get(activations.get(review.review_id)) if activations.get(review.review_id) else None,
        )
        for review in reviews
    ]


def review_summary(session: Session, review: Review) -> dict[str, Any]:
    state, revision = current_state(session, review.review_id)
    protocol = active_protocol(session, review.review_id)
    return _summary_body(review, state, revision, protocol)


def _summary_body(
    review: Review, state: str | None, revision: int, protocol: ReviewProtocolVersion | None
) -> dict[str, Any]:
    return {
        "review_id": str(review.review_id),
        "tenant_id": str(review.tenant_id),
        "project_id": review.project_id,
        "external_key": review.external_key,
        "title": review.title,
        # Derived, every time, from the append-only history. There is no column
        # to disagree with.
        "state": state,
        "revision": revision,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "created_by_subject_id": review.created_by_subject_id,
        "active_protocol_version_id": str(protocol.protocol_version_id) if protocol else None,
        "reviewers_required": protocol.reviewers_required if protocol else None,
        "exclusion_reason_vocabulary": (
            json.loads(json.dumps(protocol.exclusion_reason_vocabulary)) if protocol else []
        ),
    }


def lock_review(session: Session, review_id: uuid.UUID) -> None:
    """Serialise, per review, every write whose correctness depends on another.

    Transaction-scoped advisory lock. Two writers need this: `snapshots.request_snapshot`
    and `evidence.decide_acceptance`. A snapshot's gates read whether a version
    is currently accepted; an acceptance decision changes that. The two touch
    different tables, so under READ COMMITTED nothing conflicts and both commit
    — leaving a snapshot that pins a version withdrawn a millisecond earlier,
    which `reproduce_manifest` will then report as reproducing perfectly forever,
    because it re-derives from the frozen member rows and never re-checks.

    ⚠️ A lock only ONE of two writers takes orders nothing. If a third writer
    ever changes what a snapshot gate reads, it takes this lock too.

    An advisory lock rather than `SELECT … FOR UPDATE` on the review row,
    because the thing being serialised is a DECISION about rows that may not
    exist yet — a review with no snapshot and no acceptance has nothing to lock.
    `hashtextextended` gives a stable 64-bit key from the uuid.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"review-write:{review_id}"},
    )
