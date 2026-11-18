"""ORM models for `ER-M003` — imports, record identity/versions, sealed corpus."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

#: Our discriminator, and the one place its values are written down.
#: Emphatically NOT `work_type` — see `src/domain/policies/denylist.py`.
RECORD_TYPES = ("journal_article", "preprint", "report", "dataset", "other")

SOURCE_KINDS = ("discovery_session", "ris", "bibtex", "csv", "canonical_json")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    #: `0012` creates this; the ORM declares it too, with the same name and the
    #: same columns, for the reason spelled out on
    #: `InclusionExport.ck_export_sealed_with_vocabulary`: DDL only the migration
    #: knows about is DDL `alembic revision --autogenerate` proposes to DROP, and
    #: what would be dropped here is the only thing standing between
    #: `import_payload's replay check` and a check-then-insert race under READ COMMITTED.
    __table_args__ = (UniqueConstraint("review_id", "raw_payload_digest", name="uq_import_batch_payload_digest"),)

    import_batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportRow(Base):
    __tablename__ = "import_rows"

    import_row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_batches.import_batch_id"))
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_representation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("import_batch_id", "row_index"),)


class ReviewRecord(Base):
    """Stable identity. Never deleted, never edited — corrections are versions."""

    __tablename__ = "review_records"

    review_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewRecordVersion(Base):
    __tablename__ = "review_record_versions"

    review_record_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_records.review_record_id")
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    record_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    container_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(Text, nullable=True)
    pmid: Mapped[str | None] = mapped_column(Text, nullable=True)
    pmcid: Mapped[str | None] = mapped_column(Text, nullable=True)
    arxiv_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_doi: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_pmid: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_import_row_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("review_record_id", "version_number"),)


class CorpusVersion(Base):
    __tablename__ = "corpus_versions"

    corpus_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_ordering_rule: Mapped[str] = mapped_column(Text, nullable=False)
    member_manifest_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("review_id", "version_number"),)


class CorpusVersionMember(Base):
    __tablename__ = "corpus_version_members"

    corpus_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corpus_versions.corpus_version_id"), primary_key=True
    )
    review_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_records.review_record_id"), primary_key=True
    )
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    review_record_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
