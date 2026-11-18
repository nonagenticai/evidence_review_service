"""ER-M019 domain outbox: an event is committed with the state it describes.

Revision ID: 0019_er_m019
Revises: 0018_er_m018
Create Date: 2026-09-02

Foundation `FND-003`, the outbox half. Spec calls this group `ER-M001
foundation`; that slug was consumed by `0001`, which built the review aggregate
without it — see `0017`'s docstring for the mapping convention.

## What was actually missing, measured

`09-…:§3.7` names twenty-one events this service produces. On `origin/main` at
`915ae08` — six work packages in, `ER-001` through `ER-006`, live in production
since 2026-08 — **none of them had ever been written anywhere**. There was no
outbox table, no event row, no writer. `src/application/services/evidence.py`
says so in prose: "Writing an event means first creating the outbox (`FND-003`)."

This migration creates it.

## Why an outbox and not a publish call

`13-…:§5.3` gives one command exactly one transaction, and step 4 of that
transaction is "create its outbox event". The whole value is negative: after
this, a committed decision and the event announcing it **cannot disagree**,
because there is no ordering of failures that produces one without the other. A
service that instead published after commit has a window where the state moved
and nobody was told, and a service that published before commit has a window
where it announced something that never happened.

## ⛔ Nothing relays these rows

There is no relay, no delivery record and no consumer. `13-…:§8.1` is a
six-step path and this is step 1 of six; steps 2-6 are `FND-007`.
`research_output_service` is in the identical state and says so in its README.

That is a real limit and it is stated here rather than left for a reader to
discover: **committing to this table is not emitting**. `13-…:§8.3` — "No
service emits a listed document 09 event until its schema, example, consumer
matrix and compatibility test are merged" — is satisfied for the ENVELOPE
(`urn:datacompany:research-event-envelope:1`, released in `research_contracts`
0.5.0) and is NOT yet satisfied per event type. Nothing leaves this service, so
nothing is emitted, so the gate is not crossed. The day a relay exists, the
per-event payload schemas have to be released first.

## ⚠️ This table KEEPS row-level security, which `13-…:§5.2` does not require

The document says operational tables "are payload-minimal and excluded from
tenant RLS **so a cross-tenant worker/relay can discover work**", and
`research_output_service`'s own `domain_outbox` follows that literally, with no
policy at all. This migration deliberately does not, and the reason is measured
rather than stylistic.

`tests/migration/test_migration_round_trip.py` asserts `forced_rls == tables`
over the WHOLE schema, and `tests/integration/test_the_rls_backstop_fires.py`
exists to prove the suite cannot pass against policies that are not running.
Excluding this table would be the first hole in an invariant this service tests
for on every run — and it would be opened for a consumer that **does not
exist**: there is no relay, no relay role and no cross-tenant reader. A standing
cross-tenant exposure with no beneficiary is not a trade, it is a cost.

The exclusion is also not what §5.2 is actually after. What a relay needs is
cross-tenant *discovery*, and Postgres grants that additively — a second policy
`TO evidence_review_relay USING (true)`, or `BYPASSRLS` on that role — against a
table that still refuses every other principal. `FND-007` adds one policy; it
does not have to take one away. Doing it in that order means the estate never
has a window where the queue is world-readable and nothing is draining it.

Three things pay for the choice, and one is unchanged either way:

* `tenant_id` is `NOT NULL` on every row regardless, because `13-…:§5.1`
  requires a relay to **derive** the tenant from the row and then start a
  tenant-bound domain transaction. A relay that cannot tell whose event it is
  holding cannot do that.
* `ck_domain_outbox_payload_is_minimal` makes the payload rule a **database**
  fact rather than a comment. `er_payload_is_minimal` walks the JSON recursively
  and refuses any string over 512 characters at any depth, so an abstract, a
  full text block or an extracted narrative value cannot be stored here even by
  a caller that wants to. `13-…:§8.2` and `14-…` both forbid carrying that
  detail. This is defence in depth, not a substitute for the policy — it is what
  bounds the damage on the day the relay policy IS added.
* the app role gets `SELECT, INSERT` and the append-only trigger takes `UPDATE`
  and `DELETE` away.

## The registered-event CHECK is a closed set on purpose

`ck_domain_outbox_registered_event` pins `event_type` to the twenty-one names in
`09-…:§3.7`. `13-…:§8.3` calls for a registry; a registry that a caller can add
to at runtime by passing a new string is not one. A twenty-second event is a
migration, which is the visible, reviewable act it should be. The names are also
shape-checked and carry the estate's regulatory denylist, respelled from
`canonical_type_token` because real event names are dotted and version-suffixed
and that primitive refuses them outright — the same hand-carry the released
envelope schema documents and gates.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019_er_m019_domain_outbox"
down_revision = "0018_er_m018_accepted_evidence"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

#: The envelope every row conforms to. Released in ``research_contracts`` 0.5.0.
#: Pinned as a CHECK rather than left to the application: a row whose declared
#: grammar is a document nobody released is the exact defect that release exists
#: to have repaired, and it must not be reachable from here either.
ENVELOPE_SCHEMA_REF = "urn:datacompany:research-event-envelope:1"

#: This service's bounded context, as the envelope's ``producer``.
PRODUCER = "evidence-review"

#: The maximum length of any string anywhere in a payload. Mirrors
#: ``$defs/minimal_fact`` in the released envelope. Short enough that an abstract
#: cannot fit, long enough that no UUID, URN, digest, reason code or stage name
#: is refused.
MINIMAL_FACT_MAX = 512

#: Every event ``09-…:§3.7`` names for this service, in the document's order.
#: Mirrored by ``src/domain/policies/event_catalog.py`` and by the ORM, the way
#: ``0018`` keeps ``ACCEPTANCE_SUBJECTS``, so the CHECK and the application
#: cannot drift into two different closed sets.
#:
#: Four of these have no operation that could raise them yet and are listed
#: anyway — the catalogue module records which four and why. A registry that
#: named only what is already built could not tell a reader that the other four
#: are owed.
REGISTERED_EVENTS: tuple[str, ...] = (
    "review.created.v1",
    "review.activated.v1",
    "review.paused.v1",
    "review.resumed.v1",
    "review.reopened.v1",
    "review.archived.v1",
    "review.import.completed.v1",
    "review.duplicate_cluster.proposed.v1",
    "review.duplicate_cluster.resolved.v1",
    "review.decision.recorded.v1",
    "review.conflict.opened.v1",
    "review.conflict.resolved.v1",
    "review.research_object.approved.v1",
    "review.proposal.decided.v1",
    "review.observation.accepted.v1",
    "review.observation.superseded.v1",
    "review.assertion.recorded.v1",
    "review.appraisal.recorded.v1",
    "review.evidence_snapshot.created.v1",
    "review.stage.completed.v1",
    "review.closed.v1",
)

#: Read by ``tests/conftest.py`` — see ``0017.APPEND_ONLY_TABLES``.
APPEND_ONLY_TABLES: tuple[str, ...] = ("domain_outbox",)

_EVENT_NAME_SHAPE = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$"
_REGULATORY_DENYLIST = "regulatory|legal_|normative|consolidation"

_PAYLOAD_GUARD = f"""
    CREATE OR REPLACE FUNCTION er_payload_is_minimal(p jsonb) RETURNS boolean AS $$
    DECLARE
      child jsonb;
    BEGIN
      IF p IS NULL THEN
        RETURN true;
      END IF;
      CASE jsonb_typeof(p)
        WHEN 'string' THEN
          RETURN length(p #>> '{{}}') <= {MINIMAL_FACT_MAX};
        WHEN 'array' THEN
          FOR child IN SELECT jsonb_array_elements(p) LOOP
            IF NOT er_payload_is_minimal(child) THEN
              RETURN false;
            END IF;
          END LOOP;
          RETURN true;
        WHEN 'object' THEN
          FOR child IN SELECT value FROM jsonb_each(p) LOOP
            IF NOT er_payload_is_minimal(child) THEN
              RETURN false;
            END IF;
          END LOOP;
          RETURN true;
        ELSE
          RETURN true;
      END CASE;
    END;
    $$ LANGUAGE plpgsql IMMUTABLE;
"""


def _tenant_rls(table: str) -> str:
    """The same policy every other table in this schema carries.

    Verbatim from `0018` rather than shared: the migrations keep their own copy
    so that reading one tells you what it did, and an edit to a later policy
    cannot silently change an earlier migration's meaning on replay.
    """
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = {_TENANT_COMPARAND})
          WITH CHECK (tenant_id = {_TENANT_COMPARAND});
    """


def _grant(table: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT SELECT, INSERT ON {table} TO evidence_review_app;
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

        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            REVOKE UPDATE, DELETE ON {table} FROM evidence_review_app;
          END IF;
        END
        $$;
    """


def upgrade() -> None:
    op.execute(_PAYLOAD_GUARD)

    registered = ", ".join(f"'{name}'" for name in REGISTERED_EVENTS)

    op.create_table(
        "domain_outbox",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("producer", sa.Text(), nullable=False),
        sa.Column("resource_scope", sa.Text(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"event_type IN ({registered})",
            name="ck_domain_outbox_registered_event",
        ),
        sa.CheckConstraint(
            f"event_type ~ '{_EVENT_NAME_SHAPE}'",
            name="ck_domain_outbox_event_name_shape",
        ),
        sa.CheckConstraint(
            f"event_type !~ '{_REGULATORY_DENYLIST}'",
            name="ck_domain_outbox_event_name_not_regulatory",
        ),
        sa.CheckConstraint(
            f"schema_ref = '{ENVELOPE_SCHEMA_REF}'",
            name="ck_domain_outbox_envelope_pinned",
        ),
        sa.CheckConstraint(f"producer = '{PRODUCER}'", name="ck_domain_outbox_producer"),
        sa.CheckConstraint("resource_scope IN ('global', 'tenant')", name="ck_domain_outbox_scope"),
        sa.CheckConstraint("aggregate_version >= 1", name="ck_domain_outbox_aggregate_version"),
        sa.CheckConstraint("aggregate_type ~ '^[a-z][a-z0-9_]*$'", name="ck_domain_outbox_aggregate_type"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND er_payload_is_minimal(payload)",
            name="ck_domain_outbox_payload_is_minimal",
        ),
        sa.CheckConstraint(
            f"content_hash IS NULL OR content_hash ~ '{HASH_SHAPE}'",
            name="ck_domain_outbox_content_hash_shape",
        ),
    )

    # The relay's read order, once there is a relay. `occurred_at` alone is what
    # `research_output_service` indexes; the aggregate key is added here because
    # `13-…:§8.2` guarantees ordering per (scope, tenant, aggregate_type,
    # aggregate_id) and a consumer that has to DEFER a future version reads
    # exactly that key to find the one it is missing.
    op.create_index("ix_domain_outbox_occurred_at", "domain_outbox", ["occurred_at"])
    op.create_index(
        "ix_domain_outbox_aggregate",
        "domain_outbox",
        ["resource_scope", "tenant_id", "aggregate_type", "aggregate_id", "aggregate_version"],
    )

    op.execute(_tenant_rls("domain_outbox"))
    op.execute(_grant("domain_outbox"))
    op.execute(
        _append_only(
            "domain_outbox",
            "an event that can be edited after it was committed beside the state it describes "
            "reopens the exact gap the outbox closes",
        )
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS domain_outbox_tenant ON domain_outbox")
    op.execute("DROP TRIGGER IF EXISTS trg_domain_outbox_append_only ON domain_outbox")
    op.execute("DROP FUNCTION IF EXISTS domain_outbox_append_only()")
    op.drop_index("ix_domain_outbox_aggregate", table_name="domain_outbox")
    op.drop_index("ix_domain_outbox_occurred_at", table_name="domain_outbox")
    op.drop_table("domain_outbox")
    op.execute("DROP FUNCTION IF EXISTS er_payload_is_minimal(jsonb)")
