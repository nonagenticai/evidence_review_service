"""ER-M017 research objects: identity, immutable versions, and the reports behind them.

Revision ID: 0017_er_m017
Revises: 0016_er_m016
Create Date: 2026-09-02

Work package `ER-006` (`ER-006.1`), release slice `8810-E`, wave `W3`.

## The numbering, before anything else

The specification calls this migration group `ER-M007`. **That slug is already
taken in this repository** — `0007_er_m007_survivable_fields` — as are `ER-M008`
(`0008`, unblinding audience) and `ER-M009` (`0009`, assignment names its
corpus). The spec's numbering and this tree's numbering diverged long before
`ER-006`, and renaming shipped migrations to close the gap would be a far worse
trade than recording the mapping:

    spec ER-M007 -> 0017_er_m017 (this file)
    spec ER-M008 -> 0018_er_m018
    spec ER-M009 -> 0019_er_m019

## What this adds, and why it is not just another table

Until now `8810` could say *which records are in*. It could not say *what study
a record is a report of*. Those are different things, and the difference is the
whole reason `ResearchObject` exists in `16-…:§13.1`: several reports
(a trial registration, a primary paper, two secondary analyses) are one study,
and every count, extraction and appraisal downstream is per *study*, not per
*report*. `flow_derivation` already says so in its own docstring — the PRISMA
"studies included in review" line is the one number it refuses to derive,
because nothing in the database grouped reports into studies. This is that
grouping.

## Identity is separate from version, and neither is a status column

`16-…:§13.1`: *"Review, ReviewRecord, ResearchObject, ExtractionDataset,
Observation, ResearchAssertion, and AppraisalRecord each preserve stable identity
separately from immutable semantic versions when correction or revision is
possible. Screening, acceptance, appraisal state, and lifecycle are append-only
decisions or bitemporal assertions over exact versions; they are not mutable
columns on those versions."*

So `research_objects` holds identity and nothing that can change; the semantic
content lives in `research_object_versions`, which is append-only and
content-hashed; and acceptance is not here at all — it is `evidence_acceptances`
in `0018`. There is deliberately no `status`, no `is_current`, no `accepted`
boolean anywhere in this file. `0005` already paid for that lesson: *"An
`is_current` boolean would be a second answer to the same question, and the two
disagree the first time a correction lands concurrently with a read."*

## Composite same-tenant foreign keys — new to this repository

`16-…:§16.2` requires foreign keys that cannot cross a tenant. Measured before
writing: **this tree had none** — every existing FK is single-column, and tenant
agreement between parent and child was a property nothing enforced. RLS makes a
cross-tenant row unreadable, but unreadable is not unwritable: a writer holding
one tenant's GUC could name another tenant's parent id and the FK would accept
it, because the FK does not know about tenants and the parent lookup runs as the
owner.

Adding one needs a `UNIQUE (pk, tenant_id)` on the parent for the FK to target,
which is why `research_objects` carries a unique constraint that looks redundant
against its primary key. It is not redundant; it is the anchor.

## What the database can honestly check about inclusion, and what it cannot

A research object should only be built from a record the review actually
included. **That check is not in this file, and the omission is deliberate.**

"Included" is a *derived* fact. `flow_derivation.derive()` computes it from the
immutable decision history — supersession chains, agreement, adjudication, stage
order — and `flow.py`'s docstring is explicit that the ledger is *"derived from
immutable history, never stored as truth"*. There is no `included` column to
join to. The sealed `inclusion_exports` row stores counts and hashes, not a
member list, so it cannot be joined either.

A trigger could re-implement `derive()` in PL/pgSQL. It should not: there would
then be two implementations of the single most correctness-critical function in
this service, in two languages, drifting apart silently, and the SQL one would
be the one nobody tests against generated histories.

So the split is:

* **Here (the database):** the record exists, belongs to the *same review and
  the same tenant*, and is a member of the corpus version this object pins.
  Those are facts a constraint can state without deriving anything.
* **The service layer:** refuses to create a research object for a record whose
  derived bucket is not `included`, calling the one authority.

`research_object_records` therefore pins `corpus_version_id`, so the claim
"this report was in the corpus" is checkable forever even after later imports
move the corpus on.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_er_m017_research_objects"
down_revision = "0016_er_m016_adjudicator_name"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"

#: `0014` made every tenant column a real `uuid`, so the RLS comparand casts.
#: Transcribed from `0014`'s post-state, not from `0005`'s pre-state.
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"

#: Every table this migration makes append-only, by trigger AND by REVOKE.
#:
#: Exported as a module constant because `tests/conftest.py` READS it. That
#: harness issues a blanket `GRANT ... ON ALL TABLES` to the runtime role after
#: migrating, which restores whatever any migration revoked — so a new REVOKE
#: silently loses its privilege half in every test environment unless the list is
#: re-asserted there. `0015` established the pattern and the reason: *"IMPORTED,
#: not retyped: the migration is the authority, and a second hand-maintained copy
#: is a copy that drifts silently in the direction of granting too much."*
#:
#: CI caught exactly that on this package's first run: all 11 new tables were
#: refused with `23001` (the trigger) instead of `42501` (the grant), which is
#: half the guard missing — and the half still working is the half that was never
#: the point of the REVOKE.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "research_objects",
    "research_object_versions",
    "research_object_records",
)

#: The shape `16-…:§3.5` fixes for every content hash in the model.
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"


def _tenant_rls(table: str) -> str:
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
    """Verbatim from `0005._append_only`, plus `0015`'s REVOKE half.

    `0015` established that the trigger alone is not the whole guard: a trigger
    is a runtime refusal, while the absent grant means the app role never had
    the privilege in the first place. Both, for the same reason `0015` gives.
    """
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


#: `16-…:§5.3` — *"Every lock, decision, job, analysis, model run, and
#: publication MUST pin the exact ProtocolVersion."* A `NOT NULL uuid` column is
#: not a pin; it is a promise nobody checked. This function is created once here
#: and reused by `0018` and `0019` — one implementation, so a later change to
#: what "validly pinned" means cannot apply to two of the three tables.
#:
#: `SECURITY DEFINER` for the same reason `screening_adjudications_separation_of_duty`
#: is: the check must not depend on what the writer can see through RLS.
_PROTOCOL_PIN_FN = """
CREATE OR REPLACE FUNCTION er_pins_locked_protocol() RETURNS trigger AS $$
DECLARE
  pinned RECORD;
BEGIN
  SELECT p.review_id, p.tenant_id, p.locked_at
    INTO pinned
    FROM review_protocol_versions p
   WHERE p.protocol_version_id = NEW.protocol_version_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'protocol_version_id % does not exist', NEW.protocol_version_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;

  IF pinned.review_id <> NEW.review_id OR pinned.tenant_id <> NEW.tenant_id THEN
    RAISE EXCEPTION
      'protocol_version_id % belongs to a different review or tenant',
      NEW.protocol_version_id
      USING ERRCODE = 'restrict_violation';
  END IF;

  IF pinned.locked_at IS NULL THEN
    RAISE EXCEPTION
      'protocol_version_id % is not locked; evidence cannot be judged against a protocol that can still change',
      NEW.protocol_version_id
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
"""


def _pin_trigger(table: str) -> str:
    return f"""
        CREATE TRIGGER trg_{table}_pins_locked_protocol
          BEFORE INSERT ON {table}
          FOR EACH ROW EXECUTE FUNCTION er_pins_locked_protocol();
    """


def upgrade() -> None:
    op.execute(_PROTOCOL_PIN_FN)

    # ---- identity: the study, not the report ------------------------------
    op.create_table(
        "research_objects",
        sa.Column("research_object_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The reviewer-facing handle for this study inside this review. Stable
        # for the life of the object; a correction to what the study IS is a new
        # version, and a decision that two objects are the same study is a
        # supersession, never a rename.
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("review_id", "object_key", name="uq_research_object_key"),
        # The anchor for composite same-tenant FKs from every child. Looks
        # redundant against the primary key; it is what lets a child FK carry
        # `tenant_id` and therefore what makes cross-tenant parenting
        # unrepresentable rather than merely unreadable.
        sa.UniqueConstraint("research_object_id", "tenant_id", name="uq_research_object_tenant"),
        sa.CheckConstraint("btrim(object_key) <> ''", name="ck_research_object_key_not_blank"),
        sa.CheckConstraint("btrim(created_by_subject_id) <> ''", name="ck_research_object_author"),
    )
    op.execute(_tenant_rls("research_objects"))
    op.execute(_grant("research_objects"))
    op.execute(
        _append_only(
            "research_objects",
            "identity never changes; revise the object with a new version and merge with a supersession",
        )
    )

    # ---- immutable semantic versions --------------------------------------
    op.create_table(
        "research_object_versions",
        sa.Column("research_object_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Which released schema this version's `content` validates against, and
        # the RFC 8785 hash of that content. Recorded per version, exactly as
        # `review_protocol_versions` does it and for the same reason: a version
        # authored under one schema stays interpretable after the schema moves.
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        # Why this version exists. `16-…:§13.1` requires correction and material
        # change to be distinguishable (`DM-AT-04`): a typo fix and "this is a
        # different study than we thought" must not look identical in history.
        sa.Column("revision_kind", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        # `0016` established that a subject id alone is unreadable to a human
        # months later. Captured at write time, never joined for.
        sa.Column("created_by_display_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_object_id", "tenant_id"],
            ["research_objects.research_object_id", "research_objects.tenant_id"],
            ondelete="RESTRICT",
            name="fk_research_object_version_same_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["research_object_versions.research_object_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("research_object_id", "version_number", name="uq_research_object_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_research_object_version_positive"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_research_object_version_hash_shape"),
        sa.CheckConstraint(
            "revision_kind IN ('initial', 'correction', 'material_change')",
            name="ck_research_object_revision_kind",
        ),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_research_object_version_rationale"),
        sa.CheckConstraint(
            "(version_number = 1 AND supersedes_version_id IS NULL AND revision_kind = 'initial') "
            "OR (version_number > 1 AND supersedes_version_id IS NOT NULL AND revision_kind <> 'initial')",
            name="ck_research_object_version_chain",
        ),
    )
    op.execute(_tenant_rls("research_object_versions"))
    op.execute(_grant("research_object_versions"))
    op.execute(
        _append_only(
            "research_object_versions",
            "a version is immutable; record a new one that supersedes it",
        )
    )
    op.execute(_pin_trigger("research_object_versions"))
    op.execute(
        """
        CREATE UNIQUE INDEX uq_research_object_version_supersession_linear
          ON research_object_versions (supersedes_version_id)
          WHERE supersedes_version_id IS NOT NULL;
        """
    )

    # `16-…:§16.2` — *"DB constraints reject cross-review and cross-tenant
    # supersession."* The self-FK above cannot do this: it proves the superseded
    # row exists, not that it is the same object in the same review of the same
    # tenant. Without this, version 2 of one tenant's study could cite version 1
    # of another's as its predecessor and the history would read as continuous.
    #
    # `SECURITY DEFINER`: the predecessor may be invisible to the writer under
    # RLS, and a check that silently passes because it saw no row is the failure
    # direction that does not page anybody.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION research_object_versions_supersession_scope() RETURNS trigger AS $$
        DECLARE
          prior RECORD;
        BEGIN
          IF NEW.supersedes_version_id IS NULL THEN
            RETURN NEW;
          END IF;

          SELECT v.research_object_id, v.review_id, v.tenant_id, v.version_number
            INTO prior
            FROM research_object_versions v
           WHERE v.research_object_version_id = NEW.supersedes_version_id;

          IF NOT FOUND THEN
            RAISE EXCEPTION 'superseded version % does not exist', NEW.supersedes_version_id
              USING ERRCODE = 'foreign_key_violation';
          END IF;

          IF prior.research_object_id <> NEW.research_object_id
             OR prior.review_id <> NEW.review_id
             OR prior.tenant_id <> NEW.tenant_id THEN
            RAISE EXCEPTION
              'version % cannot supersede %: different research object, review or tenant',
              NEW.research_object_version_id, NEW.supersedes_version_id
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF prior.version_number >= NEW.version_number THEN
            RAISE EXCEPTION
              'version %s cannot supersede the later or equal version %s',
              NEW.version_number, prior.version_number
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;

        CREATE TRIGGER trg_research_object_versions_supersession_scope
          BEFORE INSERT ON research_object_versions
          FOR EACH ROW EXECUTE FUNCTION research_object_versions_supersession_scope();
        """
    )

    # ---- the reports this study was built from ----------------------------
    #
    # `ER-006.1` "source links". This is the only new table that joins to
    # screening output, and it is the join that stops this slice being a
    # free-floating CRUD layer: an object without a record behind it is a study
    # nobody screened.
    #
    # `corpus_version_id` is pinned rather than looked up, so "this report was a
    # member of the corpus when the object was built" stays checkable after a
    # later import moves the corpus on.
    op.create_table(
        "research_object_records",
        sa.Column("research_object_record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_record_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("corpus_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        # `primary` is the report the study is cited as; `companion` is a
        # secondary publication of the same study. The distinction is what keeps
        # "studies included" and "reports included" from collapsing into one
        # number again.
        sa.Column("link_role", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("linked_by_subject_id", sa.Text(), nullable=False),
        sa.Column("linked_by_display_name", sa.Text(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_object_id", "tenant_id"],
            ["research_objects.research_object_id", "research_objects.tenant_id"],
            ondelete="RESTRICT",
            name="fk_research_object_record_same_tenant",
        ),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["review_record_version_id"],
            ["review_record_versions.review_record_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["corpus_version_id"], ["corpus_versions.corpus_version_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("link_role IN ('primary', 'companion')", name="ck_research_object_record_role"),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_research_object_record_rationale"),
        # One report backs at most one study, review-wide. Two studies claiming
        # the same report is a double count in every downstream number.
        sa.UniqueConstraint("review_id", "review_record_id", name="uq_research_object_record_unique_report"),
    )
    op.execute(_tenant_rls("research_object_records"))
    op.execute(_grant("research_object_records"))
    op.execute(
        _append_only(
            "research_object_records",
            "a link is a recorded judgement; record a correcting link rather than editing this one",
        )
    )

    # Exactly one `primary` report per study. A study with two primaries has no
    # citable report; a study with none cannot be cited at all.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_research_object_record_one_primary
          ON research_object_records (research_object_id)
          WHERE link_role = 'primary';
        """
    )

    # The record must belong to THIS review and tenant, and must be a member of
    # the corpus version being pinned. Everything a constraint can honestly say
    # about a report without re-deriving the flow ledger — see the module
    # docstring for where the inclusion check lives and why it is not here.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION research_object_records_scope() RETURNS trigger AS $$
        DECLARE
          rec RECORD;
        BEGIN
          SELECT r.review_id, r.tenant_id INTO rec
            FROM review_records r
           WHERE r.review_record_id = NEW.review_record_id;

          IF NOT FOUND OR rec.review_id <> NEW.review_id OR rec.tenant_id <> NEW.tenant_id THEN
            RAISE EXCEPTION
              'review_record % does not belong to review % in this tenant',
              NEW.review_record_id, NEW.review_id
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NOT EXISTS (
            SELECT 1 FROM corpus_version_members m
             WHERE m.corpus_version_id = NEW.corpus_version_id
               AND m.review_record_id = NEW.review_record_id
               AND m.review_record_version_id = NEW.review_record_version_id
               AND m.tenant_id = NEW.tenant_id
          ) THEN
            RAISE EXCEPTION
              'review_record version % is not a member of corpus version %',
              NEW.review_record_version_id, NEW.corpus_version_id
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;

        CREATE TRIGGER trg_research_object_records_scope
          BEFORE INSERT ON research_object_records
          FOR EACH ROW EXECUTE FUNCTION research_object_records_scope();
        """
    )

    op.create_index(
        "ix_research_object_versions_object",
        "research_object_versions",
        ["research_object_id", "version_number"],
    )
    op.create_index("ix_research_objects_review", "research_objects", ["review_id"])
    op.create_index("ix_research_object_records_object", "research_object_records", ["research_object_id"])


def downgrade() -> None:
    """A real downgrade. `tests/migration/test_migration_round_trip.py` runs it."""
    op.drop_index("ix_research_object_records_object", table_name="research_object_records")
    op.drop_index("ix_research_objects_review", table_name="research_objects")
    op.drop_index("ix_research_object_versions_object", table_name="research_object_versions")

    for table in ("research_object_records", "research_object_versions", "research_objects"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")

    op.execute("DROP TRIGGER IF EXISTS trg_research_object_records_scope ON research_object_records")
    op.execute("DROP FUNCTION IF EXISTS research_object_records_scope()")
    op.execute("DROP TRIGGER IF EXISTS trg_research_object_versions_supersession_scope ON research_object_versions")
    op.execute("DROP FUNCTION IF EXISTS research_object_versions_supersession_scope()")
    op.execute("DROP TRIGGER IF EXISTS trg_research_object_versions_pins_locked_protocol ON research_object_versions")

    op.drop_table("research_object_records")
    op.drop_table("research_object_versions")
    op.drop_table("research_objects")

    # Created here, so dropped here — `0018` and `0019` reuse it and drop their
    # own triggers, never this function.
    op.execute("DROP FUNCTION IF EXISTS er_pins_locked_protocol()")
