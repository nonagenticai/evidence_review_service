"""ORM models for `ER-M002` — review identity, protocol versions, stages, state.

Every table these map to carries its real guarantee as a database constraint or
trigger, so these classes are a typed accessor rather than the place the rules
live. That split is intentional: a rule enforced only here is bypassed by the
next writer, and this service will eventually have a worker process.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

#: The lifecycle of `01-…:114-118`, mirrored from the migration so application
#: code can pre-check a transition and return a typed 409 instead of surfacing a
#: raw database exception. The database remains the authority — this is a
#: courtesy, and `tests/migration/test_an_abandoned_draft_can_be_archived.py`
#: pins this set equal to the live trigger function.
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("draft", "configured"),
        ("configured", "active"),
        ("active", "paused"),
        ("paused", "active"),
        ("active", "closed"),
        ("paused", "closed"),
        ("closed", "active"),
        ("closed", "archived"),
        # `0028`: an abandoned draft is disposed of by archiving it — the
        # database is append-only, so there is no delete to fall back on.
        ("draft", "archived"),
    }
)

REVIEW_STATES = ("draft", "configured", "active", "paused", "closed", "archived")


class Review(Base):
    __tablename__ = "reviews"

    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "external_key"),)


class ReviewProtocolVersion(Base):
    __tablename__ = "review_protocol_versions"

    protocol_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    inclusion_criteria: Mapped[dict] = mapped_column(JSONB, nullable=False)
    exclusion_criteria: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stage_definitions: Mapped[list] = mapped_column(JSONB, nullable=False)
    exclusion_reason_vocabulary: Mapped[list] = mapped_column(JSONB, nullable=False)
    reviewers_required: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("review_id", "version_number"),)

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None


class ReviewStage(Base):
    __tablename__ = "review_stages"

    stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    protocol_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage_key: Mapped[str] = mapped_column(Text, nullable=False)
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    reviewers_required: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("review_id", "stage_key"),)


class ReviewStateEvent(Base):
    """Append-only. The tail of this table IS the review's current state."""

    __tablename__ = "review_state_history"

    state_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviews.review_id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    actor_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approval_attestation_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("review_id", "revision_number"),
        CheckConstraint("revision_number >= 1"),
    )
