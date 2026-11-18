"""ORM models for `ER-M017`, `ER-M018` and `ER-M021` — research objects, accepted
evidence, and the snapshots that pin exactly what a run was allowed to read.

These classes are typed accessors and nothing more: the integrity layer — the
CHECK constraints, the partial unique indexes that keep supersession chains
linear, the composite same-tenant foreign keys, the append-only triggers and the
RLS policies — is declared only in `alembic/versions/`, exactly as in the rest of
this package. See `models/__init__.py` for why that split is deliberate and what
`alembic revision --autogenerate` will propose to do about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

#: Mirrors `ck_research_object_revision_kind` in `0017`. Closed vocabulary; the
#: CHECK and this tuple must not drift.
REVISION_KINDS = ("initial", "correction", "material_change")

#: Mirrors `ck_research_object_record_role` in `0017`. Closed vocabulary; the
#: CHECK and this tuple must not drift.
LINK_ROLES = ("primary", "companion")

#: Mirrors `ck_extraction_method_known` in `0018`. Closed vocabulary; the CHECK
#: and this tuple must not drift. `karma_proposal` arrives with `ER-006.3`.
EXTRACTION_METHODS = ("manual",)

#: Mirrors `ck_observation_basis_known` in `0018`. Closed vocabulary; the CHECK
#: and this tuple must not drift. `ck_observation_located_arm_unverifiable`
#: separately narrows what a writer can actually produce today.
BASIS_KINDS = ("evidence_located", "expert_assertion")

#: Mirrors `ck_assertion_support_stance` in `0018`. Closed vocabulary; the CHECK
#: and this tuple must not drift.
SUPPORT_STANCES = ("supports", "contradicts")

#: Mirrors `ck_acceptance_subject_kind_known` in `0018`, which builds its CHECK
#: from a constant of the same name. Closed vocabulary; the two must not drift.
ACCEPTANCE_SUBJECTS = ("research_object_version", "extraction_dataset_version", "observation_version")

#: Mirrors `ck_acceptance_decision_known` in `0018`. Closed vocabulary; the CHECK
#: and this tuple must not drift.
ACCEPTANCE_DECISIONS = ("accept", "withdraw")


# --------------------------------------------------------------------------
# ER-M017 — research objects
# --------------------------------------------------------------------------


class ResearchObject(Base):
    """Identity of a study. Append-only: there is nothing here that can change."""

    __tablename__ = "research_objects"

    research_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchObjectVersion(Base):
    __tablename__ = "research_object_versions"

    research_object_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Parented by a composite same-tenant FK declared in `0017`, so no
    #: single-column `ForeignKey` here.
    research_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_object_versions.research_object_version_id"),
        nullable=True,
    )
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    revision_kind: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchObjectRecord(Base):
    """The reports a study was built from, pinned to the corpus version they were in."""

    __tablename__ = "research_object_records"

    research_object_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    research_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_records.review_record_id"), nullable=False
    )
    review_record_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_record_versions.review_record_version_id"), nullable=False
    )
    corpus_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corpus_versions.corpus_version_id"), nullable=False
    )
    link_role: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    linked_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    linked_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# ER-M018 — extraction, observations, assertions, acceptance
# --------------------------------------------------------------------------


class ExtractionDataset(Base):
    __tablename__ = "extraction_datasets"

    extraction_dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    research_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dataset_key: Mapped[str] = mapped_column(Text, nullable=False)


class ExtractionDatasetVersion(Base):
    __tablename__ = "extraction_dataset_versions"

    extraction_dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extraction_dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_dataset_versions.extraction_dataset_version_id"),
        nullable=True,
    )
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Observation(Base):
    __tablename__ = "observations"

    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    extraction_dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    variable_key: Mapped[str] = mapped_column(Text, nullable=False)


class ObservationVersion(Base):
    """A typed value and what stands behind it — `16-…:§3.7`'s either/or.

    The evidence-located arm is declared but currently unreachable:
    `ck_observation_located_arm_unverifiable` in `0018` refuses it until the
    Ingestion/Documents `PinnedVersionRef` verifier exists.
    """

    __tablename__ = "observation_versions"

    observation_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("observation_versions.observation_version_id"),
        nullable=True,
    )
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    extraction_dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_dataset_versions.extraction_dataset_version_id"),
        nullable=False,
    )
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    basis_kind: Mapped[str] = mapped_column(Text, nullable=False)
    document_version_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expert_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    expert_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    expert_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    expert_decision_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchAssertion(Base):
    __tablename__ = "research_assertions"

    research_assertion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assertion_key: Mapped[str] = mapped_column(Text, nullable=False)


class ResearchAssertionVersion(Base):
    __tablename__ = "research_assertion_versions"

    research_assertion_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    research_assertion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_assertion_versions.research_assertion_version_id"),
        nullable=True,
    )
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchAssertionSupport(Base):
    """Which observations back a claim, and which contradict it."""

    __tablename__ = "research_assertion_support"

    support_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_assertion_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_assertion_versions.research_assertion_version_id"),
        nullable=False,
    )
    observation_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observation_versions.observation_version_id"), nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stance: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceAcceptance(Base):
    """Acceptance as an append-only decision over one exact version.

    A withdrawal is never an `UPDATE`; it is a new row citing the one it
    supersedes. `subject_content_hash` is a witness, checked equal to the
    subject's own hash by a trigger in `0018`.
    """

    __tablename__ = "evidence_acceptances"

    acceptance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    research_object_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_object_versions.research_object_version_id"),
        nullable=True,
    )
    extraction_dataset_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_dataset_versions.extraction_dataset_version_id"),
        nullable=True,
    )
    observation_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("observation_versions.observation_version_id"),
        nullable=True,
    )
    #: Generated and stored by the database. Postgres computes it AFTER
    #: BEFORE-triggers run, so it is NULL to anything inspecting the pending row.
    subject_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "COALESCE(research_object_version_id, extraction_dataset_version_id, observation_version_id)",
            persisted=True,
        ),
        nullable=True,
    )
    subject_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_acceptance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_acceptances.acceptance_id"), nullable=True
    )
    decided_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# ER-M021 — evidence snapshots
# --------------------------------------------------------------------------


class EvidenceSnapshot(Base):
    """The exact set of evidence one run was allowed to read, pinned by hash.

    `14-…:356` — "It is immutable after creation; changed evidence creates a new
    snapshot with an impact link." The table is append-only, enforced by the
    trigger `0021` installs and by a `REVOKE UPDATE, DELETE` on the application
    role: a snapshot that can be edited after a run bound its hash cannot answer
    what that run read, which is the only question it exists to answer. The
    impact link is ``supersedes_snapshot_id``, and the two partial unique indexes
    in `0021` make the supersession chain linear rather than a tree.

    ⚠️ ``gates_evaluated`` is a COLUMN and not a comment on purpose. `14-…:345`
    names five creation gates; three of them have no subject matter in this
    service, so a snapshot records which gates actually ran instead of letting a
    consumer assume five. An array rather than five booleans, because a boolean
    set can only say false, and false reads as "the gate failed" where the truth
    is "the gate does not exist". Ask
    ``domain/policies/snapshot_gates.py`` which those are and who owes each.

    Like every other class in this package this is a typed accessor and nothing
    more: the manifest pin, the hash-shape CHECKs, the gate-ledger subset and
    distinctness constraints, the chain indexes, the RLS policy and the
    append-only trigger are declared only in `alembic/versions/`.
    """

    __tablename__ = "evidence_snapshots"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence_snapshots.snapshot_id"),
        nullable=True,
    )
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the snapshot was built FOR. Required, because `14-…:366` forbids a
    #: downstream run binding a snapshot for the wrong purpose, and that check
    #: cannot be made against a snapshot that never declared one.
    intended_use: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_protocol_versions.protocol_version_id"), nullable=False
    )
    corpus_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corpus_versions.corpus_version_id"), nullable=True
    )
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_manifest_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which order key the manifest digest was computed under. A digest computed
    #: under a different order key is not a different number, it is not
    #: COMPARABLE, and a consumer recomputing under its own ordering would report
    #: tampering where there is none.
    member_ordering_rule: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: The gate ledger. See the class docstring; written from
    #: `domain/policies/snapshot_gates.evaluated_gates`.
    gates_evaluated: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("durable_jobs.job_id"), nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceSnapshotMember(Base):
    """One pinned version inside a snapshot. Append-only, like the snapshot itself.

    `14-…:344` lists six member kinds and this is ONE heterogeneous set, not six
    typed tables: a row is discriminated by ``owner_context`` + ``entity_type``,
    exactly as the released `urn:datacompany:pinned-version-ref:1` discriminates a
    manifest member. Six tables would need six digests and a migration on the day
    a seventh kind appears.

    The four order-key fields are named identically to the released contract
    rather than translated, because a member row and a manifest member must be
    recognisably the same thing or the digest computed over one cannot be checked
    against the other. `16-…:§3.5` is explicit that RFC 8785 preserves array
    order and does not canonicalize a set, so the enclosing
    ``member_manifest_hash`` is computed over these rows SORTED by
    ``owner_context, entity_type, entity_id, version_id``.

    ``content_hash`` here is the pinned target's OWN hash, and it remains
    included in the enclosing canonical view — that is what makes the manifest
    sensitive to a member whose content changed and not only to one that was
    added or removed. Removal is impossible anyway: the append-only trigger
    refuses it, because deleting a member after the digest was published makes
    the digest unverifiable rather than wrong, which is harder to notice.

    Integrity lives in `alembic/versions/0021_…`: the closed ``entity_type``
    CHECK, the hash shape, and ``uq_snapshot_member_identity``, which makes set
    semantics a database fact rather than an application convention.
    """

    __tablename__ = "evidence_snapshot_members"

    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_snapshots.snapshot_id"), nullable=False
    )
    owner_context: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    member_schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SnapshotArtifact(Base):
    """A claim about bytes that already exist — a digest, never the bytes.

    `13-…:§11` gives artifact storage to an `ArtifactStore` that is `FND-009` and
    does not exist. Rather than invent one or inline a blob into an append-only
    row nobody can delete, this row records ``payload_digest``, ``byte_size``,
    ``media_type`` and ``serializer_version``, and the service REPRODUCES the
    bytes on demand from the manifest.

    That is the stronger design for the completion criterion, not a workaround.
    `14-…:364` requires byte-identical reconstruction "under the same serializer
    version": a stored blob can only be handed back, while a reproduced one is
    re-derived from the pinned members every time and compared to the digest, so
    a member that drifted is caught rather than hidden behind a cached copy of
    the old answer. ``serializer_version`` is required because a row that cannot
    say which serializer produced it makes that criterion uncheckable.

    ``payload_digest`` is over the RAW BYTES and is not interchangeable with a
    ``content_hash`` over a canonical envelope — `16-…:§3.5` says the two "MUST
    NOT be substituted for one another". Append-only, for the same reason as its
    snapshot. The digest shape, the media-type shape and one artifact per kind
    per snapshot are declared only in `alembic/versions/0021_…`.
    """

    __tablename__ = "snapshot_artifacts"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_snapshots.snapshot_id"), nullable=False
    )
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    serializer_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
