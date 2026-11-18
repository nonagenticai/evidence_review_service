"""The ledger's four inputs were rewritable, and neither detector could see it.

`flow._collect` calls itself *"read the five immutable inputs"*. Four of the
tables it reads were not immutable in any sense the database enforced. Measured
on `0014`:

    SELECT tgname FROM pg_trigger
     WHERE tgrelid IN ('import_batches'::regclass, 'import_rows'::regclass,
                       'duplicate_clusters'::regclass,
                       'duplicate_cluster_members'::regclass)
       AND NOT tgisinternal;
    (0 rows)

    grantee=evidence_review_app on all four: SELECT, INSERT, UPDATE, DELETE

`imports.import_payload`'s own docstring already said so, in the paragraph added
while `0012` was being written: *"Append-only by construction, and NOT by
trigger. … So somebody with the database credential CAN delete these rows."* This
migration is that paragraph's other half.

## What the rewrite buys an attacker, and why nothing catches it

`identified_total` is derived from `import_rows` and the duplicate buckets from
`duplicate_clusters` / `duplicate_cluster_members`. Every one of those numbers
goes into `counts`, into `canonical_counts_hash`, into the export's SEALED
manifest and into the published PRISMA diagram. Rewriting one `import_rows.status`
moves `identified_total`; deleting a `duplicate_cluster_members` row moves
`duplicates_removed`; both leave a sealed export claiming a number the live
history no longer supports.

Neither existing check reports it.

* **`:verify` is blind by construction.** `verify_export` re-hashes the STORED
  payload and compares it against itself — its own docstring says so. It never
  reads `import_rows`, so a sealed export whose inputs were rewritten underneath
  it still answers `outcome: "verified"`.
* **`flow:rebuild` is one-shot and self-erasing.** It compares a freshly derived
  hash against the stored projection and then calls `compute_flow(persist=True)`,
  which writes the NEW hash as the newest projection. So the FIRST rebuild after
  a tamper reports `matches_previous_projection: false` — and destroys the only
  record that the two ever disagreed. The second rebuild, and every rebuild after
  it, reports `true`. A detector whose evidence is consumed by the act of
  detecting is not a control anybody can operate.

`flow:rebuild` is NOT fixed here. It is a cache-drift detector and it is correct
as one — a cache that has drifted SHOULD be corrected, and reporting the
correction is what it is for. The read-only counterpart lives in
`application/services/flow_audit.py`.

## `import_batches` is the one that could not be closed flatly

The other three take an unconditional guard: `0004._append_only`, the helper that
minted ten of the eleven guards this database already carries, copied verbatim
down to the message shape, with `0011`'s `DROP TRIGGER IF EXISTS` added because
these tables already exist.

They can be closed flatly because nothing in this service ever writes them twice
— verified against the tree, not assumed. `import_rows` and
`duplicate_cluster_members` are `session.add`-ed once and never touched again,
and `duplicate_clusters` likewise.

`import_batches` is different, and an unconditional trigger on it would have
taken the import path offline. `imports.import_payload` inserts the batch, builds
its rows, and then completes its tallies IN THE SAME TRANSACTION::

    batch.accepted_count = accepted
    batch.quarantined_count = quarantined
    batch.rejected_count = 0
    session.flush()

That is an `UPDATE import_batches`, on every single import. So the guard here is
conditional, in the shape `0002`'s `review_protocol_versions_locked_immutable`
established and `0013` reused: the row has two lives, and the trigger names the
moment it becomes evidence.

* `DELETE` — refused always, exactly like the other three.
* `UPDATE` — permitted only when **both** hold: nothing outside
  `accepted_count` / `rejected_count` / `quarantined_count` moves, and
  `OLD.created_at = transaction_timestamp()`.

## Why `transaction_timestamp()` and not the transaction id

The obvious spelling of "this row was inserted by the transaction now updating
it" is `xmin = pg_current_xact_id()::xid`. It is wrong here, measured rather than
reasoned about::

    BEGIN; INSERT …;               xmin = pg_current_xact_id()::xid  ->  t
    SAVEPOINT s1; INSERT …;        xmin = pg_current_xact_id()::xid  ->  f

A subtransaction gets its own xid while `pg_current_xact_id()` keeps returning
the top-level one, so any savepoint anywhere between the insert and the tally —
SQLAlchemy opens them, and nothing in this service promises never to — turns
every import into a 500. `created_at` defaults to `now()`, which is
`transaction_timestamp()`, which is stable across savepoints and identical for
every statement of one transaction. Equality against it is therefore true for
exactly the writes this window exists for.

The residual is a transaction that BEGINs at the same microsecond as the one that
inserted the row it is attacking, and it is worth naming rather than waving at:
such an attacker still cannot move `raw_payload_digest`, `source_kind`,
`review_id`, `tenant_id`, `submitted_by_subject_id` or `created_at`, and cannot
delete the row. The three counts are provenance reported by `GET /imports`; they
are not read by `flow._collect` and no number in `canonical_counts_hash` is
derived from them.

## The REVOKE half, and the one table that keeps `UPDATE`

`0011` explains why both halves are needed and neither substitutes for the other:
the trigger stops the OWNER too, and the REVOKE means the runtime role is refused
before the trigger is reached, so a service bug surfaces as a permission error
rather than as a business rule.

The pre-existing guarded tables were enumerated from `pg_trigger`, not from
memory. Fifteen tables carry a `BEFORE UPDATE OR DELETE` guard at `0014`. Of
those:

* `review_stages` already had `UPDATE` revoked by `0011`, and `0013` deliberately
  re-granted `DELETE` for the correction window. Untouched here.
* `review_protocol_versions` is **deliberately excluded**. Its trigger is
  conditional — it permits the write while `locked_at IS NULL` — and
  `reviews.lock_protocol_version` issues exactly that write as the runtime role::

      UPDATE review_protocol_versions SET locked_at = now(), … WHERE …

  Revoking `UPDATE` there would refuse every protocol lock in the service, which
  is a lot of blast radius for a table whose trigger already refuses every write
  that matters. Its `DELETE` is revoked: no route, service or script in this tree
  deletes a protocol version, so the privilege buys nothing and the trigger keeps
  the owner honest either way.

That leaves **13** tables taking the full `REVOKE UPDATE, DELETE`. The brief
this was written against said fourteen; fourteen is the count of guarded tables
still granting both, and it is right about that — it is `review_protocol_versions`
that must not be one of them.

`review_records` is not one of the thirteen either, despite carrying the
`0003` trigger unconditionally. `apply_survivorship`
(`application/services/imports.py`) issues::

    SELECT review_record_id FROM review_records WHERE review_record_id = :id FOR UPDATE

as the serialisation point for the two-cluster survivorship race documented on
that function — a row lock, never an UPDATE, so the immutability trigger does
not fire. **PostgreSQL requires the `UPDATE` privilege for `SELECT ... FOR
UPDATE`, whether or not any row is ever written.** `screening.py` hit the same
wall first, for `review_stages` (`UPDATE` revoked by `0011`), and chose an
advisory lock instead because that table has no `(review, stage, round)`-grain
row to lock in the first place; `review_records` does have the grain
`apply_survivorship` needs — the record itself — so a row lock is the right
primitive and revoking the privilege it depends on is the bug. `REVOKE UPDATE`
on `review_records` would not stop a rewrite — the trigger already does that,
unconditionally, and keeps doing it whether or not the grant exists — it would
only turn every `apply_survivorship` call into `InsufficientPrivilege`.

Nothing in this tree issues a `DELETE` on `review_records` — checked against
the tree, not assumed: every reference is an `INSERT`, a `SELECT`, or the
`FOR UPDATE` above, and the FKs from `duplicate_resolutions` /
`duplicate_survivorship_fields` / `screening_assignments` /
`screening_decisions` onto it are all `ondelete="RESTRICT"`, so nothing
downstream expects it to disappear either. `DELETE` is revoked. That makes
`review_records` the same shape as `import_batches` and
`review_protocol_versions`: the runtime role keeps one privilege for a
documented reason, the guarantee for everything else rests on the trigger
alone, and what is lost is the error MESSAGE for a service bug rather than the
refusal.

`import_batches` keeps `UPDATE` for the tally window above, which is the same
trade `0013` wrote down for `review_stages`'s `DELETE` and this migration now
writes down a second time for `review_records`: the runtime role keeps the
privilege, the guarantee rests on the trigger alone, and what is lost is the
error MESSAGE for a service bug rather than the refusal.

There is no data change and no backfill: this migration only adds guards.

Revision ID: 0015_er_m015_ledger_inputs
Revises: 0014_er_m014_tenant_uuid
"""

from __future__ import annotations

from alembic import op

revision = "0015_er_m015_ledger_inputs"
down_revision = "0014_er_m014_tenant_uuid"
branch_labels = None
depends_on = None

APP_ROLE = "evidence_review_app"

#: The three inputs that take the unconditional guard, and the clause that
#: explains each refusal. The message says what the row IS, not merely that the
#: write was refused — an operator reading it in a log has to be able to tell a
#: guard from an outage.
#:
#: Keyed and phrased exactly as `0004._append_only(table, reason)` renders them
#: (`'{table} is append-only: {reason}'`), so the ten guards this database
#: already carries and the three added here read as one sentence pattern rather
#: than two.
UNCONDITIONAL: dict[str, str] = {
    "import_rows": "identified_total is derived from these rows, and a sealed export cites it",
    "duplicate_clusters": ("a run is frozen, and a cluster that ordered a resolution cannot be re-drawn afterwards"),
    "duplicate_cluster_members": (
        "membership decides which records a merge removes, and duplicates_removed is derived from it"
    ),
}

#: Every column of `import_batches` EXCEPT the three tallies. Written out rather
#: than derived, because a column added later must fail CLOSED — an unlisted
#: column would silently become writable inside the tally window, and a
#: `TG_ARGV`-driven or `to_jsonb`-diffing version of this check would make that
#: the default.
BATCH_FROZEN_COLUMNS = (
    "import_batch_id",
    "review_id",
    "tenant_id",
    "source_kind",
    "source_ref",
    "raw_payload_digest",
    "submitted_by_subject_id",
    "created_at",
)

_FROZEN_PREDICATE = "\n     OR ".join(f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in BATCH_FROZEN_COLUMNS)

#: `import_batches` — DELETE always, UPDATE outside the tally window. See the
#: module docstring for why the window is timestamped rather than xid'd.
BATCHES_TRIGGER = f"""
CREATE OR REPLACE FUNCTION import_batches_append_only() RETURNS trigger AS $fn$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION
      'import_batches is append-only: a batch is the provenance of every record it carried'
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- The frozen columns first: this is the clause that makes the window narrow,
  -- and reporting it separately means a service bug that moves the wrong column
  -- says which column rather than "not in the window".
  IF {_FROZEN_PREDICATE}
  THEN
    RAISE EXCEPTION
      'import_batches is append-only: only accepted_count, rejected_count and quarantined_count '
      'may be written, and only by the transaction that inserted the batch'
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- `created_at` defaults to `now()` = `transaction_timestamp()`, so equality
  -- here means "inserted by this transaction" and stays true across savepoints,
  -- which `xmin = pg_current_xact_id()::xid` does NOT. Measured; see the
  -- docstring.
  IF OLD.created_at IS DISTINCT FROM transaction_timestamp() THEN
    RAISE EXCEPTION
      'import_batches is append-only once its transaction has committed: the tallies of batch % '
      'were completed by the transaction that created it and cannot be rewritten',
      OLD.import_batch_id
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_import_batches_append_only ON import_batches;
CREATE TRIGGER trg_import_batches_append_only
  BEFORE UPDATE OR DELETE ON import_batches
  FOR EACH ROW EXECUTE FUNCTION import_batches_append_only();
"""

#: The thirteen pre-existing tables whose guard trigger refuses EVERY update and
#: EVERY delete, enumerated from `pg_trigger` and `pg_get_functiondef` on a
#: database at `0014` rather than recalled. `review_stages` (handled by `0011` and
#: `0013`), `review_protocol_versions` (conditional; see the docstring) and
#: `review_records` (keeps `UPDATE` for `apply_survivorship`'s `FOR UPDATE`; see
#: the docstring) are absent on purpose.
FULLY_GUARDED = (
    "corpus_version_members",
    "corpus_versions",
    "duplicate_resolutions",
    "duplicate_survivorship_fields",
    "inclusion_exports",
    "review_record_versions",
    "review_state_history",
    "screening_adjudications",
    "screening_assignments",
    "screening_conflicts",
    "screening_decisions",
    "screening_unblinding_events",
)

#: `UPDATE, DELETE` revoked on these; they are the new guards' own tables.
NEWLY_GUARDED_FULL = tuple(UNCONDITIONAL)

#: `DELETE` only. `import_batches` keeps `UPDATE` for the tally window;
#: `review_protocol_versions` keeps it for `lock_protocol_version`;
#: `review_records` keeps it because `SELECT ... FOR UPDATE` in
#: `apply_survivorship` needs the privilege even though it never writes a row.
DELETE_ONLY = ("import_batches", "review_protocol_versions", "review_records")


def _role_guarded(statements: list[str], *, absent_notice: str) -> str:
    """Wrap grant changes in the existence check `0001`, `0002`, `0011` and `0013` all use.

    The app role is provisioned by CI and by the deployment, never by a
    migration, so an unguarded REVOKE fails the upgrade on any database that does
    not have it yet — including a fresh scratch database in
    `tests/migration/test_migration_round_trip.py`.
    """
    body = "\n            ".join(statements)
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            {body}
          ELSE
            RAISE NOTICE '{absent_notice}';
          END IF;
        END $$;
        """


def _append_only(table: str, reason: str) -> str:
    """`0004._append_only`, plus `0011`'s `DROP TRIGGER IF EXISTS`.

    Both idioms are current and they do not conflict: `0004` is the one every
    existing guard in this database was minted by and it is copied verbatim,
    including the message shape and the `_append_only` function name that
    `downgrade` depends on. The one addition is `0011`'s drop-before-create.
    `0004` could omit it because it creates the table in the same migration, so
    no trigger of that name can exist; this migration guards tables that have
    been live since `0003` and `0004`, where a re-run must be idempotent rather
    than a `42710`.
    """
    fn = f"{table}_append_only"
    return f"""
        CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is append-only: {reason}'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_{fn} ON {table};
        CREATE TRIGGER trg_{fn}
          BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION {fn}();
        """


def upgrade() -> None:
    for table, reason in UNCONDITIONAL.items():
        op.execute(_append_only(table, reason))
    op.execute(BATCHES_TRIGGER)

    # Named privileges rather than `REVOKE ALL` + re-GRANT, for the reason `0011`
    # gives: the runtime role must keep SELECT and INSERT on every one of these,
    # and a blanket revoke followed by a partial re-grant is one edit away from
    # taking the service offline.
    full = sorted(FULLY_GUARDED + NEWLY_GUARDED_FULL)
    op.execute(
        _role_guarded(
            [f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE};" for table in full]
            + [f"REVOKE DELETE ON {table} FROM {APP_ROLE};" for table in DELETE_ONLY],
            absent_notice=f"role {APP_ROLE} is absent; REVOKE skipped.",
        )
    )


def downgrade() -> None:
    full = sorted(FULLY_GUARDED + NEWLY_GUARDED_FULL)
    op.execute(
        _role_guarded(
            [f"GRANT UPDATE, DELETE ON {table} TO {APP_ROLE};" for table in full]
            + [f"GRANT DELETE ON {table} TO {APP_ROLE};" for table in DELETE_ONLY],
            absent_notice=f"role {APP_ROLE} is absent; GRANT skipped.",
        )
    )
    for table in (*UNCONDITIONAL, "import_batches"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
