"""ER-M022 the research event path: log, deliveries, inbox and checkpoints.

Revision ID: 0022_er_m022
Revises: 0021_er_m021
Create Date: 2026-09-03

`FND-007` — "research event v2 outbox, relay, delivery and inbox"
(`13-…:§17`), item **9** of the `15-…:§14` backlog, directly after `FND-006`
(item 8, merged and live). Its only dependency is `FND-003`, which shipped as
`ER-M019`.

## Where this sits in the six steps

`13-…:§8.1` is a six-step path. `0019` built step 1 and said so:

> ⛔ Nothing relays these rows. There is no relay, no delivery record and no
> consumer. `13-…:§8.1` is a six-step path and this is step 1 of six.

This migration builds the tables for steps 2, 3, 4 and 6:

| Step | What | Where |
|---|---|---|
| 1 | producer commits an envelope to its local outbox | `domain_outbox` (`0019`) |
| 2 | a relay inserts it **idempotently** into a durable log | `research_domain_events` |
| 3 | the relay creates delivery records for registered consumers | `event_deliveries` |
| 4 | a consumer claims, checks ordering, records an inbox receipt **in the same transaction as its local effect** | `domain_inbox` |
| 5 | it acknowledges after commit | `event_deliveries.acknowledged_at` |
| 6 | replay recreates deliveries **without changing `event_id`** | delivery has its own key; `event_id` is never rewritten |

`projection_checkpoints` comes from the same `13-…:§5.2` row list and is what
makes lag and repair measurable rather than anecdotal.

## ⛔ NOTHING IS EMITTED, and that is not an accident

`13-…:§8.3`: "No service emits a listed document `09` event until its schema,
example, consumer matrix and compatibility test are merged." Those four
artifacts exist for **zero** of the twenty-one events today — only the
ENVELOPE (`urn:datacompany:research-event-envelope:1`) is released.

So the consumer registry (`src/domain/policies/consumer_registry.py`) is
**empty**, the relay creates deliveries only for registered consumers, and a
registry with no entries produces no deliveries. Nothing leaves the service,
so nothing is emitted, so the gate is not crossed — the same posture `0019`
took, for the same reason, one step further along.

`tests/unit/test_no_consumer_outruns_its_schema.py` makes that a tripwire
rather than a promise: registering a consumer for an event whose payload schema
is not released fails the suite.

## Why a per-consumer delivery row and NOT a flag on the event

`13-…:§8.1`, verbatim: "A single global `consumed_at` flag is not sufficient
because several projections and services may consume the same event."

So there is deliberately no `consumed_at`, `relayed_at` or `delivered` column
anywhere on `domain_outbox` or on `research_domain_events`. Progress lives in
`event_deliveries`, one row per (event, consumer). The temptation to add a
boolean to the event is exactly what the document forbids, and it would look
like a harmless denormalisation right up until the second consumer arrives.

## Ordering is a stored key, not a convention

`13-…:§8.2`: "Ordering is guaranteed per `(resource_scope, resource_tenant,
aggregate_type, aggregate_id)`, using `aggregate_version`." That is exactly the
tuple `0019` already indexed on the outbox (`ix_domain_outbox_aggregate`),
which is why the relay can read in order without a sort.

`ix_research_domain_events_order` carries the same key here, and
`uq_event_deliveries_consumer_event` makes "one delivery per consumer per
event" a database fact so a replay that ran twice cannot double-deliver.

`deferred` is a first-class delivery state because `§8.2` requires it:
"Consumers defer a future version when an earlier event is missing." A
consumer that cannot yet apply version 7 must be able to say so and be retried,
which is not the same as failing.

## Poison is a state, not a deletion

`§8.2`: "An incompatible schema, impossible transition or repeated handler
failure enters a rights-safe poison queue and pages the owning team."
`poisoned` is therefore a terminal delivery state carrying a typed reason and a
bounded detail — never the payload, because a poisoned event is precisely the
one most likely to be copied into an alert.

⚠️ Nothing pages yet. There is no alert rule and no runbook wired to this
state; `15-…:§…` lists "outbox age, delivery lag, inbox duplicate and poison
event" among the dashboards owed. The state exists so the metric can, and the
README says the paging half is not built.

## RLS, and the relay's cross-tenant read

`13-…:§5.1` excludes operational tables from tenant RLS so a cross-tenant relay
can discover work. `0019` declined that for `domain_outbox` and recorded the way
out; `0020` took it — `FORCE ROW LEVEL SECURITY` plus an ADDITIVE policy scoped
to `evidence_review_worker`. This migration does the same for all four tables,
so the estate never has a window where a queue is world-readable.

⚠️ `evidence_review_worker` does not exist in production — measured on worker4
on 2026-09-03 — so those policies are skipped with a NOTICE and the tables are
per-tenant only. That is the safe default and it is also why the relay cannot
yet run cross-tenant. The role is a provisioning task, not a code change.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022_er_m022_research_event_path"
down_revision = "0021_er_m021_evidence_snapshots"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

#: `13-…:§5.1`'s "distinct restricted worker/relay role". Same principal
#: `0020` reserved; a relay and a job claimant have identical needs — read
#: operational rows across tenants, then rebind to the recorded tenant before
#: touching a domain table.
WORKER_ROLE = "evidence_review_worker"

#: Delivery lifecycle. `deferred` is required by `§8.2` (a consumer defers a
#: future version when an earlier event is missing) and is NOT a failure.
#: `poisoned` is terminal and pages — see the module docstring on what is not
#: built yet.
DELIVERY_STATES: tuple[str, ...] = (
    "pending",
    "claimed",
    "deferred",
    "acknowledged",
    "poisoned",
)

#: Terminal delivery states. `acknowledged` is the success terminal;
#: `poisoned` the failure one. A replay may create a NEW delivery for the same
#: event, which is why terminality is per-delivery and not per-event.
TERMINAL_DELIVERY_STATES: tuple[str, ...] = ("acknowledged", "poisoned")

#: Why a delivery was poisoned. Closed, because "other" in a failure taxonomy
#: is where every unexamined failure goes to be forgotten.
POISON_REASONS: tuple[str, ...] = (
    "incompatible_schema",
    "impossible_transition",
    "handler_failed_repeatedly",
    "unknown_consumer",
)

#: What an inbox receipt records. `13-…:§5.2` — "consumer/event receipt,
#: processing state and outcome".
INBOX_OUTCOMES: tuple[str, ...] = ("applied", "skipped_duplicate", "skipped_not_applicable")

#: Read by `tests/conftest.py` — see `0017.APPEND_ONLY_TABLES`.
#: `research_domain_events` is append-only: a durable log whose rows can be
#: edited is not one. The other three MUTATE by design (a delivery is claimed
#: then acknowledged; a checkpoint advances), so they are DELETE-revoked only.
APPEND_ONLY_TABLES: tuple[str, ...] = ("research_domain_events",)

#: Read by `tests/conftest.py`. Mutable, but never deletable: a delivery that
#: can be deleted makes "did this consumer see it?" unanswerable, and a deleted
#: inbox receipt reopens the exactly-once window it exists to close.
DELETE_REVOKED_TABLES: tuple[str, ...] = (
    "event_deliveries",
    "domain_inbox",
    "projection_checkpoints",
)

_TOKEN_SHAPE = r"^[a-z][a-z0-9_]*$"
_EVENT_NAME_SHAPE = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$"
_REGULATORY_DENYLIST = "regulatory|legal_|normative|consolidation"
MINIMAL_FACT_MAX = 512


def _tenant_rls(table: str) -> str:
    """Verbatim from `0019`/`0020`/`0021`, kept per-migration for their reason."""
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = {_TENANT_COMPARAND})
          WITH CHECK (tenant_id = {_TENANT_COMPARAND});
    """


def _worker_policy(table: str) -> str:
    """Cross-tenant discovery, granted ADDITIVELY to one restricted role.

    Verbatim from `0020`. Postgres ORs permissive policies, so this widens the
    table for `evidence_review_worker` and for nobody else.
    """
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
            EXECUTE 'CREATE POLICY {table}_worker ON {table} TO {WORKER_ROLE} '
                    'USING (true) WITH CHECK (true)';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {table} TO {WORKER_ROLE}';
          ELSE
            RAISE NOTICE 'role {WORKER_ROLE} is absent; cross-tenant relay policy on '
                         '{table} skipped — the log is visible per-tenant only.';
          END IF;
        END
        $$;
    """


def _grant(table: str, privileges: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT {privileges} ON {table} TO evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; DML grant on {table} skipped.';
          END IF;
        END
        $$;
    """


def _revoke(table: str, privileges: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            REVOKE {privileges} ON {table} FROM evidence_review_app;
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


def _no_delete(table: str, reason: str) -> str:
    fn = f"{table}_no_delete"
    return f"""
        CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is not deletable: {reason}'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_{fn}
          BEFORE DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION {fn}();
    """


def upgrade() -> None:
    states = ", ".join(f"'{s}'" for s in DELIVERY_STATES)
    terminal = ", ".join(f"'{s}'" for s in TERMINAL_DELIVERY_STATES)
    poisons = ", ".join(f"'{p}'" for p in POISON_REASONS)
    outcomes = ", ".join(f"'{o}'" for o in INBOX_OUTCOMES)

    # ── step 2: the durable log ────────────────────────────────────────────
    op.create_table(
        "research_domain_events",
        # ⚠️ The SAME `event_id` the outbox minted. `§8.1` step 6 — "replay
        # recreates deliveries without changing `event_id`" — is only meaningful
        # if the id survives the hop, so this is the primary key rather than a
        # new surrogate with the outbox id beside it.
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Monotonic position, for replay from a checkpoint. A timestamp cannot
        # serve: two events committed in the same transaction share `now()`, and
        # a consumer resuming "after time T" would either repeat or skip them.
        #
        # ⚠️ `Identity(always=True)`, not `autoincrement=True`. The latter does
        # nothing on a non-primary-key column — alembic emits a plain
        # `BIGINT NOT NULL` with no sequence, and the relay's INSERT (which does
        # not name the column) fails on the NOT NULL. Caught locally by running
        # it; it would otherwise have been a 500 on the first relay pass.
        #
        # `ALWAYS` rather than `BY DEFAULT` is the stricter choice and the right
        # one: a log position is the database's to assign, and a caller that
        # could supply one could place an event anywhere in a replay ordering
        # that consumers resume from.
        sa.Column("log_position", sa.BigInteger(), sa.Identity(always=True), nullable=False, unique=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("producer", sa.Text(), nullable=False),
        sa.Column("resource_scope", sa.Text(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("relayed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"event_type ~ '{_EVENT_NAME_SHAPE}'", name="ck_rde_event_name_shape"),
        sa.CheckConstraint(f"event_type !~ '{_REGULATORY_DENYLIST}'", name="ck_rde_event_name_not_regulatory"),
        sa.CheckConstraint("resource_scope IN ('global', 'tenant')", name="ck_rde_scope"),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_rde_aggregate_version"),
        sa.CheckConstraint(f"aggregate_type ~ '{_TOKEN_SHAPE}'", name="ck_rde_aggregate_type"),
        # The SAME recursive bound `0019` installed. A log entry is a copy of an
        # outbox row, so it inherits the rule rather than restating it — and if
        # the two ever disagreed, the log would be the place a payload got
        # bigger on the way past.
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND er_payload_is_minimal(payload)",
            name="ck_rde_payload_is_minimal",
        ),
        sa.CheckConstraint(f"content_hash IS NULL OR content_hash ~ '{HASH_SHAPE}'", name="ck_rde_content_hash_shape"),
    )
    # `§8.2`'s ordering key, exactly. The relay and every consumer read in this
    # order; a sort at read time would be a second definition of "in order".
    op.create_index(
        "ix_research_domain_events_order",
        "research_domain_events",
        ["resource_scope", "tenant_id", "aggregate_type", "aggregate_id", "aggregate_version"],
    )
    op.create_index("ix_research_domain_events_position", "research_domain_events", ["log_position"])

    # ── step 3/5: per-consumer delivery ────────────────────────────────────
    op.create_table(
        "event_deliveries",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_domain_events.event_id"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        # The claim, mirroring `durable_jobs`: a worker id and a bounded lease.
        # Deliberately the same shape, because a delivery IS work and a second
        # vocabulary for claiming would be a second set of bugs.
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        # `§8.2`: a consumer defers a future version. `defer_reason` is typed and
        # `retry_after` is when to look again — a deferral with neither is
        # indistinguishable from a stall.
        sa.Column("defer_reason", sa.Text(), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poison_reason", sa.Text(), nullable=True),
        sa.Column("poison_detail", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        # `§8.1` step 6: replay recreates deliveries. This says WHICH replay
        # made this one, so "why do I have two deliveries for one event" has an
        # answer that is not "somebody ran something twice".
        sa.Column("replay_of_delivery_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"state IN ({states})", name="ck_event_deliveries_state"),
        sa.CheckConstraint(f"consumer ~ '{_TOKEN_SHAPE}'", name="ck_event_deliveries_consumer_shape"),
        sa.CheckConstraint(
            "max_attempts >= 1 AND attempt >= 0 AND attempt <= max_attempts",
            name="ck_event_deliveries_attempt_within_policy",
        ),
        # `claimed` ⇔ somebody holds a lease. The reverse half is what makes a
        # reaper's population decidable — the argument `0020` makes for jobs.
        sa.CheckConstraint(
            "(state = 'claimed') = (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_event_deliveries_claimed_holds_lease",
        ),
        sa.CheckConstraint(
            "(state = 'deferred') = (defer_reason IS NOT NULL AND retry_after IS NOT NULL)",
            name="ck_event_deliveries_deferral_declares_why",
        ),
        sa.CheckConstraint(
            "(state = 'poisoned') = (poison_reason IS NOT NULL)",
            name="ck_event_deliveries_poison_names_itself",
        ),
        sa.CheckConstraint(
            f"poison_reason IS NULL OR poison_reason IN ({poisons})",
            name="ck_event_deliveries_poison_reason_registered",
        ),
        sa.CheckConstraint(
            f"poison_detail IS NULL OR length(poison_detail) <= {MINIMAL_FACT_MAX}",
            name="ck_event_deliveries_poison_detail_bounded",
        ),
        sa.CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name="ck_event_deliveries_ack_is_stamped",
        ),
        sa.CheckConstraint(
            f"state NOT IN ({terminal}) OR lease_expires_at IS NULL",
            name="ck_event_deliveries_terminal_holds_no_lease",
        ),
    )
    # One delivery per consumer per event — UNLESS it is a replay, which is
    # explicitly allowed to create another (`§8.1` step 6). Partial on
    # `replay_of_delivery_id IS NULL` so the original is unique and replays are
    # additive rather than blocked.
    op.create_index(
        "uq_event_deliveries_consumer_event",
        "event_deliveries",
        ["event_id", "consumer"],
        unique=True,
        postgresql_where=sa.text("replay_of_delivery_id IS NULL"),
    )
    op.create_index(
        "ix_event_deliveries_claimable",
        "event_deliveries",
        ["consumer", "state", "created_at"],
        postgresql_where=sa.text("state IN ('pending', 'deferred')"),
    )
    op.create_index(
        "ix_event_deliveries_lease",
        "event_deliveries",
        ["lease_expires_at"],
        postgresql_where=sa.text("state = 'claimed'"),
    )

    # ── step 4: the consumer's receipt ─────────────────────────────────────
    op.create_table(
        "domain_inbox",
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # ⚠️ THE exactly-once constraint. `§8.1` step 4 puts the receipt in the
        # same transaction as the local effect, so this unique index is what
        # makes a repeated delivery a no-op rather than a second effect. Without
        # it every other guarantee here is advisory.
        sa.UniqueConstraint("consumer", "event_id", name="uq_domain_inbox_consumer_event"),
        sa.CheckConstraint(f"consumer ~ '{_TOKEN_SHAPE}'", name="ck_domain_inbox_consumer_shape"),
        sa.CheckConstraint(f"outcome IN ({outcomes})", name="ck_domain_inbox_outcome"),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_domain_inbox_aggregate_version"),
        sa.CheckConstraint(
            f"detail IS NULL OR length(detail) <= {MINIMAL_FACT_MAX}",
            name="ck_domain_inbox_detail_bounded",
        ),
    )
    # A consumer asking "what is the newest version of this aggregate I have
    # applied?" — the question `§8.2`'s deferral rule is answered with.
    op.create_index(
        "ix_domain_inbox_progress",
        "domain_inbox",
        ["consumer", "tenant_id", "aggregate_type", "aggregate_id", "aggregate_version"],
    )

    # ── lag and repair ─────────────────────────────────────────────────────
    op.create_table(
        "projection_checkpoints",
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # The log position, not a timestamp — see `research_domain_events`.
        sa.Column("event_position", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lag_events", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("repair_state", sa.Text(), nullable=False, server_default=sa.text("'healthy'")),
        sa.Column("repair_detail", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("consumer", "tenant_id", "index_version", name="uq_projection_checkpoint"),
        sa.CheckConstraint(f"consumer ~ '{_TOKEN_SHAPE}'", name="ck_projection_checkpoints_consumer_shape"),
        sa.CheckConstraint("index_version >= 1", name="ck_projection_checkpoints_index_version"),
        sa.CheckConstraint("event_position >= 0", name="ck_projection_checkpoints_position"),
        sa.CheckConstraint("lag_events >= 0", name="ck_projection_checkpoints_lag"),
        sa.CheckConstraint(
            "repair_state IN ('healthy', 'repairing', 'stalled')",
            name="ck_projection_checkpoints_repair_state",
        ),
        sa.CheckConstraint(
            f"repair_detail IS NULL OR length(repair_detail) <= {MINIMAL_FACT_MAX}",
            name="ck_projection_checkpoints_detail_bounded",
        ),
    )

    every = ("research_domain_events", "event_deliveries", "domain_inbox", "projection_checkpoints")
    for table in every:
        op.execute(_tenant_rls(table))
        op.execute(_worker_policy(table))

    op.execute(_grant("research_domain_events", "SELECT, INSERT"))
    op.execute(_revoke("research_domain_events", "UPDATE, DELETE"))
    op.execute(
        _append_only(
            "research_domain_events",
            "a durable log whose rows can be edited after the fact is not a log, and replay "
            "would reproduce something that never happened",
        )
    )

    for table in DELETE_REVOKED_TABLES:
        op.execute(_grant(table, "SELECT, INSERT, UPDATE"))
        op.execute(_revoke(table, "DELETE"))

    op.execute(
        _no_delete(
            "event_deliveries",
            "a deletable delivery makes the question of whether this consumer ever saw the event unanswerable",
        )
    )
    op.execute(
        _no_delete(
            "domain_inbox",
            "a deleted receipt reopens the exactly-once window it exists to close",
        )
    )
    op.execute(
        _no_delete(
            "projection_checkpoints",
            "a deleted checkpoint silently restarts a consumer from the beginning of the log",
        )
    )


def downgrade() -> None:
    for table in ("projection_checkpoints", "domain_inbox", "event_deliveries"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_no_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_research_domain_events_append_only ON research_domain_events")
    op.execute("DROP FUNCTION IF EXISTS research_domain_events_append_only()")
    for table in ("projection_checkpoints", "domain_inbox", "event_deliveries", "research_domain_events"):
        op.execute(f"DROP POLICY IF EXISTS {table}_worker ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
    op.drop_table("projection_checkpoints")
    op.drop_index("ix_domain_inbox_progress", table_name="domain_inbox")
    op.drop_table("domain_inbox")
    op.drop_index("ix_event_deliveries_lease", table_name="event_deliveries")
    op.drop_index("ix_event_deliveries_claimable", table_name="event_deliveries")
    op.drop_index("uq_event_deliveries_consumer_event", table_name="event_deliveries")
    op.drop_table("event_deliveries")
    op.drop_index("ix_research_domain_events_position", table_name="research_domain_events")
    op.drop_index("ix_research_domain_events_order", table_name="research_domain_events")
    op.drop_table("research_domain_events")
