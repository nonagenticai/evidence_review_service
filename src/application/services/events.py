"""The outbox writer: an event committed in the same transaction as its cause.

`FND-003`. Every function here does exactly one thing — `session.add()` a
`DomainOutbox` row on the caller's session. None of them commits, none of them
flushes, and none of them may: the whole guarantee is that the event and the
domain row it describes reach the database in ONE transaction, so there is no
ordering of failures that produces one without the other. The commit belongs to
`actor_scope` (`database.py`), exactly as it does for the state itself.

## ⛔ Nothing relays these rows, so nothing is emitted

`13-…:§8.1` is a six-step path — commit to the outbox, relay to a durable log,
create per-consumer deliveries, consume with an inbox receipt, acknowledge,
replay. This service implements **step 1**. There is no relay, no delivery
record and no consumer. `research_output_service` is in the identical state.

That distinction is load-bearing rather than pedantic. `13-…:§8.3` says no
service emits a document 09 event until that event's schema, example, consumer
matrix and compatibility test are merged. The ENVELOPE is merged
(`urn:datacompany:research-event-envelope:1`, `research_contracts` 0.5.0); the
per-event payload schemas are not. Because nothing leaves this service, nothing
is emitted and the gate is not crossed. `FND-007` must release those schemas
before it builds the relay, not after.

## What a payload may say, and the one thing it must not

`13-…:§8.2` — identifiers, versions, hashes and minimal facts; consumers fetch
authorized detail from the producer. `14-…` adds the prohibition that matters
most here: **never a blinded decision.**

That is not a style note in this service. Blinding is enforced in both list and
detail queries (`09-…:§3.8`), `blinded_reads.py` exists to make a reviewer
unable to see a peer's verdict, and the production 403 that refuses an unblinded
read was measured on 2026-09-02. An event carrying `disposition` would hand a
consumer, over a channel with no blinding layer at all, the exact value the API
refuses — and it would do it permanently, because the outbox is append-only.

So `review.decision.recorded.v1` names the decision, its stage and its round.
It does not say what was decided, and it does not say who decided it. A consumer
that needs either asks this service and is answered under the blinding policy,
which is what §8.2 prescribes anyway. The same rule governs
`review.conflict.opened.v1`.

The database backs this up rather than trusting it:
`ck_domain_outbox_payload_is_minimal` refuses any string over 512 characters at
any depth, so an abstract or an extracted narrative value cannot be stored here
even by a caller that wants to.

## `aggregate_version` for a row that is written once

`13-…:§8.2` guarantees ordering per `(resource_scope, tenant, aggregate_type,
aggregate_id)` using `aggregate_version`, and the released envelope makes the
field required with no nullable escape.

Two shapes exist in this schema and they are handled differently on purpose:

* **Aggregates with a real counter** — the review lifecycle
  (`ReviewStateEvent.revision_number`) and the version chains
  (`ObservationVersion.version_number`, `ResearchAssertionVersion.version_number`)
  — pass that counter straight through.
* **Rows written exactly once** — an import batch, a deduplication run, a
  resolution, a decision, a conflict, an adjudication, an acceptance — are their
  own aggregate instance at version `1`. Superseding one of them does not
  rewrite it; it writes a NEW row with a NEW id, which is a new aggregate and
  gets its own event. The chain is expressed by the `supersedes_*` reference in
  the payload, not by the envelope's version.

The cost of that second choice is stated rather than hidden: a consumer wanting
the order of a supersession chain reads the payload's `supersedes_*` ref and
cannot get it from `aggregate_version`. Buying that back means a counter column
on the aggregate, which belongs to the aggregate's own migration and not to the
envelope.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from domain.policies.event_catalog import NOT_YET, REGISTERED_EVENTS
from models.foundation import PRODUCER, DomainOutbox

__all__ = [
    "emit_assertion_recorded",
    "emit_conflict_opened",
    "emit_conflict_resolved",
    "emit_decision_recorded",
    "emit_duplicate_cluster_proposed",
    "emit_duplicate_cluster_resolved",
    "emit_import_completed",
    "emit_observation_accepted",
    "emit_observation_superseded",
    "emit_research_object_approved",
    "emit_review_created",
    "emit_review_lifecycle",
]

#: `to_state` -> event, for the transitions whose event does not depend on where
#: they came from. `active` is deliberately absent: arriving at `active` is an
#: activation, a resume or a reopen depending on the state left behind, and
#: collapsing the three would tell a consumer that a reopened review — which
#: `13-…` requires an approval attestation for — is the same fact as a resume.
_LIFECYCLE_BY_TARGET = {
    "paused": "review.paused.v1",
    "closed": "review.closed.v1",
    "archived": "review.archived.v1",
}

#: `from_state` -> event, for the three ways a review becomes `active`.
#: `draft` and `configured` are both activations: `activate()` may walk
#: draft -> configured -> active in one call, and only the second edge is the
#: activation a consumer cares about.
_ACTIVE_BY_ORIGIN = {
    "draft": "review.activated.v1",
    "configured": "review.activated.v1",
    "paused": "review.resumed.v1",
    "closed": "review.reopened.v1",
}


def _write(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    aggregate_version: int,
    tenant_id: uuid.UUID,
    payload: dict[str, Any],
    content_hash: str | None = None,
) -> None:
    """Add one envelope to the caller's transaction. Never commits, never flushes.

    The two guards below are a developer convenience, not the control —
    `ck_domain_outbox_registered_event` is the control, and it runs in the
    database where a caller cannot route around it. They exist so a typo fails at
    the callsite with the name in the message rather than as an integrity error
    at commit time, several stack frames from the mistake.

    `raise`, deliberately, and not `assert`: an assertion is stripped under
    `python -O`, so the convenience would be present in development and absent in
    production — which is the one arrangement where it can mislead.
    """
    if event_type not in REGISTERED_EVENTS:
        raise ValueError(f"{event_type} is not a registered event")
    if event_type in NOT_YET:
        raise ValueError(f"{event_type} has no operation in this service that can raise it: {NOT_YET[event_type]}")
    session.add(
        DomainOutbox(
            event_id=uuid.uuid4(),
            event_type=event_type,
            producer=PRODUCER,
            resource_scope="tenant",
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            payload=payload,
            content_hash=content_hash,
        )
    )


# ------------------------------------------------------------------ reviews ---


def emit_review_created(session: Session, review: Any) -> None:
    _write(
        session,
        event_type="review.created.v1",
        aggregate_type="review",
        aggregate_id=review.review_id,
        # The review aggregate's revision counter lives on `ReviewStateEvent`,
        # and `create_review` writes revision 1 beside the review itself.
        aggregate_version=1,
        tenant_id=review.tenant_id,
        payload={
            "review_id": str(review.review_id),
            "project_id": review.project_id,
            "external_key": review.external_key,
        },
    )


def emit_review_lifecycle(session: Session, event: Any, *, from_state: str, to_state: str) -> None:
    """One emit point for six of the twenty-one events.

    Every lifecycle edge in this service goes through `reviews.transition`,
    including `activate`, which delegates to it. Wiring the six separately would
    have meant six chances to miss one; keying off the edge means a new legal
    transition with no event here is a `KeyError` at the callsite rather than
    silence.

    `draft -> configured` deliberately raises nothing: it is not one of the
    twenty-one, and inventing a name for it would put an unregistered event past
    the database's closed CHECK.
    """
    event_type = _ACTIVE_BY_ORIGIN.get(from_state) if to_state == "active" else _LIFECYCLE_BY_TARGET.get(to_state)
    if event_type is None:
        return
    _write(
        session,
        event_type=event_type,
        aggregate_type="review",
        aggregate_id=event.review_id,
        aggregate_version=event.revision_number,
        tenant_id=event.tenant_id,
        payload={
            "review_id": str(event.review_id),
            "from_state": from_state,
            "to_state": to_state,
            "state_event_id": str(event.state_event_id),
        },
    )


# ------------------------------------------------------------------ imports ---


def emit_import_completed(session: Session, batch: Any) -> None:
    _write(
        session,
        event_type="review.import.completed.v1",
        aggregate_type="import_batch",
        aggregate_id=batch.import_batch_id,
        aggregate_version=1,
        tenant_id=batch.tenant_id,
        payload={
            "review_id": str(batch.review_id),
            "import_batch_id": str(batch.import_batch_id),
            "accepted_count": batch.accepted_count,
            "rejected_count": batch.rejected_count,
            "quarantined_count": batch.quarantined_count,
            "raw_payload_digest": batch.raw_payload_digest,
        },
    )


# ------------------------------------------------------------ deduplication ---


def emit_duplicate_cluster_proposed(session: Session, run: Any) -> None:
    """One event per RUN, not per cluster.

    `09-…:§3.7` names the event in the singular, but a run that proposes 400
    clusters would then commit 400 rows in one transaction to say one thing. The
    run is the decision a consumer reacts to; `cluster_count` tells it how much
    there is to fetch, and it fetches under the producer's authorization.
    """
    _write(
        session,
        event_type="review.duplicate_cluster.proposed.v1",
        aggregate_type="deduplication_run",
        aggregate_id=run.dedup_run_id,
        aggregate_version=1,
        tenant_id=run.tenant_id,
        payload={
            "review_id": str(run.review_id),
            "dedup_run_id": str(run.dedup_run_id),
            "corpus_version_id": str(run.corpus_version_id),
            "cluster_count": run.cluster_count,
            "pair_count": run.pair_count,
            "input_digest": run.input_digest,
        },
    )


def emit_duplicate_cluster_resolved(session: Session, resolution: Any) -> None:
    _write(
        session,
        event_type="review.duplicate_cluster.resolved.v1",
        aggregate_type="duplicate_resolution",
        aggregate_id=resolution.resolution_id,
        aggregate_version=1,
        tenant_id=resolution.tenant_id,
        payload={
            "review_id": str(resolution.review_id),
            "resolution_id": str(resolution.resolution_id),
            "cluster_id": str(resolution.cluster_id),
            "resolution_type": resolution.resolution_type,
            "survivor_record_id": _opt(resolution.survivor_record_id),
            "supersedes_resolution_id": _opt(resolution.supersedes_resolution_id),
        },
    )


# ---------------------------------------------------------------- screening ---


def emit_decision_recorded(
    session: Session,
    decision: Any,
    *,
    review_id: uuid.UUID,
    tenant_id: uuid.UUID,
    stage_id: uuid.UUID,
    round_number: int,
) -> None:
    """⛔ Carries NO disposition and NO reviewer. See the module docstring.

    This is the one event where the payload rule and the blinding rule point at
    the same field, and the blinding rule wins: the outbox has no blinding layer
    and is append-only, so a `disposition` written here is a peer's verdict
    disclosed permanently over a channel that cannot take it back.
    """
    _write(
        session,
        event_type="review.decision.recorded.v1",
        aggregate_type="screening_decision",
        aggregate_id=decision.decision_id,
        aggregate_version=1,
        tenant_id=tenant_id,
        payload={
            "review_id": str(review_id),
            "decision_id": str(decision.decision_id),
            "stage_id": str(stage_id),
            "round_number": round_number,
        },
    )


def emit_conflict_opened(
    session: Session,
    conflict_id: uuid.UUID,
    *,
    review_id: uuid.UUID,
    tenant_id: uuid.UUID,
    stage_id: uuid.UUID,
    round_number: int,
) -> None:
    """⛔ Names the conflict, never the dispositions that made it one.

    A conflict exists precisely because two reviewers disagreed; publishing the
    two dispositions beside it would unblind both at once, which is the exact
    disclosure `unblind_conflict` exists to make a recorded, authorized act.
    """
    _write(
        session,
        event_type="review.conflict.opened.v1",
        aggregate_type="screening_conflict",
        aggregate_id=conflict_id,
        aggregate_version=1,
        tenant_id=tenant_id,
        payload={
            "review_id": str(review_id),
            "conflict_id": str(conflict_id),
            "stage_id": str(stage_id),
            "round_number": round_number,
        },
    )


def emit_conflict_resolved(session: Session, adjudication: Any) -> None:
    """Carries the disposition, deliberately, and that is not an inconsistency.

    An adjudication is the act that ENDS the blinding for its conflict: it is
    recorded by a named adjudicator who was authorized to unblind, and the
    resulting disposition is the review's answer rather than a reviewer's
    private verdict. `14-…` forbids carrying blinded decisions; an adjudicated
    one is no longer blinded.
    """
    _write(
        session,
        event_type="review.conflict.resolved.v1",
        aggregate_type="screening_adjudication",
        aggregate_id=adjudication.adjudication_id,
        aggregate_version=1,
        tenant_id=adjudication.tenant_id,
        payload={
            "review_id": str(adjudication.review_id),
            "adjudication_id": str(adjudication.adjudication_id),
            "conflict_id": str(adjudication.conflict_id),
            "disposition": adjudication.disposition,
            "supersedes_adjudication_id": _opt(adjudication.supersedes_adjudication_id),
        },
    )


# ----------------------------------------------------------------- evidence ---


def emit_observation_superseded(session: Session, observation: Any, version: Any) -> None:
    """Only the correction path. A first write is not a supersession.

    `09-…:§3.7` names `review.observation.superseded.v1` and
    `review.observation.accepted.v1` and no "created" event, so a version 1
    write raises nothing here. That is the document's shape, not an omission:
    an observation becomes a fact a consumer may use when it is ACCEPTED, and
    the acceptance event is where that is said.
    """
    _write(
        session,
        event_type="review.observation.superseded.v1",
        aggregate_type="observation",
        aggregate_id=observation.observation_id,
        aggregate_version=version.version_number,
        tenant_id=version.tenant_id,
        content_hash=version.content_hash,
        payload={
            "review_id": str(version.review_id),
            "observation_id": str(observation.observation_id),
            "observation_version_id": str(version.observation_version_id),
            "version_number": version.version_number,
            "supersedes_version_id": _opt(version.supersedes_version_id),
        },
    )


def emit_assertion_recorded(session: Session, assertion: Any, version: Any) -> None:
    _write(
        session,
        event_type="review.assertion.recorded.v1",
        aggregate_type="research_assertion",
        aggregate_id=assertion.research_assertion_id,
        aggregate_version=version.version_number,
        tenant_id=version.tenant_id,
        content_hash=version.content_hash,
        payload={
            "review_id": str(version.review_id),
            "research_assertion_id": str(assertion.research_assertion_id),
            "research_assertion_version_id": str(version.research_assertion_version_id),
            "assertion_key": assertion.assertion_key,
            "version_number": version.version_number,
            "supersedes_version_id": _opt(version.supersedes_version_id),
        },
    )


#: `subject_kind` -> the acceptance event `09-…:§3.7` names for it.
#: `extraction_dataset_version` is absent because the document names no event
#: for accepting a dataset, and the database's closed CHECK would refuse an
#: invented one. Recording the absence here is what keeps it a decision.
_ACCEPTANCE_EVENTS = {
    "research_object_version": "review.research_object.approved.v1",
    "observation_version": "review.observation.accepted.v1",
}


def emit_acceptance(session: Session, acceptance: Any, *, subject_kind: str, decision: str) -> None:
    """Two of the twenty-one, keyed by what was accepted.

    A `withdraw` raises nothing. `09-…:§3.7` names `approved` and `accepted` and
    no withdrawal event, and reusing the acceptance name for its reversal would
    tell a consumer the opposite of what happened — the worst available failure
    for an append-only record nobody can correct afterwards.
    """
    if decision != "accept":
        return
    event_type = _ACCEPTANCE_EVENTS.get(subject_kind)
    if event_type is None:
        return
    _write(
        session,
        event_type=event_type,
        aggregate_type="evidence_acceptance",
        aggregate_id=acceptance.acceptance_id,
        aggregate_version=1,
        tenant_id=acceptance.tenant_id,
        content_hash=acceptance.subject_content_hash,
        payload={
            "review_id": str(acceptance.review_id),
            "acceptance_id": str(acceptance.acceptance_id),
            "subject_kind": subject_kind,
            "supersedes_acceptance_id": _opt(acceptance.supersedes_acceptance_id),
        },
    )


#: Kept as aliases so the module surface names the two events it can raise
#: rather than one function with a hidden branch.
emit_research_object_approved = emit_acceptance
emit_observation_accepted = emit_acceptance


def _opt(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def emit_evidence_snapshot_created(session: Session, snapshot: Any) -> None:
    """`review.evidence_snapshot.created.v1` — the eighteenth of the twenty-one.

    `09-…:332` is precise about when: "only terminal application of the complete
    immutable manifest emits" it. So this is called from `snapshots._build`
    AFTER the snapshot, every member and the artifact row are in the session,
    and inside the same transaction — a consumer told a snapshot exists must
    never find it half-written, and `13-…:§5.3` step 4 is what makes that
    impossible rather than unlikely.

    The payload carries the CONTENT HASH, which matters more here than anywhere
    else in this module. `14-…:359` — consumers "pin the snapshot ID/hash and
    never dereference 'latest'" — so an event that named only the id would make
    every consumer fetch before it could pin, and a consumer that fetched a
    different version than the one announced would never know.

    `gates_evaluated` rides along for the same reason it is a column: a consumer
    deciding whether it may bind this snapshot has to be able to see that three
    of the five gates `14-…:345` names did not run. Sending the id alone would
    make the incomplete gating invisible to exactly the audience it is for.
    """
    _write(
        session,
        event_type="review.evidence_snapshot.created.v1",
        aggregate_type="evidence_snapshot",
        aggregate_id=snapshot.snapshot_id,
        aggregate_version=snapshot.version_number,
        tenant_id=snapshot.tenant_id,
        content_hash=snapshot.content_hash,
        payload={
            "review_id": str(snapshot.review_id),
            "snapshot_id": str(snapshot.snapshot_id),
            "intended_use": snapshot.intended_use,
            "member_count": str(snapshot.member_count),
            "member_manifest_hash": snapshot.member_manifest_hash,
            "gates_evaluated": list(snapshot.gates_evaluated or []),
            "supersedes_snapshot_id": _opt(snapshot.supersedes_snapshot_id),
        },
    )
