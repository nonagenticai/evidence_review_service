"""ER-M005 screening: assignments, blinded append-only decisions, conflicts, flow, export.

Revision ID: 0005_er_m005
Revises: 0004_er_m004
Create Date: 2026-08-08

This migration carries **L1 of the five blinding layers** (`ER-004.2`,
`01-…:471-472`). L1 is the only one that survives a bug in every other layer,
because it is enforced by Postgres against the connection rather than by code the
next router can forget to call.

    USING (
      tenant_id = current_setting('app.current_tenant', true)
      AND (
        reviewer_subject_id = current_setting('app.actor_subject_id', true)
        OR current_setting('app.actor_role', true) = 'lead'
        OR EXISTS (a recorded unblinding event for this exact conflict)
      )
    )

Three clauses, in the order they are trusted:

1. **Your own decision.** Always visible; a reviewer must be able to see and
   correct what they recorded.
2. **The lead.** Somebody has to adjudicate and export. This is a read role, not
   an admin flag — the adjudication trigger below still refuses a lead who took
   part in the conflict they are adjudicating (`01-…:659`).
3. **A recorded unblinding.** The only door, and it leaves a row. Before it
   exists the conflict detail returns `dispositions: "redacted"`; after it, the
   policy opens for that conflict alone — not for the review, and not for the
   reviewer.

`FORCE ROW LEVEL SECURITY` plus a non-owner runtime role is what makes this hold
against the application itself: `evidence_review_app` cannot
`ALTER TABLE ... DISABLE ROW LEVEL SECURITY`, so a compromised or buggy service
process cannot turn the policy off.

## No mutable "current" column, anywhere

`screening_decisions` is append-only and a correction supersedes (`ER-004.5`).
The current decision is the tail of the supersession chain, made deterministic
by two partial uniques:

* one un-superseded decision per `(review, stage, round, record, reviewer)`;
* one successor per superseded decision.

An `is_current` boolean would be a second answer to the same question, and the
two disagree the first time a correction lands concurrently with a read.

## Conflicts are derived facts, and immutable

There is no `status` column to update. A conflict is resolved exactly when an
adjudication exists for it, so "is this conflict still open?" has one answer
computed from one place.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_er_m005"
down_revision = "0004_er_m004"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
ACTOR_GUC = "app.actor_subject_id"
ROLE_GUC = "app.actor_role"


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
    # ---- assignments: immutable ------------------------------------------
    op.create_table(
        "screening_assignments",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_subject_id", sa.Text(), nullable=False),
        sa.Column("assigned_by_subject_id", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stage_id"], ["review_stages.stage_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "review_id",
            "stage_id",
            "round_number",
            "review_record_id",
            "reviewer_subject_id",
            name="uq_assignment_once",
        ),
        sa.CheckConstraint("btrim(reviewer_subject_id) <> ''", name="ck_assignment_reviewer_not_blank"),
        sa.CheckConstraint("round_number >= 1", name="ck_assignment_round_positive"),
    )
    op.execute(_tenant_rls("screening_assignments"))
    op.execute(_grant("screening_assignments"))
    op.execute(_append_only("screening_assignments", "assignment inputs are immutable (ER-004.1)"))

    # L4's cursor is `(assigned_at, assignment_id)` over the caller's OWN
    # assignments. This index is what makes that cursor cheap; a cursor over
    # `review_records` instead would expose gaps that reveal the other
    # reviewer's activity, which is why the queue never paginates that way.
    op.create_index(
        "ix_assignments_queue",
        "screening_assignments",
        ["review_id", "stage_id", "round_number", "reviewer_subject_id", "assigned_at", "assignment_id"],
    )

    # ---- decisions: append-only, blinded ---------------------------------
    op.create_table(
        "screening_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_record_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_subject_id", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("exclusion_reason_code", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stage_id"], ["review_stages.stage_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["review_record_version_id"],
            ["review_record_versions.review_record_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["assignment_id"], ["screening_assignments.assignment_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_decision_id"], ["screening_decisions.decision_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("disposition IN ('include','exclude','uncertain')", name="ck_decision_disposition"),
        # An exclusion must cite a reason. `ER-005.4` requires exclusion-reason
        # semantics to be preserved in the flow ledger; a reasonless exclusion
        # would make a PRISMA diagram that cannot say why anything was dropped.
        sa.CheckConstraint(
            "(disposition <> 'exclude') OR (btrim(coalesce(exclusion_reason_code,'')) <> '')",
            name="ck_decision_exclusion_has_reason",
        ),
        sa.CheckConstraint("btrim(reviewer_subject_id) <> ''", name="ck_decision_reviewer_not_blank"),
    )
    op.execute(_grant("screening_decisions"))
    op.execute(_append_only("screening_decisions", "a correction supersedes; a decision is never overwritten"))

    op.execute(
        """
        CREATE UNIQUE INDEX uq_decision_one_current_per_reviewer
          ON screening_decisions (review_id, stage_id, round_number, review_record_id, reviewer_subject_id)
          WHERE supersedes_decision_id IS NULL;

        CREATE UNIQUE INDEX uq_decision_supersession_linear
          ON screening_decisions (supersedes_decision_id)
          WHERE supersedes_decision_id IS NOT NULL;
        """
    )

    # ---- conflicts: derived, immutable -----------------------------------
    op.create_table(
        "screening_conflicts",
        sa.Column("conflict_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stage_id"], ["review_stages.stage_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        # `ER-004.4` — "create ONE deterministic conflict under disagreement".
        # The uniqueness is what makes it one; the service inserts with
        # ON CONFLICT DO NOTHING, so two concurrent second-decisions produce a
        # single row rather than a race.
        sa.UniqueConstraint("review_id", "stage_id", "round_number", "review_record_id", name="uq_conflict_once"),
    )
    op.execute(_tenant_rls("screening_conflicts"))
    op.execute(_grant("screening_conflicts"))
    op.execute(_append_only("screening_conflicts", "resolution is an adjudication row, not a status update"))

    # ---- adjudications ----------------------------------------------------
    op.create_table(
        "screening_adjudications",
        sa.Column("adjudication_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conflict_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("exclusion_reason_code", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("adjudicator_subject_id", sa.Text(), nullable=False),
        sa.Column("supersedes_adjudication_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conflict_id"], ["screening_conflicts.conflict_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_adjudication_id"], ["screening_adjudications.adjudication_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("disposition IN ('include','exclude','uncertain')", name="ck_adjudication_disposition"),
        sa.CheckConstraint(
            "(disposition <> 'exclude') OR (btrim(coalesce(exclusion_reason_code,'')) <> '')",
            name="ck_adjudication_exclusion_has_reason",
        ),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_adjudication_rationale_not_blank"),
        sa.CheckConstraint("btrim(adjudicator_subject_id) <> ''", name="ck_adjudication_adjudicator_not_blank"),
    )
    op.execute(_tenant_rls("screening_adjudications"))
    op.execute(_grant("screening_adjudications"))
    op.execute(_append_only("screening_adjudications", "a revised adjudication supersedes the previous one"))
    op.execute(
        """
        CREATE UNIQUE INDEX uq_adjudication_one_current_per_conflict
          ON screening_adjudications (conflict_id)
          WHERE supersedes_adjudication_id IS NULL;

        CREATE UNIQUE INDEX uq_adjudication_supersession_linear
          ON screening_adjudications (supersedes_adjudication_id)
          WHERE supersedes_adjudication_id IS NOT NULL;
        """
    )

    # `01-…:659` — separation of duty. An adjudicator who is one of the
    # conflicting reviewers is deciding their own disagreement, so this is
    # refused in the database rather than in a permission check the API could be
    # routed around. It reads the decisions table directly, which is why the
    # function is SECURITY DEFINER: the adjudicating lead may not be able to see
    # every conflicting decision under the blinding policy, and the check must
    # not depend on what they can see.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION screening_adjudications_separation_of_duty() RETURNS trigger AS $$
        DECLARE
          took_part boolean;
        BEGIN
          SELECT EXISTS (
            SELECT 1
              FROM screening_conflicts c
              JOIN screening_decisions d
                ON d.review_id = c.review_id
               AND d.stage_id = c.stage_id
               AND d.round_number = c.round_number
               AND d.review_record_id = c.review_record_id
             WHERE c.conflict_id = NEW.conflict_id
               AND d.reviewer_subject_id = NEW.adjudicator_subject_id
          ) INTO took_part;

          IF took_part THEN
            RAISE EXCEPTION
              'adjudicator % recorded a decision in this conflict and cannot adjudicate it',
              NEW.adjudicator_subject_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;

        CREATE TRIGGER trg_screening_adjudications_separation_of_duty
          BEFORE INSERT ON screening_adjudications
          FOR EACH ROW EXECUTE FUNCTION screening_adjudications_separation_of_duty();
        """
    )

    # ---- unblinding audit -------------------------------------------------
    op.create_table(
        "screening_unblinding_events",
        sa.Column("unblinding_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conflict_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("actor_subject_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["conflict_id"], ["screening_conflicts.conflict_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_unblinding_reason_not_blank"),
        sa.CheckConstraint("btrim(actor_subject_id) <> ''", name="ck_unblinding_actor_not_blank"),
    )
    op.execute(_tenant_rls("screening_unblinding_events"))
    op.execute(_grant("screening_unblinding_events"))
    op.execute(_append_only("screening_unblinding_events", "the audit trail of who looked is the whole point"))

    # ---- L1: the blinding policy ------------------------------------------
    # Written AFTER the unblinding table exists, because the third clause reads
    # it. Tenant isolation is folded in rather than added as a second policy:
    # multiple permissive policies are OR-ed, so a separate tenant policy would
    # make a cross-tenant read of one's own subject id succeed.
    op.execute(
        f"""
        ALTER TABLE screening_decisions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE screening_decisions FORCE ROW LEVEL SECURITY;

        CREATE POLICY screening_decisions_blinded ON screening_decisions
          USING (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND (
              reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
              OR current_setting('{ROLE_GUC}', true) = 'lead'
              OR EXISTS (
                SELECT 1
                  FROM screening_unblinding_events u
                  JOIN screening_conflicts c ON c.conflict_id = u.conflict_id
                 WHERE c.review_id = screening_decisions.review_id
                   AND c.stage_id = screening_decisions.stage_id
                   AND c.round_number = screening_decisions.round_number
                   AND c.review_record_id = screening_decisions.review_record_id
              )
            )
          )
          WITH CHECK (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
          );
        """
    )

    # ---- flow ledger -------------------------------------------------------
    op.create_table(
        "flow_revisions",
        sa.Column("flow_revision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("review_id", "revision_number", name="uq_flow_revision_number"),
    )
    op.execute(_tenant_rls("flow_revisions"))
    op.execute(_grant("flow_revisions"))

    op.create_table(
        "flow_projections",
        sa.Column("projection_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("flow_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_counts_hash", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["flow_revision_id"], ["flow_revisions.flow_revision_id"], ondelete="RESTRICT"),
    )
    op.execute(_tenant_rls("flow_projections"))
    op.execute(_grant("flow_projections"))
    op.create_index("ix_flow_projections_review", "flow_projections", ["review_id", "computed_at"])

    # ---- the sealed inclusion-set export -----------------------------------
    op.create_table(
        "inclusion_exports",
        sa.Column("export_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("flow_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("corpus_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        # TWO different hashes of two different things (`16-…:357-364`,
        # `DM-AT-28`). `manifest_hash` is the RFC 8785 canonical hash of the
        # metadata envelope — stable across serializations. `payload_digest` is
        # sha256 of the exact bytes handed to the caller. Substituting one for
        # the other passes a tamper check it should fail, so the two columns
        # exist and neither is derivable from the other.
        sa.Column("manifest_hash", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # NOT NULL, deliberately. `ER-009.3` / `01-…:626-627` — never present a
        # filtered export as silently complete. An empty list is an assertion
        # that nothing was omitted; NULL would be silence.
        sa.Column("omissions", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["flow_revision_id"], ["flow_revisions.flow_revision_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["corpus_version_id"], ["corpus_versions.corpus_version_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("jsonb_typeof(omissions) = 'array'", name="ck_export_omissions_is_array"),
        sa.CheckConstraint("manifest_hash ~ '^sha256:[0-9a-f]{64}$'", name="ck_export_manifest_hash_shape"),
        sa.CheckConstraint("payload_digest ~ '^sha256:[0-9a-f]{64}$'", name="ck_export_payload_digest_shape"),
        # If these were ever equal it would mean one had been written into the
        # other's column — the substitution `DM-AT-28` warns about.
        sa.CheckConstraint("manifest_hash <> payload_digest", name="ck_export_hashes_are_distinct"),
    )
    op.execute(_tenant_rls("inclusion_exports"))
    op.execute(_grant("inclusion_exports"))
    op.execute(_append_only("inclusion_exports", "a sealed export is evidence; a new export is a new row"))

    op.create_index("ix_decisions_record", "screening_decisions", ["review_id", "stage_id", "review_record_id"])
    op.create_index("ix_conflicts_review", "screening_conflicts", ["review_id", "stage_id", "round_number"])
    op.create_index("ix_unblinding_conflict", "screening_unblinding_events", ["conflict_id"])


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS screening_decisions_blinded ON screening_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_screening_adjudications_separation_of_duty ON screening_adjudications")
    op.execute("DROP FUNCTION IF EXISTS screening_adjudications_separation_of_duty()")
    for table in (
        "inclusion_exports",
        "screening_unblinding_events",
        "screening_adjudications",
        "screening_conflicts",
        "screening_decisions",
        "screening_assignments",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
    op.drop_table("inclusion_exports")
    op.drop_table("flow_projections")
    op.drop_table("flow_revisions")
    op.drop_table("screening_unblinding_events")
    op.drop_table("screening_adjudications")
    op.drop_table("screening_conflicts")
    op.drop_table("screening_decisions")
    op.drop_table("screening_assignments")
