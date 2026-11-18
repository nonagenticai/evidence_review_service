"""ER-M002 reviews: identity, immutable protocol versions, and derived current state.

Revision ID: 0002_er_m002
Revises: 0001_er_m001
Create Date: 2026-08-08

Three structural decisions here are the ones worth reading, because each replaces
a check that would otherwise live in application code that a second caller can
bypass.

**`reviews.current_state` does not exist.** `16-datacompany-canonical-domain-data-model.md:344-346`
makes any "current" column a rebuildable projection rather than authority, so the
current state is the tail of an append-only `review_state_history`. A column would
be a second answer to the same question, and the two answers diverge the first
time a transition is rolled back.

**A locked protocol version is immutable in the database.** `01-…:120` requires a
locked `ProtocolVersion` for activation, and `DM-AT-02` requires that the lock
mean something. A `BEFORE UPDATE OR DELETE` trigger enforces it, so an accidental
`UPDATE review_protocol_versions SET ...` during an incident is an error rather
than a silently rewritten protocol under decisions already taken against it.

**The exclusion-reason vocabulary is closed and lives on the protocol version.**
`01-…:106-107` forbids fixed reason codes in the service. They are therefore data
on the locked protocol, and a decision citing a code outside that list is refused
against the protocol the decision was actually taken under — not against whatever
the vocabulary happens to be later.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_er_m002"
down_revision = "0001_er_m001"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"

#: The lifecycle of `01-evidence-review-8810.md:114-118`. Legal transitions are a
#: table rather than a set of IFs so that the state machine can be read, and
#: tested, as data.
LEGAL_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("draft", "configured"),
    ("configured", "active"),
    ("active", "paused"),
    ("paused", "active"),
    ("active", "closed"),
    ("paused", "closed"),
    ("closed", "active"),  # approved reopen only; ER-001.5 requires an attestation
    ("closed", "archived"),
)


def _append_only_trigger(table: str, reason: str) -> str:
    """DDL for the guard that makes a table append-only in the database.

    Application-level append-only is a convention; this is a constraint. It is
    written once and applied to every table whose rows are evidence about a
    human judgement, because the whole value of that evidence is that it cannot
    have been edited afterwards.
    """
    fn = f"{table}_append_only"
    return f"""
        CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is append-only: {reason}'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_{fn}
          BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION {fn}();
    """


def _tenant_rls(table: str) -> str:
    """ENABLE + FORCE row-level security with the standard tenant predicate.

    FORCE matters: ENABLE alone exempts the owner, and the owner is who
    migrations connect as. The runtime role is a non-owner so it cannot turn any
    of this off — see `src/config.py` for why there are two roles at all.
    """
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = current_setting('{TENANT_GUC}', true))
          WITH CHECK (tenant_id = current_setting('{TENANT_GUC}', true));
    """


def _grant(table: str) -> str:
    """Grant DML to the runtime role when it exists.

    Conditional for the same reason ER-M001's grant is: the role is provisioned
    out of band and is absent from CI's ephemeral postgres. Absent is announced
    with a NOTICE rather than skipped silently, so a production database missing
    the role shows up in the migration log instead of as a runtime permission
    denial hours later.
    """
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; DML grant on {table} skipped.';
          END IF;
        END
        $$;
    """


def upgrade() -> None:
    # ---- reviews: stable identity only -----------------------------------
    op.create_table(
        "reviews",
        sa.Column("review_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        # The caller's own key for this review, so a retried create is the same
        # review rather than a second one. Scoped per project, not global.
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "project_id", "external_key", name="uq_reviews_project_external_key"),
        sa.CheckConstraint("btrim(created_by_subject_id) <> ''", name="ck_reviews_creator_not_blank"),
    )
    op.execute(_tenant_rls("reviews"))
    op.execute(_grant("reviews"))

    # ---- protocol versions: immutable once locked -------------------------
    op.create_table(
        "review_protocol_versions",
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Which released schema this version's content validates against, and the
        # hash of that content. Both are recorded per version rather than per
        # review: a protocol locked under one schema stays interpretable after
        # the schema moves on.
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("inclusion_criteria", postgresql.JSONB(), nullable=False),
        sa.Column("exclusion_criteria", postgresql.JSONB(), nullable=False),
        sa.Column("stage_definitions", postgresql.JSONB(), nullable=False),
        # A CLOSED vocabulary. `01-…:106-107` forbids fixed reason codes in the
        # service, so they are protocol data; a decision citing a code absent
        # from this list is refused.
        sa.Column("exclusion_reason_vocabulary", postgresql.JSONB(), nullable=False),
        sa.Column("reviewers_required", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by_subject_id", sa.Text(), nullable=True),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("review_id", "version_number", name="uq_protocol_version_number"),
        sa.CheckConstraint("reviewers_required >= 1", name="ck_protocol_reviewers_required_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(exclusion_reason_vocabulary) = 'array'",
            name="ck_protocol_reason_vocabulary_is_array",
        ),
        # A lock has an author. A locked_at without a locker is an unattributable
        # freeze of the document every later decision is judged against.
        sa.CheckConstraint(
            "(locked_at IS NULL AND locked_by_subject_id IS NULL) "
            "OR (locked_at IS NOT NULL AND btrim(locked_by_subject_id) <> '')",
            name="ck_protocol_lock_is_attributable",
        ),
    )
    op.execute(_tenant_rls("review_protocol_versions"))
    op.execute(_grant("review_protocol_versions"))

    # Immutable ONCE LOCKED — not immutable always. An unlocked draft is still
    # being written; the lock is the moment it becomes evidence. DELETE is
    # refused for locked rows too: a protocol that decisions reference cannot be
    # made to vanish, and the FK from `review_state_history` would not stop a
    # delete of a version nothing has activated against yet.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION review_protocol_versions_locked_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.locked_at IS NOT NULL THEN
            RAISE EXCEPTION 'protocol version % is locked and cannot be modified or deleted',
              OLD.protocol_version_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_review_protocol_versions_locked_immutable
          BEFORE UPDATE OR DELETE ON review_protocol_versions
          FOR EACH ROW EXECUTE FUNCTION review_protocol_versions_locked_immutable();
        """
    )

    # ---- stages -----------------------------------------------------------
    op.create_table(
        "review_stages",
        sa.Column("stage_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage_key", sa.Text(), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reviewers_required", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("review_id", "stage_key", name="uq_review_stage_key"),
        sa.CheckConstraint("reviewers_required >= 1", name="ck_stage_reviewers_required_positive"),
    )
    op.execute(_tenant_rls("review_stages"))
    op.execute(_grant("review_stages"))

    # ---- append-only state history: the authority on current state --------
    op.create_table(
        "review_state_history",
        sa.Column("state_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("actor_subject_id", sa.Text(), nullable=False),
        # Set on activation. Null elsewhere; the check below pins that.
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        # `01-…:126-127` — a reopen is always attested, and the attestation is
        # bound to the exact closed revision it reopens.
        sa.Column("approval_attestation_ref", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        # The optimistic-concurrency key. Two concurrent transitions from the
        # same observed revision collide here rather than both applying, which
        # is `ER-001` "concurrent patch/close/reopen yields one legal outcome"
        # as a unique index instead of a lock the application must remember to
        # take.
        sa.UniqueConstraint("review_id", "revision_number", name="uq_review_state_revision"),
        sa.CheckConstraint("revision_number >= 1", name="ck_state_revision_positive"),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_state_rationale_not_blank"),
        sa.CheckConstraint("btrim(actor_subject_id) <> ''", name="ck_state_actor_not_blank"),
        sa.CheckConstraint(
            "to_state IN ('draft','configured','active','paused','closed','archived')",
            name="ck_state_to_state_known",
        ),
        # Activation without a protocol version is the failure `DM-AT-02` exists
        # to prevent: a review taking decisions against nothing.
        sa.CheckConstraint(
            "(to_state <> 'active') OR (protocol_version_id IS NOT NULL)",
            name="ck_state_active_requires_protocol",
        ),
        # A reopen is a closed -> active transition and is the only transition
        # that must carry an attestation.
        sa.CheckConstraint(
            "(NOT (from_state = 'closed' AND to_state = 'active')) "
            "OR (btrim(coalesce(approval_attestation_ref, '')) <> '')",
            name="ck_state_reopen_requires_attestation",
        ),
    )
    op.execute(_tenant_rls("review_state_history"))
    op.execute(_grant("review_state_history"))
    op.execute(_append_only_trigger("review_state_history", "a transition that happened cannot un-happen"))

    # The legal-transition table, as data, checked by a trigger. Keeping it in
    # the database means a direct INSERT — a repair script, a future worker —
    # cannot walk the review into a state the lifecycle forbids.
    transitions_sql = ", ".join(f"('{a}','{b}')" for a, b in LEGAL_TRANSITIONS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION review_state_history_legal_transition() RETURNS trigger AS $$
        DECLARE
          prev_state text;
          prev_revision int;
        BEGIN
          SELECT h.to_state, h.revision_number INTO prev_state, prev_revision
            FROM review_state_history h
           WHERE h.review_id = NEW.review_id
           ORDER BY h.revision_number DESC
           LIMIT 1;

          IF prev_state IS NULL THEN
            -- The first row is the review coming into existence.
            IF NEW.from_state IS NOT NULL OR NEW.to_state <> 'draft' OR NEW.revision_number <> 1 THEN
              RAISE EXCEPTION 'the first state row must be revision 1, NULL -> draft (got % -> %, revision %)',
                NEW.from_state, NEW.to_state, NEW.revision_number
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
          END IF;

          IF NEW.from_state IS DISTINCT FROM prev_state THEN
            RAISE EXCEPTION 'stale transition: review is in state %, caller declared %',
              prev_state, NEW.from_state
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.revision_number <> prev_revision + 1 THEN
            RAISE EXCEPTION 'revision must be contiguous: expected %, got %',
              prev_revision + 1, NEW.revision_number
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NOT ((NEW.from_state, NEW.to_state) IN ({transitions_sql})) THEN
            RAISE EXCEPTION 'illegal lifecycle transition % -> %', NEW.from_state, NEW.to_state
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_review_state_history_legal_transition
          BEFORE INSERT ON review_state_history
          FOR EACH ROW EXECUTE FUNCTION review_state_history_legal_transition();
        """
    )

    # Activation must name a LOCKED protocol version belonging to this review.
    # Both halves matter: an unlocked protocol is still editable, and a protocol
    # from another review is a cross-aggregate reference the FK alone permits.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION review_state_history_activation_gate() RETURNS trigger AS $$
        DECLARE
          pv_review uuid;
          pv_locked timestamptz;
        BEGIN
          IF NEW.protocol_version_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT p.review_id, p.locked_at INTO pv_review, pv_locked
            FROM review_protocol_versions p
           WHERE p.protocol_version_id = NEW.protocol_version_id;

          IF pv_review IS DISTINCT FROM NEW.review_id THEN
            RAISE EXCEPTION 'protocol version % belongs to a different review', NEW.protocol_version_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          IF pv_locked IS NULL THEN
            RAISE EXCEPTION 'protocol version % is not locked; activation requires a locked protocol (DM-AT-02)',
              NEW.protocol_version_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_review_state_history_activation_gate
          BEFORE INSERT ON review_state_history
          FOR EACH ROW EXECUTE FUNCTION review_state_history_activation_gate();
        """
    )

    op.create_index("ix_review_state_history_review", "review_state_history", ["review_id", "revision_number"])
    op.create_index("ix_review_protocol_versions_review", "review_protocol_versions", ["review_id"])
    op.create_index("ix_review_stages_review", "review_stages", ["review_id", "stage_order"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_review_state_history_activation_gate ON review_state_history")
    op.execute("DROP FUNCTION IF EXISTS review_state_history_activation_gate()")
    op.execute("DROP TRIGGER IF EXISTS trg_review_state_history_legal_transition ON review_state_history")
    op.execute("DROP FUNCTION IF EXISTS review_state_history_legal_transition()")
    op.execute("DROP TRIGGER IF EXISTS trg_review_state_history_append_only ON review_state_history")
    op.execute("DROP FUNCTION IF EXISTS review_state_history_append_only()")
    op.execute("DROP TRIGGER IF EXISTS trg_review_protocol_versions_locked_immutable ON review_protocol_versions")
    op.execute("DROP FUNCTION IF EXISTS review_protocol_versions_locked_immutable()")
    op.drop_table("review_state_history")
    op.drop_table("review_stages")
    op.drop_table("review_protocol_versions")
    op.drop_table("reviews")
