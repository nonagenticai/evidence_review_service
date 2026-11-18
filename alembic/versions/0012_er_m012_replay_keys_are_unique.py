"""Three replay keys were enforced by a SELECT, and a SELECT is not a constraint.

`flow.create_export`, `imports.import_payload` and `deduplication.create_run` all
implement "the same inputs are the same artefact, replay it" the same way: ask
whether a row with this key exists, and insert one if it does not. That is a
check-then-act, and this service runs at READ COMMITTED, where two transactions
take the SELECT before either commits, both see nothing, and both insert.

Verified directly against the database before writing this, as the owner:

    \\d import_batches      -> PRIMARY KEY only
    \\d inclusion_exports   -> PRIMARY KEY only
    \\d deduplication_runs  -> ix_dedup_runs_digest (review_id, input_digest), NOT unique

So nothing underneath any of the three checks enforced what the check was for.
Reproduced with two threads and two sessions
(`tests/integration/test_a_second_writer_cannot_mint_a_twin.py`), the winner's
transaction held open across the loser's whole call:

    exports        : 2 rows for one manifest_hash, both verified=True
    import batches : 2 rows for one (review_id, raw_payload_digest)
    dedup runs     : 2 rows for one (review_id, input_digest)

The consequences are not symmetric. `inclusion_exports` is append-only, so the
twin seal is PERMANENT and `DELETE` is refused by the trigger — which makes the
#53 "an unchanged review seals one export" fix a TOCTOU rather than a guarantee.
`import_batches` doubles the corpus, and every downstream count agrees with the
doubling because `identified_total` is derived from `import_rows`; worse, the
next import of those same bytes reaches `scalar_one_or_none()` on two rows,
raises `MultipleResultsFound`, and leaves the route emitting a bare
`500 text/plain` outside the problem+json contract every other error honours.
`deduplication_runs` yields two cluster sets over one corpus, either of which
`resolve_cluster` will accept.

The trigger is NOT a studio double-click: `ScreeningPanel.svelte` sets `busy`
synchronously before any await and disables the button, so that client
serialises. It is a worker retry — the exact case `imports.py`'s module docstring
claims safety for — two leads or two tabs, or a second pod.

## What this migration is, and what it is only half of

Three UNIQUE constraints on the keys the three replay checks already use:

* `uq_export_manifest_per_review`  — `inclusion_exports (review_id, manifest_hash)`
* `uq_import_batch_payload_digest` — `import_batches (review_id, raw_payload_digest)`
* `uq_dedup_run_input_digest`      — `deduplication_runs (review_id, input_digest)`

Constraints rather than bare `CREATE UNIQUE INDEX`, because the application half
of the fix spells `ON CONFLICT ON CONSTRAINT <name>` — the form
`flow.current_flow_revision` and `screening._maybe_open_conflict` already use —
and Postgres accepts `ON CONSTRAINT` only for a real constraint. A unique index
would force `index_elements`, i.e. a second spelling of the same idea in the same
codebase.

The index alone would NOT be a fix, and shipping it without the application half
would be a regression: the losing transaction would take an `IntegrityError`, and
a caller who asked for something that already exists would get a 500 or a
`409 state_conflict` where the documented answer is `replayed: true`. Both halves
land together.

## `ix_dedup_runs_digest` is dropped, not kept alongside

It is `(review_id, input_digest)` non-unique — exactly the columns of
`uq_dedup_run_input_digest`, whose backing index is a btree on the same leading
columns and serves every lookup the old one served. Keeping both would be a
second index to write on every dedup run for no read it can answer alone.

`ix_dedup_runs_review` (`review_id, created_at`) is untouched: it orders runs by
time and is not a prefix of anything here.

## What this would require if the tables held data

Production holds ZERO rows in all 25 domain tables — verified as a superuser with
RLS bypassed — so all three constraints are created over empty tables and cannot
fail. That is an argument for landing it NOW rather than an argument that the
question does not matter, because on a loaded database each of the three would
need, in order:

1. a duplicate pre-check, e.g.
   `SELECT review_id, manifest_hash FROM inclusion_exports
      GROUP BY 1, 2 HAVING count(*) > 1`,
   with a refusal that names the rows rather than an `ALTER TABLE` that fails
   with a constraint name and no context;
2. a de-duplication that keeps the OLDEST row of each group — the one every
   `replayed` answer already handed out, and for `inclusion_exports` the one a
   consumer may already have cited;
3. and a deletion that most of the three cannot simply perform. Measured, not
   assumed — `pg_trigger` on a migrated database:

   * `inclusion_exports` carries `trg_inclusion_exports_append_only`,
     `BEFORE DELETE OR UPDATE`, raising `restrict_violation` unconditionally.
     Removing a twin seal means DROPping or disabling that trigger inside the
     migration and restoring it after.
   * `import_batches` and `deduplication_runs` carry NO trigger of their own, so
     the batch or run row deletes cleanly — but the batch's `import_rows`
     children must go first (`ON DELETE RESTRICT`), and each accepted row points
     at a `review_records` row whose `review_record_versions` are governed by
     `trg_review_records_immutable` and
     `trg_review_record_versions_immutable`, both `BEFORE DELETE OR UPDATE`.
     Unwinding a duplicated import therefore also means suspending those two.

That is a destructive multi-table unwind under suspended integrity guards,
against evidence artefacts. It is exactly the migration nobody should have to
write, and it is the one this repository will owe the day a duplicate exists.

## Not CONCURRENTLY

`ALTER TABLE ... ADD CONSTRAINT UNIQUE` takes an ACCESS EXCLUSIVE lock and cannot
be built concurrently in one step (that needs `CREATE UNIQUE INDEX CONCURRENTLY`
outside a transaction, then `ADD CONSTRAINT ... USING INDEX`). The same argument
`0010` made applies and is measured, not assumed: the tables are empty, so the
lock is taken over nothing. If this ever has to be applied to a loaded database,
that is a decision to take then, with the row count in hand.

Revision ID: 0012_er_m012_replay_keys
Revises: 0011_er_m011_stages
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_er_m012_replay_keys"
down_revision = "0011_er_m011_stages"
branch_labels = None
depends_on = None

#: `(constraint name, table, columns)`. Named once and driven from here by BOTH
#: `upgrade` and `downgrade`, for the reason `0010` gives: `0009` shipped a
#: `downgrade` that dropped a constraint name no `upgrade` had ever created and
#: CI went red on #26 for exactly that. A shared table is the form that cannot
#: diverge. These names are also quoted verbatim in the three services'
#: `on_conflict_do_nothing(constraint=...)` calls — a rename here is a rename
#: there, and `tests/migration/test_survivable_vocabulary_agrees.py` is the
#: precedent for facts that live in two files.
REPLAY_KEYS: tuple[tuple[str, str, list[str]], ...] = (
    ("uq_export_manifest_per_review", "inclusion_exports", ["review_id", "manifest_hash"]),
    ("uq_import_batch_payload_digest", "import_batches", ["review_id", "raw_payload_digest"]),
    ("uq_dedup_run_input_digest", "deduplication_runs", ["review_id", "input_digest"]),
)

#: Superseded by `uq_dedup_run_input_digest`, which covers the same columns and
#: is unique. Restored by `downgrade`.
REDUNDANT_INDEX = ("ix_dedup_runs_digest", "deduplication_runs", ["review_id", "input_digest"])


def upgrade() -> None:
    for name, table, columns in REPLAY_KEYS:
        # LOUD ON DUPLICATES, rather than a bare `ALTER TABLE` failure.
        #
        # The tables are empty in every environment this runs against today, so
        # this raises nowhere — but "the ALTER will just fail" is not a plan: it
        # fails with a constraint name, a table name and no way to find the rows,
        # against evidence tables that cannot simply be cleaned up. See the module
        # docstring for what resolving it actually takes.
        #
        # ## The `NO FORCE ROW LEVEL SECURITY` around the count is load-bearing
        #
        # This check WAS vacuous, and it was caught the only way it could be: by
        # running the migration against a database that genuinely held duplicates
        # (the RED fixture of
        # `tests/integration/test_a_second_writer_cannot_mint_a_twin.py`, two
        # exports sharing one `manifest_hash`). The `DO` block reported nothing and
        # the `ALTER TABLE` failed one statement later with
        # `could not create unique index ... Duplicate keys exist.`
        #
        # The reason is `ER-M001`'s tenant pattern. Every one of these three
        # tables is `ENABLE ROW LEVEL SECURITY` **and** `FORCE ROW LEVEL SECURITY`
        # (measured: `relrowsecurity`/`relforcerowsecurity` are both `t`), and
        # FORCE applies the policy to the table OWNER too — which is the role
        # alembic connects as. The policy keys on `app.current_tenant`, a GUC no
        # migration sets, so `SELECT count(*) FROM inclusion_exports` returned
        # **0 while two rows existed**. A pre-check that reads through RLS is a
        # pre-check of nothing, and it fails in the safest-looking direction: it
        # says "no duplicates" on every database.
        #
        # `ALTER TABLE ... NO FORCE`/`FORCE` around the count is the narrow fix.
        # It is DDL, so it is transactional and rolls back with the rest of the
        # revision if anything below raises; it needs no new role and no
        # `BYPASSRLS`, which only a superuser can grant and which CI's runtime role
        # deliberately does not have; and it lasts one statement. `SET row_security
        # = off` is NOT an alternative — for a forced table it makes the query
        # ERROR rather than widening it.
        #
        # ## And the count is taken in PYTHON, not in a `DO $$ ... RAISE`
        #
        # Also measured rather than reasoned. A PL/pgSQL `RAISE EXCEPTION` aborts
        # the transaction, so every statement after it — including a `finally:`
        # that restores `FORCE ROW LEVEL SECURITY` — fails with
        # `InFailedSqlTransaction`, and THAT is the error the operator sees. The
        # carefully written message naming the table, the count and the remedy was
        # replaced by "current transaction is aborted", which is the one sentence
        # that tells them nothing.
        #
        # So the check reads through the bind and raises a Python exception. The
        # message survives, and the `FORCE` is restored on the success path only —
        # correctly, because the failure path rolls the whole revision back and
        # `ALTER TABLE ... NO FORCE` is transactional DDL, so the rollback restores
        # it. A `finally` here would be strictly worse than nothing: it can only
        # run in the state where it is guaranteed to fail.
        column_list = ", ".join(columns)
        bind = op.get_bind()
        bind.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))  # noqa: S608 - constant
        offenders = bind.execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {column_list} HAVING count(*) > 1) d"  # noqa: S608, E501 - constants from REPLAY_KEYS
            )
        ).scalar_one()
        if offenders:
            raise RuntimeError(
                f"{table} holds {offenders} duplicate group(s) on ({column_list}), so "
                f"{name} cannot be created. These rows were minted by the check-then-insert race "
                "this migration closes. Resolving them means deleting evidence rows — for "
                "inclusion_exports with trg_inclusion_exports_append_only suspended, and for "
                "import_batches with the batch's import_rows, review_records and "
                "review_record_versions unwound under their own immutability triggers. See the "
                "docstring of 0012_er_m012_replay_keys before doing any of it."
            )
        bind.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))  # noqa: S608 - constant
        op.create_unique_constraint(name, table, columns)

    op.drop_index(REDUNDANT_INDEX[0], table_name=REDUNDANT_INDEX[1])


def downgrade() -> None:
    op.create_index(REDUNDANT_INDEX[0], REDUNDANT_INDEX[1], REDUNDANT_INDEX[2])
    for name, table, _ in REPLAY_KEYS:
        op.drop_constraint(name, table, type_="unique")
