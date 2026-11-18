"""ER-M004 deduplication: frozen runs, proposed clusters, reversible resolution.

Revision ID: 0004_er_m004
Revises: 0003_er_m003
Create Date: 2026-08-08

`ER-003`'s hard requirement is that merge -> split -> merge is **reversible and
attributable** (`01-…:456`) and that no original is ever deleted (`01-…:103`,
`:313`). Both are structural here rather than procedural.

**Reversal is an insert, not an undo.** `duplicate_resolutions` is append-only; a
merge is undone by inserting a *new* `split` row whose `supersedes_resolution_id`
names the merge. After merge -> split -> merge the table holds three rows, each
with its own actor and rationale, and the current resolution is the tail of the
chain. An `UPDATE ... SET resolution_type='split'` would lose the fact that a
merge ever happened, which is exactly the attribution the requirement asks for.

**Two partial uniques make that chain well-formed under concurrency:**

* ``UNIQUE (supersedes_resolution_id) WHERE NOT NULL`` — one successor per link,
  so the chain is linear and cannot fork into two disagreeing "current" answers.
* ``UNIQUE (cluster_id) WHERE supersedes_resolution_id IS NULL`` — exactly one
  head per cluster, so two concurrent first-resolutions produce one winner at the
  database rather than two roots.

Together these are `01-…:457` ("concurrent resolution produces one result")
without an application lock anyone can forget to take.

**Survivorship is FK-enforced.** Every `duplicate_survivorship_fields` row points
at a real `review_record_versions` row with `ON DELETE RESTRICT`, so
`01-…:453` — "every survivor field resolves to an original record/version" — is a
foreign key, and the source record it resolves to cannot be deleted underneath it.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_er_m004"
down_revision = "0003_er_m003"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"


def _tenant_rls(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = current_setting('{TENANT_GUC}', true))
          WITH CHECK (tenant_id = current_setting('{TENANT_GUC}', true));
    """


def _grant(table: str) -> str:
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


def _append_only(table: str, reason: str) -> str:
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


def upgrade() -> None:
    # ---- runs: the frozen input ------------------------------------------
    op.create_table(
        "deduplication_runs",
        sa.Column("dedup_run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("corpus_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine_id", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        # `ER-003.1` — corpus version, engine, engine version and config frozen
        # into one digest, so "the same input gives the same clusters" is a
        # checkable claim rather than an assumption about what was running.
        sa.Column("input_digest", sa.Text(), nullable=False),
        sa.Column("cluster_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pair_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["corpus_version_id"], ["corpus_versions.corpus_version_id"], ondelete="RESTRICT"),
    )
    op.execute(_tenant_rls("deduplication_runs"))
    op.execute(_grant("deduplication_runs"))
    op.create_index("ix_dedup_runs_review", "deduplication_runs", ["review_id", "created_at"])
    op.create_index("ix_dedup_runs_digest", "deduplication_runs", ["review_id", "input_digest"])

    # ---- pair scores ------------------------------------------------------
    op.create_table(
        "duplicate_pair_scores",
        sa.Column("pair_score_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dedup_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("left_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("right_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Which rule fired, and its score. The baseline is exact-match, so the
        # score is 1.0 — but the column exists because `ER-003.2` allows a
        # qualified similarity extension, and adding it later must not require a
        # migration that rewrites history.
        sa.Column("matched_on", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["dedup_run_id"], ["deduplication_runs.dedup_run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["left_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["right_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        # An ordered pair, deduplicated: (A,B) and (B,A) are one fact.
        sa.CheckConstraint("left_record_id < right_record_id", name="ck_pair_ordered"),
        sa.UniqueConstraint("dedup_run_id", "left_record_id", "right_record_id", name="uq_pair_per_run"),
    )
    op.execute(_tenant_rls("duplicate_pair_scores"))
    op.execute(_grant("duplicate_pair_scores"))

    # ---- clusters ---------------------------------------------------------
    op.create_table(
        "duplicate_clusters",
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dedup_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("cluster_key", sa.Text(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["dedup_run_id"], ["deduplication_runs.dedup_run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("dedup_run_id", "cluster_key", name="uq_cluster_key_per_run"),
        sa.CheckConstraint("member_count >= 2", name="ck_cluster_has_two_members"),
    )
    op.execute(_tenant_rls("duplicate_clusters"))
    op.execute(_grant("duplicate_clusters"))

    op.create_table(
        "duplicate_cluster_members",
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("review_record_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("cluster_id", "review_record_id", name="pk_duplicate_cluster_members"),
        sa.ForeignKeyConstraint(["cluster_id"], ["duplicate_clusters.cluster_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["review_record_version_id"],
            ["review_record_versions.review_record_version_id"],
            ondelete="RESTRICT",
        ),
    )
    op.execute(_tenant_rls("duplicate_cluster_members"))
    op.execute(_grant("duplicate_cluster_members"))

    # ---- resolutions: append-only, chained --------------------------------
    op.create_table(
        "duplicate_resolutions",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("resolution_type", sa.Text(), nullable=False),
        sa.Column("supersedes_resolution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("survivor_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("decided_by_subject_id", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["cluster_id"], ["duplicate_clusters.cluster_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_resolution_id"], ["duplicate_resolutions.resolution_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["survivor_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("resolution_type IN ('merge','split','no_match')", name="ck_resolution_type"),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_resolution_rationale_not_blank"),
        sa.CheckConstraint("btrim(decided_by_subject_id) <> ''", name="ck_resolution_actor_not_blank"),
        # A merge names its survivor; a split and a no-match do not have one.
        # Without this, a merge with a null survivor would leave the cluster
        # collapsed onto nothing.
        sa.CheckConstraint(
            "(resolution_type = 'merge' AND survivor_record_id IS NOT NULL) "
            "OR (resolution_type <> 'merge' AND survivor_record_id IS NULL)",
            name="ck_resolution_survivor_matches_type",
        ),
    )
    op.execute(_tenant_rls("duplicate_resolutions"))
    op.execute(_grant("duplicate_resolutions"))
    op.execute(
        _append_only(
            "duplicate_resolutions",
            "reversal is a NEW row superseding the old one, never an edit",
        )
    )

    # The two partial uniques the reversibility guarantee rests on.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_resolution_supersession_linear
          ON duplicate_resolutions (supersedes_resolution_id)
          WHERE supersedes_resolution_id IS NOT NULL;

        CREATE UNIQUE INDEX uq_resolution_one_head_per_cluster
          ON duplicate_resolutions (cluster_id)
          WHERE supersedes_resolution_id IS NULL;
        """
    )

    # A superseding resolution must stay inside its own cluster and tenant.
    # `01-…:328` — "database constraints reject cross-review and cross-tenant
    # supersession". The FK permits both; this does not.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION duplicate_resolutions_same_cluster() RETURNS trigger AS $$
        DECLARE
          prev record;
        BEGIN
          IF NEW.supersedes_resolution_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT cluster_id, review_id, tenant_id INTO prev
            FROM duplicate_resolutions WHERE resolution_id = NEW.supersedes_resolution_id;
          IF prev.cluster_id IS DISTINCT FROM NEW.cluster_id
             OR prev.review_id IS DISTINCT FROM NEW.review_id
             OR prev.tenant_id IS DISTINCT FROM NEW.tenant_id THEN
            RAISE EXCEPTION 'resolution % cannot supersede % across cluster/review/tenant',
              NEW.resolution_id, NEW.supersedes_resolution_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_duplicate_resolutions_same_cluster
          BEFORE INSERT ON duplicate_resolutions
          FOR EACH ROW EXECUTE FUNCTION duplicate_resolutions_same_cluster();
        """
    )

    # ---- survivorship fields ---------------------------------------------
    op.create_table(
        "duplicate_survivorship_fields",
        sa.Column("survivorship_field_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        # ON DELETE RESTRICT: the original this field came from cannot be
        # deleted while a survivor claims it. That is `01-…:453` as a foreign key.
        sa.Column("source_record_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["resolution_id"], ["duplicate_resolutions.resolution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_record_version_id"],
            ["review_record_versions.review_record_version_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("resolution_id", "field_name", name="uq_survivorship_field_once"),
    )
    op.execute(_tenant_rls("duplicate_survivorship_fields"))
    op.execute(_grant("duplicate_survivorship_fields"))
    op.execute(_append_only("duplicate_survivorship_fields", "field survivorship belongs to its resolution"))

    op.create_index("ix_cluster_members_record", "duplicate_cluster_members", ["review_id", "review_record_id"])
    op.create_index("ix_resolutions_cluster", "duplicate_resolutions", ["cluster_id", "decided_at"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_duplicate_resolutions_same_cluster ON duplicate_resolutions")
    op.execute("DROP FUNCTION IF EXISTS duplicate_resolutions_same_cluster()")
    for table in ("duplicate_survivorship_fields", "duplicate_resolutions"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
    op.drop_table("duplicate_survivorship_fields")
    op.drop_table("duplicate_resolutions")
    op.drop_table("duplicate_cluster_members")
    op.drop_table("duplicate_clusters")
    op.drop_table("duplicate_pair_scores")
    op.drop_table("deduplication_runs")
