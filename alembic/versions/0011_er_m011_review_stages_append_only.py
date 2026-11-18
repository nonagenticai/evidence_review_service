"""`review_stages` is append-only, and the runtime role may not rewrite it.

`flow.py` states "`review_stages` is append-only" as a fact and reasons from it —
that is why `_position_of` treats an unplaceable stage as a claim about the
database being intact rather than a branch anybody hits. It was not true.
Measured on `0010`:

    SELECT tgname FROM pg_trigger
     WHERE tgrelid='review_stages'::regclass AND NOT tgisinternal;
    (0 rows)                       -- the only history table in the schema with none

    grantee=evidence_review_app on review_stages: SELECT, INSERT, UPDATE, DELETE

That was inert until `#42`, which made `stage_order` decide which stage settles a
record and therefore load-bearing for `canonical_counts_hash` — a value sealed
into an append-only export. Anything holding the app credential could rewrite two
rows and invert a sealed export's counts, and `:verify` would still answer
`verified`, because it re-hashes the stored payload and has no way to know the
stage order moved underneath it.

Both halves are needed and neither substitutes for the other. The trigger stops
the owner too (`FORCE ROW LEVEL SECURITY` does not apply to DDL-level privilege,
and migrations connect as the owner); the REVOKE means the runtime role is
refused before the trigger is even reached, so the error a service bug produces
names a permission rather than a business rule.

There is no data change and no backfill: this migration only adds guards.

Revision ID: 0011_er_m011_stages
Revises: 0010_queue_progress
"""

from __future__ import annotations

from alembic import op

revision = "0011_er_m011_stages"
down_revision = "0010_queue_progress"
branch_labels = None
depends_on = None

REASON = "a stage that ordered a recorded judgement cannot be re-ordered afterwards"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION review_stages_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'review_stages is append-only: {REASON}'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_review_stages_append_only ON review_stages;
        CREATE TRIGGER trg_review_stages_append_only
          BEFORE UPDATE OR DELETE ON review_stages
          FOR EACH ROW EXECUTE FUNCTION review_stages_append_only();
        """
    )
    # Named explicitly rather than `REVOKE ALL` + re-GRANT: the runtime role must
    # keep SELECT and INSERT, and a blanket revoke followed by a partial grant is
    # one edit away from taking the service offline.
    #
    # Guarded on the role existing, the same shape `0001` and `0002` use for
    # their grants: the app role is provisioned in CI and by the deployment, not
    # by the migration, so an unguarded REVOKE fails the upgrade on any database
    # that does not have it yet.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            REVOKE UPDATE, DELETE ON review_stages FROM evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; REVOKE skipped.';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT UPDATE, DELETE ON review_stages TO evidence_review_app;
          END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_review_stages_append_only ON review_stages")
    op.execute("DROP FUNCTION IF EXISTS review_stages_append_only()")
