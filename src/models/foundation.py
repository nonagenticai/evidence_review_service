"""ORM models for the shared foundation tables `13-…:§5.2` gives every service.

Three so far: `domain_outbox` (`ER-M019`, `FND-003`) and `durable_jobs` plus
`job_attempts` (`ER-M020`, `FND-006`). They are OPERATIONAL tables, not domain
ones — IDs, hashes, bounded facts and artifact refs, never scientific payload.

⚠️ **Corrected.** This docstring used to say `domain_outbox` "is the only table
in this schema without row-level security". That was never true of the code
below it: `0019` deliberately KEPT `FORCE ROW LEVEL SECURITY` and spends four
paragraphs saying why it declined the `§5.2` exclusion. The sentence described
`research_output_service`'s outbox, which does follow §5.2 literally, and it
would have told a reader here that a cross-tenant reader already worked. `0020`
keeps RLS on both of its tables for the same reason and adds the additive
worker policy `0019` reserved.

Like every other class in this package these are typed accessors and nothing
more: the registered-event and registered-type CHECKs, the envelope pin, the
recursive payload bound, the job transition table and the append-only and seal
triggers are declared only in `alembic/versions/`. See `models/__init__.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

#: Mirrors `ck_domain_outbox_envelope_pinned` in `0019`, which mirrors the
#: released `research_contracts` document. Three copies of one string is two too
#: many in the abstract; here the CHECK is what a bad deploy cannot route around
#: and this constant is what the service writes, and they are pinned equal by
#: `tests/unit/test_the_event_catalogue_is_the_registry.py`.
ENVELOPE_SCHEMA_REF = "urn:datacompany:research-event-envelope:1"

#: Mirrors `ck_domain_outbox_producer` in `0019`.
PRODUCER = "evidence-review"


class DomainOutbox(Base):
    """One research domain event, committed in the same transaction as its cause."""

    __tablename__ = "domain_outbox"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False, default=ENVELOPE_SCHEMA_REF)
    producer: Mapped[str] = mapped_column(Text, nullable=False, default=PRODUCER)
    resource_scope: Mapped[str] = mapped_column(Text, nullable=False, default="tenant")
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


class DurableJob(Base):
    """One long-running operation that outlives the request that asked for it.

    `ER-M020`. Operational, like `DomainOutbox` above and unlike everything in
    the other model modules: it carries IDs, hashes, bounded facts and artifact
    refs, never scientific payload. `13-…:§7.3` — "Large inputs and outputs are
    immutable artifact refs."

    Both tables keep row-level security. `0020`'s docstring records why, and what
    replaces the exclusion `13-…:§5.1` describes: an additive policy scoped to
    `evidence_review_worker`, so cross-tenant queue discovery is granted to one
    restricted role rather than taken away from everybody.

    ⚠️ ``state`` is a plain ``str`` here and a closed CHECK in the database, and
    the transition table is a trigger. This class is a typed accessor and
    nothing more — see ``models/__init__.py``. Reading a legal-transition rule
    off it would find nothing, which is the intended answer: ask
    ``domain/policies/job_catalog.py``.
    """

    __tablename__ = "durable_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")

    command_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    profile_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    pack_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    workflow_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: An identity REFERENCE. `13-…:§9.3` — a job "never retains a human
    #: credential" — is why there is no token column here and must never be one.
    originating_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    originating_actor_role: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    waiting_dependency_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconcile_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    callback_state: Mapped[str | None] = mapped_column(Text, nullable=True)

    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_manifest_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class JobAttempt(Base):
    """One execution of one job, immutable once it has ended.

    `13-…:§5.2` calls this "immutable execution-attempt history". `0020` seals a
    row on close rather than on insert, because a row that must be able to
    finish cannot be immutable from birth — history is what has already
    happened. ``ended_at`` and ``outcome`` are set together or not at all.

    ``tenant_id`` is denormalised from the job on purpose: an RLS predicate that
    joined to `durable_jobs` would be a policy whose correctness depended on
    another table's policy.
    """

    __tablename__ = "job_attempts"

    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("durable_jobs.job_id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ResearchDomainEvent(Base):
    """One event in the durable log — step 2 of `13-…:§8.1`.

    `ER-M022`. Carries the SAME `event_id` the outbox minted, because step 6
    ("replay recreates deliveries without changing `event_id`") is only
    meaningful if the id survives the hop.

    ⚠️ There is deliberately no `consumed`, `delivered` or `consumed_at` column
    here. `§8.1`: "A single global `consumed_at` flag is not sufficient because
    several projections and services may consume the same event." Progress
    lives in `event_deliveries`, one row per (event, consumer).
    """

    __tablename__ = "research_domain_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    #: Assigned by the database — `GENERATED ALWAYS AS IDENTITY` in `ER-M022`.
    #: Never set from Python; a caller that could supply one could place an
    #: event anywhere in the replay ordering consumers resume from.
    log_position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    producer: Mapped[str] = mapped_column(Text, nullable=False)
    resource_scope: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    relayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EventDelivery(Base):
    """One event's journey to ONE consumer — steps 3 and 5 of `13-…:§8.1`.

    Claim shape mirrors `DurableJob` deliberately: a delivery IS work, and a
    second vocabulary for claiming would be a second set of bugs.

    `deferred` is not a failure — `§8.2` requires a consumer to defer a future
    version when an earlier event is missing. `poisoned` is terminal and is
    where `§8.2`'s "rights-safe poison queue" lives.
    """

    __tablename__ = "event_deliveries"

    delivery_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_domain_events.event_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    defer_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poison_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    poison_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_of_delivery_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DomainInboxReceipt(Base):
    """A consumer's proof it handled an event — step 4 of `13-…:§8.1`.

    Written "in the same transaction as its local effect", which is what
    `uq_domain_inbox_consumer_event` turns into exactly-once: a repeated
    delivery collides on that index and becomes a no-op rather than a second
    effect. Every other guarantee in this subsystem is advisory without it.
    """

    __tablename__ = "domain_inbox"

    receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProjectionCheckpoint(Base):
    """How far a consumer has read, and whether it is healthy.

    `13-…:§5.2` — "consumer/index version, event position, lag and repair
    state". `event_position` is a LOG POSITION and not a timestamp: two events
    committed in one transaction share `now()`, so a consumer resuming "after
    time T" would either repeat them or skip them.
    """

    __tablename__ = "projection_checkpoints"

    checkpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_position: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lag_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repair_state: Mapped[str] = mapped_column(Text, nullable=False, default="healthy")
    repair_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ArtifactRecord(Base):
    """What this service knows about an artifact — and never where it is stored.

    `13-…:376` in full: "Artifact Service ID/version/hash, classification, rights
    snapshot, semantic role and owning aggregate; **never a raw storage key**".
    That last clause is the reason the table exists rather than a convenience: a
    storage key here would let a domain service address MinIO directly and route
    around the chain `13-…:739-741` makes strict — domain -> Artifact -> Storage
    -> MinIO, with the Storage Service the only holder of MinIO credentials
    (`artifact_service/src/services/storage_service.py:26-31`).

    What is enforced in the DATABASE is narrower than that sentence, and this
    docstring states the DDL and not the intent. `0026` replaced `0025`'s two
    `^[^/]+$` CHECKs — which refused only values containing a SLASH, and so
    accepted `s3:bucket:key`, a bare bucket name, `bucket\\path\\file`, a
    percent-encoded `s3://…` and any opaque blob — with two POSITIVE format
    constraints taken from the port's own types
    (`adapters/artifact_store.py:ArtifactRef`, `artifact_id: uuid.UUID` and
    `version: int`):

    * `ck_artifact_records_artifact_id_is_a_uuid` —
      `artifact_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'`.
      The canonical LOWERCASE UUID text form. It asserts nothing about RFC-4122
      version or variant nibbles, because `uuid.UUID` asserts nothing about them
      either.
    * `ck_artifact_records_artifact_version_is_a_positive_integer` —
      `artifact_version ~ '^[1-9][0-9]*$'`. A decimal integer from 1 upward, no
      sign, no separators, no leading zeros.

    That is the whole of it. Neither CHECK knows what a storage key is, and
    neither claims to: they refuse one only as a consequence of admitting
    exactly two formats. `artifact_content_hash` is pinned separately by
    `0025`'s `^sha256:[0-9a-f]{64}$`.

    ⚠️ The previous version of this paragraph promised the CHECK refused "an
    opaque blob" when the regex could not see one, and that unfalsifiable claim
    is why the gap survived review. A docstring that describes more than the DDL
    does is the defect, not a rounding error: keep this list in step with
    `alembic/versions/0026_*.py` or delete the list.

    ⚠️ `rights_checks_evaluated` is honestly EMPTY and stays that way for now.
    `13-…:759-760` names seven actions requiring a rights re-check before use —
    display, model/provider use, translation, quotation, export, redistribution,
    publication — and no rights authority exists to perform it (Ingestion
    `DocumentVersion`/rights is backlog item 14, and `13-…:1020` makes `FND-009`
    depend on it). Recording the empty set is the `0021.gates_evaluated` pattern:
    a visible gap a reader can count, rather than an absence they must infer.
    """

    __tablename__ = "artifact_records"

    artifact_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    owning_aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    owning_aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: Identity only. `0026`'s CHECK admits the canonical lowercase UUID text
    #: form here and a positive decimal integer (no leading zeros) below —
    #: `ArtifactRef.artifact_id: uuid.UUID` and `ArtifactRef.version: int`,
    #: spelled as the `TEXT` values `13-…` requires foreign identifiers to be.
    artifact_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_version: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    classification: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_role: Mapped[str] = mapped_column(Text, nullable=False)

    # The rights snapshot, `13-…:754-757`.
    rights_source_id: Mapped[str] = mapped_column(Text, nullable=False)
    rights_document_id: Mapped[str] = mapped_column(Text, nullable=False)
    rights_document_version_id: Mapped[str] = mapped_column(Text, nullable=False)
    rights_decision_id: Mapped[str] = mapped_column(Text, nullable=False)
    rights_evaluated_purpose: Mapped[str] = mapped_column(Text, nullable=False)
    rights_decision_version: Mapped[str] = mapped_column(Text, nullable=False)
    rights_decision_hash: Mapped[str] = mapped_column(Text, nullable=False)
    rights_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rights_expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rights_territory_constraint: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Which of the seven actions were actually re-checked. Empty today.
    rights_checks_evaluated: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    #: `13-…:795-798`'s mechanism — a reference, carrying nothing about what it
    #: permits. Residency POLICY CONTENT has no schema and `IMP-DEC-019` is open,
    #: so there is deliberately no region column and no allowed-region enum.
    residency_policy_ref: Mapped[str] = mapped_column(Text, nullable=False)

    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
