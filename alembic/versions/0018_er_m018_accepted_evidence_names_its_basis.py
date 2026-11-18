"""ER-M018 accepted evidence: extraction, observations, assertions, and acceptance.

Revision ID: 0018_er_m018
Revises: 0017_er_m017
Create Date: 2026-09-02

Work package `ER-006` (`ER-006.2`, `ER-006.4`, `ER-006.5`), slice `8810-E`, wave `W3`.
Spec calls this group `ER-M008`; that slug is taken by `0008` — see `0017`'s
docstring for the mapping.

## The sentence this migration exists to enforce

`16-…:§3.7`: *"Every accepted material assertion must resolve to either:
1. an immutable `DocumentVersionRef` and exact `EvidenceLocation`; or 2. an
explicit `ExpertAssertion` with actor, role, rationale, and decision reference."*

`§13.1` puts that choice on the observation — *"Observation = typed value or
statement with exact evidence or an expert-assertion marker"* — so the columns
live on `observation_versions`, and `ck_observation_basis_exclusive` makes
**neither** and **both** unrepresentable. Not "validated on the way in": a body
that satisfies no arm cannot become a row.

## Why the evidence-located arm is switched off, and why that is the model's rule

`§7.2` assigns `EvidenceLocation` to **Ingestion/Documents**, not to this
service. `DM-AT-25` requires a `PinnedVersionRef` whose owner, type, scope or
hash is wrong to **fail closed**. `8810` has no client that can ask
Ingestion/Documents whether a location is real, so today it cannot tell a
genuine reference from an invented one.

Storing it anyway and calling it evidence is the failure this whole service
exists to prevent, so the located arm is refused at the only place that cannot
be routed around:

    CONSTRAINT ck_observation_located_arm_unverifiable CHECK (basis_kind = 'expert_assertion')

This is deliberately a *second* constraint rather than a narrower first one. The
full shape stays declared and reviewable, so when the Ingestion/Documents
verifier lands, the migration that enables the arm is a one-line
`DROP CONSTRAINT` against a schema that already describes it — not a redesign.
`ER-006.4` asks for "manual/derived observation versions"; the manual path is
the one whose provenance this service can actually stand behind today.

## Acceptance is a decision, never a column

`§13.1`: *"…acceptance … are append-only decisions or bitemporal assertions over
exact versions; they are not mutable columns on those versions."*

`evidence_acceptances` names exactly one subject version
(`num_nonnulls(...) = 1`), and two partial unique indexes make the chain
deterministic, exactly as `0005` did for adjudications:

* one un-superseded acceptance per subject version — so "is this accepted?" has
  one answer, computed from one place;
* one successor per superseded acceptance — so the chain cannot fork.

A withdrawal is not an `UPDATE`; it is a new row citing the one it supersedes.

`subject_content_hash` is a **witness**: the subject's hash copied at insert and
checked equal by trigger. It is what makes "accepted" mean "accepted *this
content*". Without it an acceptance points at a row id, and the row id of an
immutable version tells you nothing you did not already know — but the witness
survives into exports and snapshots, where the version row does not travel.

## The hash graph stays acyclic

`§14.6.7`: *"A later-created translation, binding, evidence link/package,
assessment, set membership, status/applicability assertion, or other reverse
association MUST NOT be hashed back into its accepted subject version."*

Made structural rather than remembered: **no version table in this file carries
any column referencing an acceptance.** The arrow only ever points from the
decision to the version. An `accepted_at` on `observation_versions` would have
been the natural, wrong thing to write, and would have made every observation's
content hash depend on a fact recorded after it was hashed.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_er_m018_accepted_evidence"
down_revision = "0017_er_m017_research_objects"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

#: Every table this migration makes append-only. Read by `tests/conftest.py` —
#: see `0017.APPEND_ONLY_TABLES` for why the constant exists rather than a list
#: retyped in the harness.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "extraction_datasets",
    "extraction_dataset_versions",
    "observations",
    "observation_versions",
    "research_assertions",
    "research_assertion_versions",
    "research_assertion_support",
    "evidence_acceptances",
)

#: The three things an acceptance can be about. Kept as a module constant and
#: mirrored by the ORM, the way `0007` keeps the survivorship vocabulary, so the
#: CHECK and the application cannot drift into two different closed sets.
ACCEPTANCE_SUBJECTS = ("research_object_version", "extraction_dataset_version", "observation_version")


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


def _pin_trigger(table: str) -> str:
    """Reuses `er_pins_locked_protocol` created in `0017`. One implementation."""
    return f"""
        CREATE TRIGGER trg_{table}_pins_locked_protocol
          BEFORE INSERT ON {table}
          FOR EACH ROW EXECUTE FUNCTION er_pins_locked_protocol();
    """


def _identity_columns(key_column: str, key_name: str) -> list[Any]:
    """The identity half of an aggregate: a handle, and nothing that can change.

    Every aggregate in `16-…:§13.1` is a pair, and every identity half is the
    same five columns plus the `UNIQUE (pk, tenant_id)` anchor that lets children
    carry a composite same-tenant foreign key.

    ⚠️ This returns COLUMNS rather than calling `op.create_table` itself, and the
    difference is load-bearing rather than stylistic.
    `tests/unit/test_model_metadata.py` finds every table this service owns by
    walking the migration AST for `op.create_table("<literal>")`. A helper that
    made the call with the name in a VARIABLE hid three tables from it —
    `extraction_datasets`, `observations` and `research_assertions` — and the
    test read them as mapped-but-never-migrated. The table name has to appear as
    a literal at the call site, so the call site is where it stays.
    """
    return [
        sa.Column(key_column, postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(key_column, "tenant_id", name=f"uq_{key_name}_tenant"),
        sa.CheckConstraint("btrim(created_by_subject_id) <> ''", name=f"ck_{key_name}_author"),
    ]


def _identity_guards(name: str) -> None:
    """RLS, the runtime grant and the append-only trigger for an identity table."""
    op.execute(_tenant_rls(name))
    op.execute(_grant(name))
    op.execute(_append_only(name, "identity never changes; revise with a new version"))


def upgrade() -> None:
    # ---- extraction datasets ----------------------------------------------
    #
    # `ER-006.2`. A dataset is the schema- and protocol-pinned container the
    # observations of one study belong to. It exists so that "which schema was
    # this extracted under, against which locked protocol" is answerable per
    # extraction rather than per review.
    op.create_table("extraction_datasets", *_identity_columns("extraction_dataset_id", "extraction_dataset"))
    _identity_guards("extraction_datasets")
    op.add_column(
        "extraction_datasets",
        sa.Column("research_object_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column("extraction_datasets", sa.Column("dataset_key", sa.Text(), nullable=False))
    op.create_foreign_key(
        "fk_extraction_dataset_object_same_tenant",
        "extraction_datasets",
        "research_objects",
        ["research_object_id", "tenant_id"],
        ["research_object_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_extraction_dataset_key", "extraction_datasets", ["research_object_id", "dataset_key"]
    )

    op.create_table(
        "extraction_dataset_versions",
        sa.Column("extraction_dataset_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("extraction_dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        # How this version came to exist. `manual` is the only value a writer can
        # produce today; `karma_proposal` is NOT declared here, because a value
        # no code can write is a vocabulary entry that lies about the system's
        # capabilities. It arrives with `ER-006.3`, in the migration that also
        # brings the proposal table.
        sa.Column("extraction_method", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_by_display_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["extraction_dataset_id", "tenant_id"],
            ["extraction_datasets.extraction_dataset_id", "extraction_datasets.tenant_id"],
            ondelete="RESTRICT",
            name="fk_extraction_dataset_version_same_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["extraction_dataset_versions.extraction_dataset_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("extraction_dataset_id", "version_number", name="uq_extraction_dataset_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_extraction_dataset_version_positive"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_extraction_dataset_hash_shape"),
        sa.CheckConstraint("extraction_method IN ('manual')", name="ck_extraction_method_known"),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_extraction_dataset_rationale"),
        sa.CheckConstraint(
            "(version_number = 1 AND supersedes_version_id IS NULL) "
            "OR (version_number > 1 AND supersedes_version_id IS NOT NULL)",
            name="ck_extraction_dataset_version_chain",
        ),
    )
    op.execute(_tenant_rls("extraction_dataset_versions"))
    op.execute(_grant("extraction_dataset_versions"))
    op.execute(_append_only("extraction_dataset_versions", "a version is immutable; supersede it instead"))
    op.execute(_pin_trigger("extraction_dataset_versions"))
    op.execute(
        """
        CREATE UNIQUE INDEX uq_extraction_dataset_supersession_linear
          ON extraction_dataset_versions (supersedes_version_id)
          WHERE supersedes_version_id IS NOT NULL;
        """
    )

    # ---- observations: a typed value, and what stands behind it -----------
    op.create_table("observations", *_identity_columns("observation_id", "observation"))
    _identity_guards("observations")
    op.add_column(
        "observations",
        sa.Column("extraction_dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column("observations", sa.Column("variable_key", sa.Text(), nullable=False))
    op.create_foreign_key(
        "fk_observation_dataset_same_tenant",
        "observations",
        "extraction_datasets",
        ["extraction_dataset_id", "tenant_id"],
        ["extraction_dataset_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_observation_variable", "observations", ["extraction_dataset_id", "variable_key"])

    op.create_table(
        "observation_versions",
        sa.Column("observation_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Which dataset version this observation's value belongs to. Pinned, so
        # a later dataset revision does not silently re-parent old observations.
        sa.Column("extraction_dataset_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        # ---- `16-…:§3.7`, the either/or -----------------------------------
        sa.Column("basis_kind", sa.Text(), nullable=False),
        # arm 1: evidence-located (declared, currently unreachable — see docstring)
        sa.Column("document_version_ref", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_location", postgresql.JSONB(), nullable=True),
        # arm 2: expert assertion
        sa.Column("expert_subject_id", sa.Text(), nullable=True),
        sa.Column("expert_role", sa.Text(), nullable=True),
        sa.Column("expert_rationale", sa.Text(), nullable=True),
        sa.Column("expert_decision_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_by_display_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["observation_id", "tenant_id"],
            ["observations.observation_id", "observations.tenant_id"],
            ondelete="RESTRICT",
            name="fk_observation_version_same_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["observation_versions.observation_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_dataset_version_id"],
            ["extraction_dataset_versions.extraction_dataset_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("observation_id", "version_number", name="uq_observation_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_observation_version_positive"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_observation_hash_shape"),
        sa.CheckConstraint("basis_kind IN ('evidence_located', 'expert_assertion')", name="ck_observation_basis_known"),
        sa.CheckConstraint(
            "(version_number = 1 AND supersedes_version_id IS NULL) "
            "OR (version_number > 1 AND supersedes_version_id IS NOT NULL)",
            name="ck_observation_version_chain",
        ),
        # `16-…:§3.7`. Neither arm, or both, is unrepresentable.
        sa.CheckConstraint(
            "(basis_kind = 'evidence_located'"
            "  AND document_version_ref IS NOT NULL AND evidence_location IS NOT NULL"
            "  AND expert_subject_id IS NULL AND expert_role IS NULL"
            "  AND expert_rationale IS NULL AND expert_decision_ref IS NULL)"
            " OR (basis_kind = 'expert_assertion'"
            "  AND document_version_ref IS NULL AND evidence_location IS NULL"
            "  AND btrim(expert_subject_id) <> '' AND btrim(expert_role) <> ''"
            "  AND btrim(expert_rationale) <> '' AND expert_decision_ref IS NOT NULL)",
            name="ck_observation_basis_exclusive",
        ),
        # `DM-AT-25` fail-closed, as DDL. Dropped by the migration that lands the
        # Ingestion/Documents `PinnedVersionRef` verifier — not before.
        sa.CheckConstraint("basis_kind = 'expert_assertion'", name="ck_observation_located_arm_unverifiable"),
    )
    op.execute(_tenant_rls("observation_versions"))
    op.execute(_grant("observation_versions"))
    op.execute(_append_only("observation_versions", "a version is immutable; supersede it instead"))
    op.execute(_pin_trigger("observation_versions"))
    op.execute(
        """
        CREATE UNIQUE INDEX uq_observation_supersession_linear
          ON observation_versions (supersedes_version_id)
          WHERE supersedes_version_id IS NOT NULL;
        """
    )

    # ---- research assertions: a reviewed claim over observations ----------
    #
    # `16-…:§13.1` — *"A ResearchAssertion links approved observations or
    # immutable analysis/model artifacts."* The link is a separate table because
    # one claim is supported by many observations and, importantly, may be
    # CONTRADICTED by others: a claim that hides its disconfirming evidence is
    # the specific dishonesty a systematic review exists to prevent.
    op.create_table("research_assertions", *_identity_columns("research_assertion_id", "research_assertion"))
    _identity_guards("research_assertions")
    op.add_column("research_assertions", sa.Column("assertion_key", sa.Text(), nullable=False))
    op.create_unique_constraint("uq_research_assertion_key", "research_assertions", ["review_id", "assertion_key"])

    op.create_table(
        "research_assertion_versions",
        sa.Column("research_assertion_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_assertion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_by_display_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_assertion_id", "tenant_id"],
            ["research_assertions.research_assertion_id", "research_assertions.tenant_id"],
            ondelete="RESTRICT",
            name="fk_research_assertion_version_same_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["research_assertion_versions.research_assertion_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("research_assertion_id", "version_number", name="uq_research_assertion_version_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_research_assertion_version_positive"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_research_assertion_hash_shape"),
        sa.CheckConstraint("btrim(statement) <> ''", name="ck_research_assertion_statement"),
        sa.CheckConstraint(
            "(version_number = 1 AND supersedes_version_id IS NULL) "
            "OR (version_number > 1 AND supersedes_version_id IS NOT NULL)",
            name="ck_research_assertion_version_chain",
        ),
    )
    op.execute(_tenant_rls("research_assertion_versions"))
    op.execute(_grant("research_assertion_versions"))
    op.execute(_append_only("research_assertion_versions", "a version is immutable; supersede it instead"))
    op.execute(_pin_trigger("research_assertion_versions"))
    op.execute(
        """
        CREATE UNIQUE INDEX uq_research_assertion_supersession_linear
          ON research_assertion_versions (supersedes_version_id)
          WHERE supersedes_version_id IS NOT NULL;
        """
    )

    op.create_table(
        "research_assertion_support",
        sa.Column("support_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("research_assertion_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_assertion_version_id"],
            ["research_assertion_versions.research_assertion_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_version_id"],
            ["observation_versions.observation_version_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("stance IN ('supports', 'contradicts')", name="ck_assertion_support_stance"),
        sa.UniqueConstraint(
            "research_assertion_version_id", "observation_version_id", name="uq_assertion_support_once"
        ),
    )
    op.execute(_tenant_rls("research_assertion_support"))
    op.execute(_grant("research_assertion_support"))
    op.execute(
        _append_only(
            "research_assertion_support",
            "support is part of the assertion version it belongs to; revise the assertion instead",
        )
    )

    # ---- acceptance: the decision, over an exact version ------------------
    subjects = ", ".join(f"'{s}'" for s in ACCEPTANCE_SUBJECTS)
    op.create_table(
        "evidence_acceptances",
        sa.Column("acceptance_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("research_object_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("extraction_dataset_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observation_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Generated, so "which version is this about" is one column to index and
        # to make unique, without three nullable partial indexes that would each
        # be correct and collectively unenforceable.
        sa.Column(
            "subject_version_id",
            postgresql.UUID(as_uuid=True),
            sa.Computed(
                "COALESCE(research_object_version_id, extraction_dataset_version_id, observation_version_id)",
                persisted=True,
            ),
            nullable=True,
        ),
        # The witness. See the module docstring: this is what makes "accepted"
        # mean "accepted THIS content" once the row has travelled into a
        # snapshot or an export and left the version table behind.
        sa.Column("subject_content_hash", sa.Text(), nullable=False),
        sa.Column("protocol_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("supersedes_acceptance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_subject_id", sa.Text(), nullable=False),
        sa.Column("decided_by_display_name", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_object_version_id"],
            ["research_object_versions.research_object_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_dataset_version_id"],
            ["extraction_dataset_versions.extraction_dataset_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_version_id"],
            ["observation_versions.observation_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_acceptance_id"], ["evidence_acceptances.acceptance_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["protocol_version_id"], ["review_protocol_versions.protocol_version_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(f"subject_kind IN ({subjects})", name="ck_acceptance_subject_kind_known"),
        sa.CheckConstraint("decision IN ('accept', 'withdraw')", name="ck_acceptance_decision_known"),
        sa.CheckConstraint("btrim(rationale) <> ''", name="ck_acceptance_rationale"),
        sa.CheckConstraint("btrim(decided_by_subject_id) <> ''", name="ck_acceptance_author"),
        sa.CheckConstraint(f"subject_content_hash ~ '{HASH_SHAPE}'", name="ck_acceptance_subject_hash_shape"),
        # Exactly one subject. Not "at least one" — an acceptance naming two
        # versions is a decision about neither.
        sa.CheckConstraint(
            "num_nonnulls(research_object_version_id, extraction_dataset_version_id, observation_version_id) = 1",
            name="ck_acceptance_exactly_one_subject",
        ),
        # And the discriminator must agree with which column is filled, or the
        # column and the label are two answers to one question.
        sa.CheckConstraint(
            "(subject_kind = 'research_object_version' AND research_object_version_id IS NOT NULL)"
            " OR (subject_kind = 'extraction_dataset_version'"
            "     AND extraction_dataset_version_id IS NOT NULL)"
            " OR (subject_kind = 'observation_version' AND observation_version_id IS NOT NULL)",
            name="ck_acceptance_kind_matches_subject",
        ),
        # A withdrawal can never start a chain: there is nothing to withdraw.
        # An acceptance may either start one (the first decision about this
        # version) or continue one (re-accepting after a withdrawal).
        #
        # This constraint originally also forced every `accept` to have a NULL
        # predecessor. That was wrong, and running it found it: after a
        # withdrawal the version could never be accepted again, because the only
        # legal re-acceptance was a second chain root and
        # `uq_acceptance_one_chain_per_subject` correctly refuses those. The
        # reviewer would have been told "duplicate key" for doing exactly what
        # the withdrawal invited them to do.
        sa.CheckConstraint(
            "decision <> 'withdraw' OR supersedes_acceptance_id IS NOT NULL",
            name="ck_acceptance_withdrawal_cites_what_it_withdraws",
        ),
    )
    op.execute(_tenant_rls("evidence_acceptances"))
    op.execute(_grant("evidence_acceptances"))
    op.execute(
        _append_only(
            "evidence_acceptances",
            "a decision is a record of what someone decided; withdraw it with a new row",
        )
    )
    op.execute(_pin_trigger("evidence_acceptances"))
    op.execute(
        """
        -- One CHAIN per subject version, not "one live decision". The pair is
        -- `0005`'s adjudication idiom exactly: one root per key, one successor
        -- per node, therefore exactly one linear chain whose TAIL is the
        -- current fact. Naming it "one live" was the mistake that hid the
        -- re-acceptance defect above.
        CREATE UNIQUE INDEX uq_acceptance_one_chain_per_subject
          ON evidence_acceptances (review_id, subject_version_id)
          WHERE supersedes_acceptance_id IS NULL;

        CREATE UNIQUE INDEX uq_acceptance_supersession_linear
          ON evidence_acceptances (supersedes_acceptance_id)
          WHERE supersedes_acceptance_id IS NOT NULL;
        """
    )

    # The witness must actually witness. Without this the column is a free-text
    # field the writer fills in, and an acceptance could claim a content hash
    # the subject never had — which is worse than not recording one, because it
    # looks like verification.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION evidence_acceptances_witnesses_its_subject() RETURNS trigger AS $$
        DECLARE
          actual text;
          prior RECORD;
          mine uuid;
        BEGIN
          SELECT CASE NEW.subject_kind
                   WHEN 'research_object_version' THEN
                     (SELECT v.content_hash FROM research_object_versions v
                       WHERE v.research_object_version_id = NEW.research_object_version_id)
                   WHEN 'extraction_dataset_version' THEN
                     (SELECT v.content_hash FROM extraction_dataset_versions v
                       WHERE v.extraction_dataset_version_id = NEW.extraction_dataset_version_id)
                   WHEN 'observation_version' THEN
                     (SELECT v.content_hash FROM observation_versions v
                       WHERE v.observation_version_id = NEW.observation_version_id)
                 END
            INTO actual;

          IF actual IS NULL THEN
            RAISE EXCEPTION 'acceptance names a % that does not exist', NEW.subject_kind
              USING ERRCODE = 'foreign_key_violation';
          END IF;

          IF actual <> NEW.subject_content_hash THEN
            RAISE EXCEPTION
              'acceptance witnesses % but the % actually hashes to %',
              NEW.subject_content_hash, NEW.subject_kind, actual
              USING ERRCODE = 'restrict_violation';
          END IF;

          -- A chain must stay about ONE version. Without this, a withdrawal of
          -- observation X could cite the acceptance of observation Y as its
          -- predecessor: both partial uniques would still be satisfied, and the
          -- history would read as a coherent chain about neither.
          IF NEW.supersedes_acceptance_id IS NOT NULL THEN
            SELECT a.subject_version_id, a.review_id, a.tenant_id INTO prior
              FROM evidence_acceptances a
             WHERE a.acceptance_id = NEW.supersedes_acceptance_id;

            -- NOT `NEW.subject_version_id`: it is a GENERATED column, and
            -- Postgres computes generated values AFTER BEFORE-triggers run, so
            -- it is still NULL here. Comparing against it would make every
            -- supersession fail this check for a reason that has nothing to do
            -- with the data.
            mine := COALESCE(NEW.research_object_version_id,
                             NEW.extraction_dataset_version_id,
                             NEW.observation_version_id);

            IF NOT FOUND THEN
              RAISE EXCEPTION 'superseded acceptance % does not exist', NEW.supersedes_acceptance_id
                USING ERRCODE = 'foreign_key_violation';
            END IF;

            IF prior.subject_version_id <> mine
               OR prior.review_id <> NEW.review_id
               OR prior.tenant_id <> NEW.tenant_id THEN
              RAISE EXCEPTION
                'acceptance % cannot supersede %: a different subject version, review or tenant',
                NEW.acceptance_id, NEW.supersedes_acceptance_id
                USING ERRCODE = 'restrict_violation';
            END IF;
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;

        CREATE TRIGGER trg_evidence_acceptances_witnesses_its_subject
          BEFORE INSERT ON evidence_acceptances
          FOR EACH ROW EXECUTE FUNCTION evidence_acceptances_witnesses_its_subject();
        """
    )

    op.create_index("ix_observations_dataset", "observations", ["extraction_dataset_id"])
    op.create_index("ix_observation_versions_observation", "observation_versions", ["observation_id", "version_number"])
    op.create_index("ix_acceptances_subject", "evidence_acceptances", ["review_id", "subject_version_id"])
    op.create_index("ix_assertion_support_observation", "research_assertion_support", ["observation_version_id"])


def downgrade() -> None:
    for index, table in (
        ("ix_assertion_support_observation", "research_assertion_support"),
        ("ix_acceptances_subject", "evidence_acceptances"),
        ("ix_observation_versions_observation", "observation_versions"),
        ("ix_observations_dataset", "observations"),
    ):
        op.drop_index(index, table_name=table)

    op.execute("DROP TRIGGER IF EXISTS trg_evidence_acceptances_witnesses_its_subject ON evidence_acceptances")
    op.execute("DROP FUNCTION IF EXISTS evidence_acceptances_witnesses_its_subject()")

    # Reverse creation order, derived from the constant so the drop list and the
    # REVOKE list cannot drift apart.
    tables = tuple(reversed(APPEND_ONLY_TABLES))
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_pins_locked_protocol ON {table}")

    for table in tables:
        op.drop_table(table)
