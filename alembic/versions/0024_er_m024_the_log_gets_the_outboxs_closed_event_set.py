"""ER-M024: the durable log gets the outbox's closed event-name CHECK.

Revision ID: 0024_er_m024_log_catalogue
Revises: 0023_er_m023_worker_policies
Create Date: 2026-09-04

## The gap this closes

`0019` gave `domain_outbox` a closed catalogue: `ck_domain_outbox_registered_event`
pins `event_type` to the twenty-one names `09-…:§3.7` lists, mirrored (not
derived — see `0019`'s own docstring on why) by
`src/domain/policies/event_catalog.REGISTERED_EVENTS` and held equal to both by
`tests/unit/test_the_event_catalogue_is_the_registry.py`.

`0022` built `research_domain_events` — the durable LOG a relay copies outbox
rows into — right beside it, with the same shape checks
(`ck_rde_event_name_shape`, `ck_rde_event_name_not_regulatory`) and the same
payload guard, but never the closed-set CHECK. `relay.py`'s `INSERT ... SELECT`
is the only writer today and it copies `event_type` verbatim from a row the
outbox's own CHECK already validated, so the gap has produced nothing bad yet —
but the invariant that matters ("this log holds only catalogued events") lived
entirely in that ONE copy path, in Python-adjacent SQL, and nowhere in the
table itself. Any future writer — a backfill script, a hand-run `INSERT`, a
second producer added later — could put `review.not_catalogued.v1` straight
into the log, and the table would accept it. Measured live before this
migration: it does.

## Why the closed list is copied a THIRD time rather than read from the outbox

`ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)` cannot reference another
table, so `research_domain_events` cannot delegate to
`ck_domain_outbox_registered_event` by any FK-like mechanism — a `CHECK` is
evaluated per-row against constants and functions, not against a sibling
table's rows. Postgres offers no "this CHECK equals that CHECK" primitive
either. So the twenty-one names are transcribed again here, the same posture
`0019` already took relative to `event_catalog.py`: independent spellings held
equal by a test are what catch an edit to one of them, and a single shared
constant would make every copy agree by construction — including when all of
them are wrong. `REGISTERED_EVENTS` below is byte-identical to `0019`'s; a
follow-up test can assert that the way
`test_the_event_catalogue_is_the_registry.py` already asserts `0019` against
`event_catalog.py`.

## Why this is its own migration and not folded into `0022`

`0022` is applied in production (`alembic_version` already passed it, same
argument `0023`'s docstring makes about `0022` itself). Editing an applied
migration changes what a fresh database gets while leaving production
untouched, and the divergence is invisible because both would report
`alembic_version = 0022`.

## Pre-existing rows the new CHECK would refuse

It REFUSES to run, naming them. It does not delete them.

An earlier draft did delete them — disabling `trg_research_domain_events_append_only`
and `trg_event_deliveries_no_delete`, deleting, re-enabling, logging the counts.
That is wrong, and the reasoning is kept on `_refuse_if_uncatalogued` because the
draft looked careful: the append-only trigger is a control, and switching a control
off to do the thing it forbids is routing around it rather than satisfying it.
Erasure is `IMP-DEC-005`'s open decision, not a migration's. And a row with an
uncatalogued `event_type` means a writer exists that this service does not know
about — `relay.py`, the only writer today, copies `event_type` verbatim from
`domain_outbox`, whose own closed CHECK (`ck_domain_outbox_registered_event`,
`0019`) already refuses anything off this list. That is a finding. Deleting it
destroys the only evidence of the finding and lets the deploy proceed.

Production is unaffected either way: `research_domain_events` holds zero rows
there, measured on worker4. The single row that provoked the draft was inserted by
an adversarial audit into a throwaway container.

"""

from __future__ import annotations

from alembic import op

revision = "0024_er_m024_log_catalogue"
down_revision = "0023_er_m023_worker_policies"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_rde_registered_event"
TABLE = "research_domain_events"

#: Byte-identical to `REGISTERED_EVENTS` in `0019` and in
#: `src/domain/policies/event_catalog.py` — see the module docstring on why a
#: CHECK cannot delegate to a sibling table's CHECK and this has to be its own
#: transcription rather than a shared constant.
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


def _add_check_idempotent(table: str, name: str, condition: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = '{name}' AND conrelid = '{table}'::regclass
          ) THEN
            ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition});
          END IF;
        END
        $$;
    """


def _refuse_if_uncatalogued(table: str, registered_list_sql: str) -> str:
    """REFUSE to migrate while a row the new CHECK would reject exists.

    ⚠️ An earlier draft of this migration DELETED those rows. It disabled
    `trg_{table}_append_only` and `trg_event_deliveries_no_delete`, deleted, and
    re-enabled them, logging the counts. That was wrong on three counts and the
    reasoning is worth keeping, because the draft looked careful.

    First, the append-only trigger is a CONTROL, and a migration that switches a
    control off to do the thing the control forbids has not satisfied it — it has
    routed around it. `ER-M022` installed those triggers precisely so that no code
    path could delete a committed domain event; a migration is a code path.

    Second, erasure is not this migration's decision to make. `IMP-DEC-005` owns
    lawful erasure versus immutable provenance and is still open. A row here is a
    domain event somebody committed in the same transaction as the state it
    describes, and destroying it destroys the evidence of whatever wrote it.

    Third, deleting is the WORST response to the situation it is meant to handle.
    A row with an uncatalogued `event_type` means a writer exists that nobody knows
    about — the only in-tree writer, `relay.py`, cannot produce one. That is a
    finding. Deleting it removes the only evidence of the finding and lets the
    deploy proceed as if nothing happened.

    So this refuses instead, naming the offending types and counts. Production is
    unaffected either way: `research_domain_events` holds zero rows there, measured
    on worker4. The one row that provoked the draft was inserted by an audit into a
    throwaway container, which is exactly the kind of provenance a delete would
    have erased before anyone could establish it.
    """
    return f"""
        DO $$
        DECLARE
          offenders text;
          offending_rows integer;
        BEGIN
          SELECT string_agg(DISTINCT event_type, ', '), count(*)
            INTO offenders, offending_rows
            FROM {table}
           WHERE event_type NOT IN ({registered_list_sql});

          IF offending_rows > 0 THEN
            RAISE EXCEPTION
              '0024 refuses to run: {table} holds % row(s) whose event_type is outside the '
              'catalogue (%). relay.py cannot produce these, so a writer exists that this '
              'service does not know about -- that is a finding, not a cleanup. Deleting them '
              'would need the append-only trigger disabled, and erasure is IMP-DEC-005''s '
              'decision, not a migration''s. Investigate the writer, then decide.',
              offending_rows, offenders;
          END IF;
        END
        $$;
    """


def upgrade() -> None:
    registered = ", ".join(f"'{name}'" for name in REGISTERED_EVENTS)
    op.execute(_refuse_if_uncatalogued(TABLE, registered))
    op.execute(_add_check_idempotent(TABLE, CONSTRAINT_NAME, f"event_type IN ({registered})"))


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}")
