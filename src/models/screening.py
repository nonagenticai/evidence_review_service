"""ORM models for `ER-M004` and `ER-M005` — dedup, screening, flow, export."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

DISPOSITIONS = ("include", "exclude", "uncertain")
RESOLUTION_TYPES = ("merge", "split", "no_match")

#: The closed vocabulary for `inclusion_exports.sealed_with`. `unknown` is a
#: legacy value for rows written before the column existed; nothing writes it.
SEAL_MODES = ("content_hash", "canonical_sha256", "unknown")


# --------------------------------------------------------------------------
# ER-M004 — deduplication
# --------------------------------------------------------------------------


class DeduplicationRun(Base):
    __tablename__ = "deduplication_runs"

    #: `0012` creates this; the ORM declares it too, with the same name and the
    #: same columns, for the reason spelled out on
    #: `InclusionExport.ck_export_sealed_with_vocabulary`: DDL only the migration
    #: knows about is DDL `alembic revision --autogenerate` proposes to DROP, and
    #: what would be dropped here is the only thing standing between
    #: `create_run's replay check` and a check-then-insert race under READ COMMITTED.
    __table_args__ = (UniqueConstraint("review_id", "input_digest", name="uq_dedup_run_input_digest"),)

    dedup_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    corpus_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    engine_id: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_digest: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pair_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicatePairScore(Base):
    __tablename__ = "duplicate_pair_scores"

    pair_score_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dedup_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("deduplication_runs.dedup_run_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    left_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    right_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    matched_on: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicateCluster(Base):
    __tablename__ = "duplicate_clusters"

    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dedup_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("deduplication_runs.dedup_run_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cluster_key: Mapped[str] = mapped_column(Text, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicateClusterMember(Base):
    __tablename__ = "duplicate_cluster_members"

    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("duplicate_clusters.cluster_id"), primary_key=True
    )
    review_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_record_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class DuplicateResolution(Base):
    """Append-only. Reversal is a NEW row superseding the old one."""

    __tablename__ = "duplicate_resolutions"

    resolution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("duplicate_clusters.cluster_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resolution_type: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_resolution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    survivor_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicateSurvivorshipField(Base):
    __tablename__ = "duplicate_survivorship_fields"

    survivorship_field_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resolution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("duplicate_resolutions.resolution_id")
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_record_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (UniqueConstraint("resolution_id", "field_name"),)


# --------------------------------------------------------------------------
# ER-M005 — screening
# --------------------------------------------------------------------------


class ScreeningAssignment(Base):
    __tablename__ = "screening_assignments"

    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The sealed corpus this assignment was minted from (ER-M009). Not a
    #: convenience: the queue resolves each record's content THROUGH it, so a
    #: reviewer is shown the version the corpus contains rather than the
    #: review's tail. Without it a survivorship merge silently changes what an
    #: already-assigned reviewer is looking at.
    corpus_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corpus_versions.corpus_version_id"), nullable=False
    )
    reviewer_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScreeningDecision(Base):
    """Append-only and BLINDED.

    Never query this class directly outside
    ``application.services.blinded_reads`` — L2 of the blinding layers is a test
    that greps for exactly that, because a raw ``select(ScreeningDecision)``
    elsewhere is a read path the policy still covers but the *shape* of whose
    result nobody reviewed.
    """

    __tablename__ = "screening_decisions"

    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_record_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reviewer_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    exclusion_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScreeningConflict(Base):
    __tablename__ = "screening_conflicts"

    conflict_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    review_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScreeningAdjudication(Base):
    __tablename__ = "screening_adjudications"

    adjudication_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conflict_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("screening_conflicts.conflict_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    exclusion_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    adjudicator_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The adjudicator's name as their credential gave it, captured at write
    #: time rather than resolved at read time.
    #:
    #: Nullable and NOT backfillable: this table is append-only (`0005` refuses
    #: every UPDATE), so rows written before this column existed keep `NULL`
    #: permanently and readers fall back to `adjudicator_subject_id`. That
    #: fallback is the intended rendering for them, not a gap to repair.
    #:
    #: Captured rather than looked up because resolving a subject id to a name
    #: at read time would mean a Keycloak admin credential with realm-wide
    #: `view-users` — a user directory grafted into a service whose only tenant
    #: boundary is RLS. A name recorded at the moment of the act is also the
    #: more honest record: it says who ruled as they were known then.
    adjudicator_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_adjudication_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScreeningUnblindingEvent(Base):
    __tablename__ = "screening_unblinding_events"

    unblinding_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conflict_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("screening_conflicts.conflict_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: WHO this disclosure is for (`ER-004.2`, `0008`). L1's third clause reads
    #: it, so this is not a record of the decision — it IS the decision. Always
    #: contains the unblinding actor; anyone else is named by the lead.
    disclosed_to_subject_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FlowRevision(Base):
    __tablename__ = "flow_revisions"

    flow_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FlowProjection(Base):
    """A CACHE. `POST /flow:rebuild` recomputes from zero and must agree."""

    __tablename__ = "flow_projections"

    projection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    flow_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    canonical_counts_hash: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InclusionExport(Base):
    __tablename__ = "inclusion_exports"

    # Migration 0006 creates this CHECK; the ORM has to declare it too, with the
    # same name and the same text. When only the migration knew about it,
    # `alembic revision --autogenerate` compared a database that HAS the
    # constraint against metadata that does not and emitted a `drop_constraint`
    # — a real instruction to delete the only thing keeping `sealed_with` a
    # closed vocabulary. The blast-radius test caught exactly that.
    #
    # It went red on main rather than on either PR: the branch that added the
    # constraint was cut before the branch that tightened the test, so the two
    # only ever met in the squashed merge commit. Neither PR was wrong about
    # itself; no PR run existed that contained both halves.
    __table_args__ = (
        CheckConstraint(
            "sealed_with IN ('content_hash', 'canonical_sha256', 'unknown')",
            name="ck_export_sealed_with_vocabulary",
        ),
        #: `0012`, declared here by the argument the comment above already makes.
        #: This one is the load-bearing case for it: `inclusion_exports` is
        #: append-only, so a twin seal minted by the race this forbids can never
        #: be deleted by anybody, at any privilege this service holds.
        UniqueConstraint("review_id", "manifest_hash", name="uq_export_manifest_per_review"),
    )

    export_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    flow_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    corpus_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: HOW `manifest_hash` was produced — `content_hash` (validated against the
    #: declared schema) or `canonical_sha256` (nothing validated). No default:
    #: an insert that omits it must fail rather than inherit a mode it did not
    #: use. `unknown` exists only for rows written before the column.
    sealed_with: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    omissions: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
