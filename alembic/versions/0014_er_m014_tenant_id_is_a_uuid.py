"""`tenant_id` is a `uuid` in the column type, not only in the contract.

Every one of the 25 tenant columns in this database was `text`. Measured on
worker4 before writing this: 25 of 25 `text`, and 0 rows in all 25 tables. So
there is nothing to remap, nothing to audit and nothing to back-fill — this is a
type declaration catching up with a fact every writer already obeyed, and it is
the last moment it can be made cheaply.

## What `text` was actually costing

`Actor.tenant_id` comes from a Keycloak `tenant_id` claim or, under
`ENABLE_AUTH=false`, from `X-Tenant-Id`. Both are UUIDs in every environment we
run. A `text` column does not say so, and the RLS predicate
`tenant_id = current_setting('app.current_tenant', true)` compares bytes, so the
database treated `'A1B2…'`, `'a1b2…'` and `'a1b2… '` as three different tenants.
A caller who picked the wrong casing did not get a refusal — they got an empty,
successful read of their own data, which is the failure direction that does not
page anybody. `uuid` collapses all three spellings into one value at the type
boundary, and `'not-a-tenant'` stops being storable at all.

## Why the policies are dropped and recreated rather than left alone

Postgres refuses `ALTER TABLE … ALTER COLUMN … TYPE` while a policy names the
column — `cannot alter type of a column used in a policy definition`. DROP first,
ALTER, CREATE is therefore not a style choice, it is the only order that works.

Two of the 25 are not the uniform shape and are written out individually:

* `operation_idempotency` keys on `resource_tenant_id`, not `tenant_id` (`0001`).
* `screening_decisions` has no `_tenant` policy at all. `0005` folded tenant
  isolation INTO `screening_decisions_blinded` deliberately — "multiple
  permissive policies are OR-ed, so a separate tenant policy would make a
  cross-tenant read of one's own subject id succeed" — and `0008` narrowed its
  third clause to the declared audience. That predicate is transcribed verbatim
  from `0008` with ONE character sequence added, `::uuid`, on the two tenant
  comparands. Nothing else in it moves. It is the ER-004.2 reviewer-blinding
  boundary and the tables are empty, so no row count anywhere could catch a
  transcription error in it.

## `::uuid` and not `NULLIF(…, '')::uuid`

`current_setting(name, true)` returns NULL for an unbound GUC, `NULL::uuid` is
NULL, and `tenant_id = NULL` is NULL — so "an unbound tenant sees nothing", which
is what every migration from `0009` onward relies on when it reads through FORCE
ROW LEVEL SECURITY, survives this change untouched. A `NULLIF` would exist to
absorb an empty-string GUC, and nothing writes one: `bind_actor` binds
`str(actor.tenant_id)`, which after `api/dependencies` validates the boundary is
always a canonical UUID string. Adding the branch would only hide the day
something starts writing a value this migration would otherwise refuse.

## FORCE is NOT re-asserted, on purpose

`relforcerowsecurity` is a property of the TABLE, not of the policy or the
column. It survives both `DROP POLICY` and `ALTER COLUMN … TYPE`. `0009` and
`0012` bracket work in `NO FORCE` / `FORCE` because they need to READ rows as the
owner during the migration; this migration reads no row, so reproducing that
dance here would only add a window in which the tables are unguarded.

## The trigger `0013` left behind

`review_stages_append_only` declared `bound_tenant text` and compared it with
`OLD.tenant_id`. There is no `text = uuid` operator, so leaving that function
alone would make EVERY delete on `review_stages` abort with
`operator does not exist: text = uuid` — the correction window `0013` opened
would be shut again by a type error, and it would surface as a 500 on a re-lock
rather than as anything this migration touches. Re-declared as `uuid`, so the
trigger and the policies now agree, character for character, on what "bound to
this row's tenant" means.

The assignment moved out of the `DECLARE` and below the `TG_OP` check. A
`DECLARE` initialiser runs on every invocation including the `UPDATE` path, and a
malformed GUC there would replace `0013`'s append-only message with
`invalid_text_representation`. Both refuse; only one of them explains why.

Revision ID: 0014_er_m014_tenant_uuid
Revises: 0013_er_m013_window
"""

from __future__ import annotations

from alembic import op

revision = "0014_er_m014_tenant_uuid"
down_revision = "0013_er_m013_window"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
ACTOR_GUC = "app.actor_subject_id"
ROLE_GUC = "app.actor_role"

#: The 24 tables whose policy is the uniform `<table>_tenant`, paired with the
#: column it names. Only `operation_idempotency` (`0001`) departs from
#: `tenant_id`, and it is carried in the same list rather than special-cased so
#: that "25 columns" is a property of one list somebody can count.
TENANT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("operation_idempotency", "resource_tenant_id"),
    # 0002
    ("reviews", "tenant_id"),
    ("review_protocol_versions", "tenant_id"),
    ("review_stages", "tenant_id"),
    ("review_state_history", "tenant_id"),
    # 0003
    ("import_batches", "tenant_id"),
    ("import_rows", "tenant_id"),
    ("review_records", "tenant_id"),
    ("review_record_versions", "tenant_id"),
    ("corpus_versions", "tenant_id"),
    ("corpus_version_members", "tenant_id"),
    # 0004
    ("deduplication_runs", "tenant_id"),
    ("duplicate_pair_scores", "tenant_id"),
    ("duplicate_clusters", "tenant_id"),
    ("duplicate_cluster_members", "tenant_id"),
    ("duplicate_resolutions", "tenant_id"),
    ("duplicate_survivorship_fields", "tenant_id"),
    # 0005
    ("screening_assignments", "tenant_id"),
    ("screening_conflicts", "tenant_id"),
    ("screening_adjudications", "tenant_id"),
    ("screening_unblinding_events", "tenant_id"),
    ("flow_revisions", "tenant_id"),
    ("flow_projections", "tenant_id"),
    ("inclusion_exports", "tenant_id"),
)

#: The 25th. Its policy is `screening_decisions_blinded`, not
#: `screening_decisions_tenant`, so it cannot go through `_retype`.
BLINDED_TABLE = "screening_decisions"

ALL_TABLES: tuple[str, ...] = tuple(t for t, _ in TENANT_COLUMNS) + (BLINDED_TABLE,)

#: `0013`'s wording, reused verbatim for the same reason `0013` reused `0011`'s:
#: it is the sentence that explains the refusal, and two spellings of it drift.
REASON = "a stage that ordered a recorded judgement cannot be re-ordered afterwards"


def _ownership_guard(tables: tuple[str, ...]) -> str:
    """Refuse before the first ALTER if this role cannot own one of these tables.

    `ALTER TABLE … TYPE` needs OWNERSHIP, not DML. A foreign-owned table is not
    discovered by the grants the migration role holds; it is discovered halfway
    through the ALTERs, with the policies of everything already converted
    dropped and recreated and the rest still `text`. One clean named abort
    before any DDL costs a deploy; the other shape cost 17 minutes of outage
    earlier in this sweep on a single-replica service with no old pod left
    serving.

    Written as a `DO` block rather than as Python over `op.get_bind()` so the
    whole migration stays expressible through `op.execute` — see `upgrade`.
    """
    listed = ", ".join(f"'{t}'" for t in tables)
    return f"""
        DO $$
        DECLARE
          foreign_tables text;
        BEGIN
          SELECT string_agg(format('%s (owned by %s)', c.relname, pg_get_userbyid(c.relowner)), ', '
                            ORDER BY c.relname)
            INTO foreign_tables
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema()
             AND c.relkind = 'r'
             AND c.relname IN ({listed})
             AND NOT pg_has_role(current_user, c.relowner, 'USAGE');

          IF foreign_tables IS NOT NULL THEN
            RAISE EXCEPTION
              'ER-M014 refuses to start: role % is not a member of the owning role of %',
              current_user, foreign_tables
              USING ERRCODE = 'insufficient_privilege',
                    HINT = 'ALTER COLUMN ... TYPE requires ownership, not INSERT/UPDATE. '
                           'Every one of these tables is owned by evidence_review. Fix the '
                           'ownership before deploying; do not GRANT around it.';
          END IF;
        END $$;
    """


def _retype(table: str, column: str, *, to_uuid: bool) -> str:
    """DROP the policy, change the type, recreate the policy. In that order.

    The ALTER is guarded on the column's CURRENT type rather than run
    unconditionally. `uuid::uuid` is a legal cast, so a second run would succeed
    either way — but it would succeed by rewriting the whole table and rebuilding
    every index on it, including `uq_reviews_project_external_key`. A no-op that
    rewrites the heap is not a no-op.

    The DROP/CREATE pair is unconditional and idempotent on its own:
    `DROP POLICY IF EXISTS` then `CREATE POLICY` lands the same text whichever
    state the database was in.
    """
    want = "uuid" if to_uuid else "text"
    comparand = f"current_setting('{TENANT_GUC}', true)" + ("::uuid" if to_uuid else "")
    return f"""
        DROP POLICY IF EXISTS {table}_tenant ON {table};

        DO $$
        BEGIN
          IF (SELECT a.atttypid
                FROM pg_attribute a
               WHERE a.attrelid = '{table}'::regclass
                 AND a.attname = '{column}') IS DISTINCT FROM '{want}'::regtype
          THEN
            EXECUTE 'ALTER TABLE {table} ALTER COLUMN {column} TYPE {want} USING {column}::{want}';
          END IF;
        END $$;

        CREATE POLICY {table}_tenant ON {table}
          USING ({column} = {comparand})
          WITH CHECK ({column} = {comparand});
    """


def _retype_blinded(*, to_uuid: bool) -> str:
    """`screening_decisions`, whose policy is the blinding boundary itself.

    The predicate below is `0008`'s, transcribed rather than rebuilt. The only
    difference between the two branches of this function is `::uuid` on the two
    tenant comparands: the `reviewer_subject_id`, `app.actor_role` and
    `disclosed_to_subject_ids` clauses stay `text` because the subject ids they
    compare ARE text — a Keycloak `sub` is a UUID today but
    `screening_unblinding_events.disclosed_to_subject_ids` is `text[]` and
    `ck_unblinding_actor_not_blank` is the only thing that constrains its shape.
    Widening those here would be a second, unrequested change to the one policy
    nothing in this database can detect a mistake in.
    """
    want = "uuid" if to_uuid else "text"
    tenant = f"current_setting('{TENANT_GUC}', true)" + ("::uuid" if to_uuid else "")
    return f"""
        DROP POLICY IF EXISTS screening_decisions_blinded ON screening_decisions;

        DO $$
        BEGIN
          IF (SELECT a.atttypid
                FROM pg_attribute a
               WHERE a.attrelid = 'screening_decisions'::regclass
                 AND a.attname = 'tenant_id') IS DISTINCT FROM '{want}'::regtype
          THEN
            EXECUTE 'ALTER TABLE screening_decisions ALTER COLUMN tenant_id TYPE {want} USING tenant_id::{want}';
          END IF;
        END $$;

        CREATE POLICY screening_decisions_blinded ON screening_decisions
          USING (
            tenant_id = {tenant}
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
                   AND current_setting('{ACTOR_GUC}', true) = ANY (u.disclosed_to_subject_ids)
              )
            )
          )
          WITH CHECK (
            tenant_id = {tenant}
            AND reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
          );
    """


def _append_only_trigger(tenant_type: str) -> str:
    """`0013`'s conditional trigger, with `bound_tenant` re-declared.

    Written out rather than imported from `0013` for the reason `0013` gives for
    writing out `0011`'s: a migration that reaches into another revision's module
    breaks the moment that revision is squashed.
    """
    cast_to = "::uuid" if tenant_type == "uuid" else ""
    return f"""
CREATE OR REPLACE FUNCTION review_stages_append_only() RETURNS trigger AS $fn$
DECLARE
  bound_tenant {tenant_type};
BEGIN
  IF TG_OP <> 'DELETE' THEN
    RAISE EXCEPTION 'review_stages is append-only: {REASON}'
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- Read AFTER the TG_OP check, not in the DECLARE: an unparseable GUC must not
  -- replace the message above with a cast error on the UPDATE path.
  bound_tenant := current_setting('{TENANT_GUC}', true){cast_to};

  -- NON-VACUITY. Every check below reads a FORCE ROW LEVEL SECURITY table keyed
  -- on app.current_tenant; unbound, they all answer "no work exists" and permit
  -- everything. Refuse rather than read nothing. See 0013's module docstring.
  IF bound_tenant IS NULL OR bound_tenant IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION
      'review_stages is append-only: app.current_tenant is not bound to this row''s tenant, '
      'so whether this stage has been used cannot be determined'
      USING ERRCODE = 'restrict_violation';
  END IF;

  IF EXISTS (SELECT 1 FROM screening_assignments a WHERE a.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM screening_decisions d WHERE d.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM screening_conflicts c WHERE c.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM inclusion_exports e WHERE e.review_id = OLD.review_id)
     OR EXISTS (
          SELECT 1 FROM review_state_history h
           WHERE h.review_id = OLD.review_id AND h.to_state = 'active'
        )
  THEN
    RAISE EXCEPTION
      'review_stages is append-only once its stage set has been used: {REASON}. '
      'This review has been activated, or holds an assignment, decision, conflict or sealed export.'
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN OLD;
END;
$fn$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # Every statement below goes through `op.execute` on a literal string, with
    # no `op.get_bind()` anywhere, so this revision is expressible under
    # `alembic upgrade --sql`. `0013` is not, and that is the direction this
    # repo should be moving rather than away from.
    op.execute(_ownership_guard(ALL_TABLES))
    for table, column in TENANT_COLUMNS:
        op.execute(_retype(table, column, to_uuid=True))
    op.execute(_retype_blinded(to_uuid=True))
    # `CREATE OR REPLACE FUNCTION` re-points the existing
    # `trg_review_stages_append_only` at the new body, so there is no window in
    # which the table is unguarded.
    op.execute(_append_only_trigger("uuid"))


def downgrade() -> None:
    op.execute(_ownership_guard(ALL_TABLES))
    op.execute(_append_only_trigger("text"))
    op.execute(_retype_blinded(to_uuid=False))
    for table, column in TENANT_COLUMNS:
        op.execute(_retype(table, column, to_uuid=False))
