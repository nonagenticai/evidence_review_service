"""ER-M023: create the cross-tenant worker policies that 0020 and 0022 skipped.

Revision ID: 0023_er_m023_worker_policies
Revises: 0022_er_m022_research_event_path

## The defect this repairs, which is a defect in ORDERING, not in either migration

`0020` and `0022` both grant an ADDITIVE row-level-security policy to one
restricted role, `evidence_review_worker`, so a relay and a lease reaper can see
every tenant's rows in one pass. Both wrapped that grant in a guard:

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_worker') THEN
      ...
    ELSE
      RAISE NOTICE 'role evidence_review_worker is absent; ... skipped'

The guard is correct — a migration that fails because an operational role has not
been created yet is a migration that blocks a deploy on a chicken-and-egg. But
the consequence was never closed: the role does not exist in production, measured
on worker4 on 2026-09-04, so BOTH migrations took the `ELSE` branch, emitted a
NOTICE into a log nobody reads, and completed successfully.

**Creating the role now does not retroactively create the policies.** A migration
runs once. `alembic_version` sits at `0022`, `0020` and `0022` will never execute
again, and their guard has already made its decision. So the estate could
provision the role, restart everything, and still have a worker that reads zero
rows across every table — with `pg_roles` showing the role present, which is
exactly the evidence that would make an operator stop looking.

This migration exists to be the thing that runs AFTER the role exists. It is
idempotent, so it is safe to run before the role exists too (it does nothing), and
safe to run twice.

## Why not fold this into 0022

`0022` is applied in production. Editing an applied migration changes what a fresh
database gets while leaving production untouched, which is how the two diverge
silently — and the divergence would be invisible precisely because both would
report `alembic_version = 0022`.

## Why the policy is created with a name check rather than CREATE POLICY IF NOT EXISTS

Postgres has no `CREATE POLICY IF NOT EXISTS`. The `pg_policies` lookup is the
supported spelling, and it also makes the migration honest about the case it is
built for: a database where SOME of these policies exist (created by 0020 when the
role happened to be present) and some do not.
"""

from __future__ import annotations

from alembic import op

revision = "0023_er_m023_worker_policies"
down_revision = "0022_er_m022_research_event_path"
branch_labels = None
depends_on = None

WORKER_ROLE = "evidence_review_worker"

#: Every table the relay or the reaper must read across tenants. The first two are
#: `0020`'s (durable jobs), the last four are `0022`'s (the research event path).
#: `projection_checkpoints` is deliberately absent: no projector exists, and a
#: policy for a reader nobody has written is a grant with no purpose to review.
WORKER_TABLES: tuple[str, ...] = (
    "durable_jobs",
    "job_attempts",
    "domain_outbox",
    "research_domain_events",
    "event_deliveries",
    "domain_inbox",
)


def _create_policy(table: str) -> str:
    """Create `<table>_worker` if the role exists and the policy does not."""
    return f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
            RAISE NOTICE 'role {WORKER_ROLE} is absent; cross-tenant policy on {table} '
                         'still skipped. Create the role, then run this migration again — '
                         'it is idempotent.';
          ELSIF EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = '{table}' AND policyname = '{table}_worker'
          ) THEN
            RAISE NOTICE 'policy {table}_worker already exists; nothing to do.';
          ELSE
            EXECUTE 'CREATE POLICY {table}_worker ON {table} TO {WORKER_ROLE} '
                    'USING (true) WITH CHECK (true)';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {table} TO {WORKER_ROLE}';
            RAISE NOTICE 'created policy {table}_worker.';
          END IF;
        END
        $$;
    """


def _drop_policy(table: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = '{table}' AND policyname = '{table}_worker'
          ) THEN
            EXECUTE 'DROP POLICY {table}_worker ON {table}';
          END IF;
        END
        $$;
    """


def _connect_and_usage() -> str:
    """Let the role reach the schema at all.

    A table grant is worthless without `CONNECT` on the database and `USAGE` on
    the schema, and the failure mode of omitting them is unhelpful: the role
    authenticates, then every statement fails on permission for `public`, which
    reads like a broken query rather than a missing grant.

    `current_database()` rather than a literal, because this migration runs
    against the CI ephemeral database as well as production and the two are not
    called the same thing. That is also why it is `EXECUTE format(...)` — GRANT
    does not take an expression for the database name.
    """
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {WORKER_ROLE}', current_database());
            EXECUTE 'GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}';
          END IF;
        END
        $$;
    """


def upgrade() -> None:
    op.execute(_connect_and_usage())
    for table in WORKER_TABLES:
        op.execute(_create_policy(table))


def downgrade() -> None:
    """Drop the policies, and deliberately NOT the role.

    A downgrade removes what this migration added. The role is operational state
    owned by the host configuration, not by the schema, and dropping it here would
    let a schema rollback take a credential other things may depend on.

    ⚠️ Downgrading strands any running worker: it will keep its connection, read
    zero rows across every table, and report success. The worker's own preflight
    catches this on its next start, which is the reason that preflight refuses
    rather than warns.
    """
    for table in reversed(WORKER_TABLES):
        op.execute(_drop_policy(table))
