"""ER-M001 foundation: idempotency, and the tenant/actor RLS pattern every later table follows.

Revision ID: 0001_er_m001
Revises:
Create Date: 2026-08-08

This is the baseline. It carries exactly one real table rather than being an
empty no-op, because the point of the first migration is to prove the parts that
are expensive to discover late: that migrations run as the owner, that the app
role can read and write but cannot disable a policy, and that the row-level
security predicate reads the same GUC names `src/actor.py` binds.

`operation_idempotency`'s key is the one `01-evidence-review-8810.md:693-696`
specifies: scope, tenant, principal, operation and idempotency key together. The
principal is part of the key deliberately — two reviewers replaying the same
client-generated key are two different facts, and collapsing them would let one
reviewer's response be served to another.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_er_m001"
down_revision = None
branch_labels = None
depends_on = None

# Repeated from src/actor.py. tests/unit/test_actor.py pins the names there and
# tests/integration pins this migration against them, so the two cannot drift
# into a policy that reads a GUC nobody sets — which matches no rows, silently.
TENANT_GUC = "app.current_tenant"


def upgrade() -> None:
    op.create_table(
        "operation_idempotency",
        sa.Column("resource_scope", sa.Text(), nullable=False),
        sa.Column("resource_tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_subject_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # The fingerprint of the request that FIRST used this key. A replay with
        # the same key and a different body is a conflict, not a replay: serving
        # the original response would silently discard the second request.
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_headers", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint(
            "resource_scope",
            "resource_tenant_id",
            "principal_subject_id",
            "operation_id",
            "idempotency_key",
            name="pk_operation_idempotency",
        ),
    )

    # A record of a human judgement is never anonymous, at any layer.
    op.create_check_constraint(
        "ck_operation_idempotency_principal_not_blank",
        "operation_idempotency",
        "btrim(principal_subject_id) <> ''",
    )

    # ---- tenant isolation -------------------------------------------------
    # FORCE, not just ENABLE: ENABLE alone exempts the table owner, and the owner
    # is who migrations and any future maintenance script connect as. The runtime
    # role is a NON-owner precisely so it cannot turn this off.
    op.execute("ALTER TABLE operation_idempotency ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE operation_idempotency FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY operation_idempotency_tenant ON operation_idempotency
          USING (resource_tenant_id = current_setting('{TENANT_GUC}', true))
          WITH CHECK (resource_tenant_id = current_setting('{TENANT_GUC}', true))
        """
    )

    # The app role gets DML and nothing else. Default privileges already grant
    # this for future tables; stating it here makes the first table's grants
    # explicit rather than dependent on the order roles were created in.
    #
    # CONDITIONAL, because the role is provisioned out of band — it exists on
    # worker4, and does not exist in CI's per-job ephemeral postgres:18. An
    # unconditional GRANT made `alembic upgrade head` fail in CI with "role
    # does not exist", which is a migration asserting a fact about its
    # environment rather than about the schema. The environments that HAVE the
    # role still get the grant; the ones that do not are not silently skipped —
    # they raise a NOTICE naming the role, so a production deploy against a
    # database where the role was never created is visible in the migration log
    # instead of surfacing later as a permission denial at runtime.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON operation_idempotency TO evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; DML grant skipped. Expected in CI, NOT in production.';
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS operation_idempotency_tenant ON operation_idempotency")
    op.drop_table("operation_idempotency")
