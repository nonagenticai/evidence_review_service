"""ER-M025: `artifact_records` — an Artifact Service pointer, never a storage key.

Revision ID: 0025_er_m025_artifact_records
Revises: 0024_er_m024_log_catalogue
Create Date: 2026-09-05

`FND-009` — "immutable `ArtifactStore` and rights snapshot adapter"
(`13-…:§17`). Its row is `13-…:376`, verbatim:

> `artifact_records` | Artifact Service ID/version/hash, classification, rights
> snapshot, semantic role and owning aggregate; never a raw storage key

Every column below traces to that sentence, to the rights-snapshot fields at
`13-…:754-757`, or to the residency mechanism at `13-…:795-798`. Nothing here
is this service's own invention.

## What does NOT exist yet, and is not simulated

`13-…:§17`'s dependency column names `FND-009`'s own prerequisite: "`FND-002`,
Ingestion rights contract". Neither the Artifact Service nor a rights authority
exists. `0021`'s `snapshot_artifacts` already found this gap for evidence
artifacts and reproduces bytes from a manifest rather than inventing a store;
this table is the OTHER half `0021`'s own docstring named as `FND-009` and left
undone — a row that can hold what an artifact IS (its identity, its
classification, its rights snapshot) without holding what only the real
Artifact Service can hand back (bytes, a signed URL, a storage key).

## The rights re-check is measured as SEVEN actions, not eight

`13-…:759-760`: "The service checks current rights before display,
model/provider use, translation, quotation, export, redistribution and
publication." Counted directly off that sentence: display (1), model/provider
use (2), translation (3), quotation (4), export (5), redistribution (6),
publication (7) — **seven**, not any other number a paraphrase might carry
forward.

None of the seven can be wired today. `13-…:1020` (`FND-015`'s row) makes the
rights-change/deletion contract depend on `ING-00`/`FRCP-00` inventories and
`IMP-DEC-005`; more directly, `FND-009` itself depends on the "Ingestion rights
contract", and Ingestion's `DocumentVersion`/rights model is backlog item 14 —
absent from this codebase today, the same absence `0021` measured for
`proposal_decisions` and `ProposalBundle`. There is no rights authority to call,
so there is nothing this table's writers can check against, and simulating a
pass would be indistinguishable from having verified one.

So, exactly as `0021.gates_evaluated` records which of `14-…:345`'s five gates
actually ran, `rights_checks_evaluated` records which of these seven actions
were re-verified against a live rights authority before this row was written.
`RIGHTS_CHECKABLE_ACTIONS` is the closed set the column may name; today no
writer names any of them, so the column defaults to `'{}'` on every row, and a
consumer asking "was this display re-checked?" gets an honest "no", not a
column that does not exist to ask.

## Two gaps this migration deliberately does NOT close

**The disposition matrix (`13-…:763-777`).** Seven bullets under "Before
production, legal/security owners approve a disposition matrix for:" — tenant
metadata deletion, licensed content removal, participant/confidential data
erasure, legal hold, artifact crypto-shredding, retained non-content hashes,
already-published outputs. No cells, no states, no schema. `IMP-DEC-005`
("lawful erasure versus immutable provenance", `15-…:963`) is open, gated on
"first real customer import". There is therefore no `disposition` column, no
disposition enum and no erasure state machine here — inventing one would be
answering a question the named owners have not yet answered, the exact mistake
`0024` refused to make about deleting an uncatalogued log row.

**Residency policy CONTENT (`13-…:787-793`).** "Before environment
provisioning, platform/security owners record the allowed region for each
tenant and data class…" has no schema in this repository, and `IMP-DEC-019`
("regional event/artifact/registry topology", `15-…:977`) is open, gated on
"production environment provisioning". So `residency_policy_ref` below stores
ONLY the reference `13-…:795` requires every artifact token to carry — no
region name, no allowed-region enum, no replication/backup/DR/support-access
columns. What IS enforced is the MECHANISM at `13-…:795-798`: "Cross-region
movement is denied unless that exact policy authorizes it." This table has no
column, function or code path that moves, replicates or re-homes a recorded
artifact to a different region — the deny-by-default posture is the absence of
any such mechanism, not a flag guarding one that exists.

## "Never a raw storage key" is a CHECK, not a comment

`research_contracts` 0.x already released `urn:datacompany:typed-refs:1#/$defs/artifact_ref`
and ships the fixture that makes this enforceable:
`fixtures/foundation/v1/invalid/typed-ref-artifact-carrying-a-storage-key.json`
smuggles `"storage_key": "s3://artifact-store/runs/2026/07/output.parquet?expires=3600"`
into an otherwise-valid pin, and its `expected_schema_failures` names
`unevaluatedProperties` as the keyword that refuses it — refused for having a
member the pin does not declare, not for any pattern on the value itself. The
sibling `typed-ref-artifact-carrying-slot-members-inline.json` proves the other
half of the same sentence: `semantic_role`, `classification` and
`rights_snapshot_ref` belong on the ENCLOSING row (`artifact_records`, here),
never inside the pin.

A JSON Schema `unevaluatedProperties: false` has no SQL analogue — this table
has named, typed columns rather than a validated envelope — so the refusal has
to be re-expressed as a CHECK on the column that plays the pin's role:
`artifact_id` and `artifact_version`. Both are cross-service VALUES (`13-…`'s
"Foreign identifiers from other services are values plus version/hash
snapshots, never SQL foreign keys into another database"), so they are
opaque `TEXT`, typed exactly widely enough that a caller COULD write a bucket
path, an `s3://` URI or any other slash-bearing locator into them by mistake —
which is precisely the mistake the fixture demonstrates and precisely what
`ck_artifact_records_artifact_id_not_storage_key` and its version-column sibling
refuse. `content_hash` needs no matching CHECK for the SAME shape: its own
`HASH_SHAPE` regex (`^sha256:[0-9a-f]{64}$`) already admits no slash, so a
storage key could not pass it under a different name.

`tests/migration/test_the_artifact_ref_is_never_a_storage_key.py` proves both
directions: the exact string from the negative fixture is refused, and the
exact `entity_id`/`content_hash` values from the positive
`typed-ref-artifact.json` fixture are accepted.

## Append-only, and why no supersession chain

`13-…:376` calls this a "snapshot" and `§11.2` says a rights-change "does not
silently erase history or continue delivery" — the same argument `0021` made
for `evidence_snapshots`. So this table takes the append-only trigger.

It does NOT take a `supersedes_record_id` chain. `0021` needed one because a
snapshot is superseded BY CONSTRUCTION — creating version 2 is the service's own
write path. A rights re-check is not: no writer exists that re-evaluates rights
and writes a successor row, because no rights authority exists to call (see
above). Adding a chain column with no writer that populates it is exactly the
shape `tests/migration/test_a_supersession_chain_declares_its_scope.py` polices
for the OTHER five work packages that already have one and no guard — this
table avoids joining that list by not having the column until a real writer
needs it.

## Why there is no FK from `owning_aggregate_id`

`owning_aggregate_type` is deliberately open — a research object version, an
evidence snapshot, a review, a durable job's result, anything this service
might attach a produced or ingested artifact to. `0022.domain_inbox` took the
same `aggregate_type` + `aggregate_id` shape for the identical reason: a single
column cannot be a foreign key into more than one table, and a per-type
constraint would need one CHECK per aggregate type today and a migration for
every one added later. `owning_aggregate_type` still carries the token-shape
CHECK every open classifier column in this schema carries.

## Cross-tenant worker visibility

`ENABLE`/`FORCE ROW LEVEL SECURITY` plus the per-tenant policy, and — matching
every operational table since `0020` — an ADDITIVE policy scoped to
`evidence_review_worker`, guarded exactly as `0022`/`0023` guard theirs: skipped
with a `NOTICE` when the role does not exist. No worker reads this table today;
it is provisioned the same way `0022` provisioned `research_domain_events`
before any relay existed, so a future impact-work consumer for a rights-change
event does not need a schema migration of its own just to see the rows it must
reconcile.

## A known gap this migration cannot close from its own scope

`tests/conftest.py._APPEND_ONLY_MIGRATIONS` is a hand-maintained list that
re-asserts each append-only migration's REVOKE after the harness's blanket
`GRANT …, UPDATE, DELETE … TO evidence_review_app`. This migration is
deliberately NOT added to it: this task's scope is `alembic/versions/0025_*.py`
and a test under `tests/migration/`, and that list lives in `tests/conftest.py`,
outside both. Until a follow-up adds `"0025_er_m025_an_artifact_ref_is_never_a_storage_key.py"`
there, the harness's blanket grant restores `UPDATE, DELETE` on
`artifact_records` to `evidence_review_app` in every local/CI test run — the
`trg_artifact_records_append_only` trigger still refuses both unconditionally,
because a trigger fires regardless of privilege, but the PRIVILEGE half of the
guard (the one `0015`'s and `0022`'s docstrings warn is easy to lose silently)
is not yet re-asserted for this table. Recorded here rather than left to be
discovered as a surprise pass in `test_the_runtime_role_is_refused_by_the_grant`-shaped
test, the way `0015`'s docstring says it was the first time.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_er_m025_artifact_records"
down_revision = "0024_er_m024_log_catalogue"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

WORKER_ROLE = "evidence_review_worker"

#: `13-…:759-760`, counted directly: seven actions, not eight. No writer names
#: any of them today — see the module docstring on why there is no rights
#: authority to check against yet. Mirrors `0021.SNAPSHOT_GATES`'s posture.
RIGHTS_CHECKABLE_ACTIONS: tuple[str, ...] = (
    "display",
    "model_provider_use",
    "translation",
    "quotation",
    "export",
    "redistribution",
    "publication",
)

#: Read by `tests/conftest.py` — see `0017.APPEND_ONLY_TABLES`. Not yet wired
#: into `_APPEND_ONLY_MIGRATIONS` there; see the module docstring's last
#: section on why that is an explicit, recorded gap rather than an omission.
APPEND_ONLY_TABLES: tuple[str, ...] = ("artifact_records",)

_TOKEN_SHAPE = r"^[a-z][a-z0-9_]*$"
_REGULATORY_DENYLIST = "regulatory|legal_|normative|consolidation"

#: `research_contracts` `fixtures/foundation/v1/invalid/typed-ref-artifact-carrying-a-storage-key.json`
#: smuggles `s3://artifact-store/runs/2026/07/output.parquet?expires=3600` in
#: beside a pin; `bucket/path`-shaped and `s3://…`-shaped values share exactly
#: one property no legitimate opaque ID or hash has: a slash. One character
#: class refuses "s3://…", "bucket/path" and any other slash-bearing opaque
#: blob in a single regex, and also refuses blank — `+` requires at least one
#: character.
_NOT_STORAGE_KEY_SHAPE = r"^[^/]+$"


def _tenant_rls(table: str) -> str:
    """Verbatim from `0019`/`0020`/`0021`/`0022`, kept per-migration for their reason."""
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = {_TENANT_COMPARAND})
          WITH CHECK (tenant_id = {_TENANT_COMPARAND});
    """


def _worker_policy(table: str) -> str:
    """Cross-tenant discovery, granted ADDITIVELY to one restricted role.

    Verbatim from `0020`/`0022`. Postgres ORs permissive policies, so this
    widens the table for `evidence_review_worker` and for nobody else.
    """
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
            EXECUTE 'CREATE POLICY {table}_worker ON {table} TO {WORKER_ROLE} '
                    'USING (true) WITH CHECK (true)';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {table} TO {WORKER_ROLE}';
          ELSE
            RAISE NOTICE 'role {WORKER_ROLE} is absent; cross-tenant worker policy on '
                         '{table} skipped — the table is visible per-tenant only.';
          END IF;
        END
        $$;
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
    rights_actions = ", ".join(f"'{a}'" for a in RIGHTS_CHECKABLE_ACTIONS)

    op.create_table(
        "artifact_records",
        sa.Column("artifact_record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The aggregate this artifact belongs to. Open, not a single FK target —
        # see the module docstring's `0022.domain_inbox` comparison.
        sa.Column("owning_aggregate_type", sa.Text(), nullable=False),
        sa.Column("owning_aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        # `13-…:376`: "Artifact Service ID/version/hash". Cross-service VALUES,
        # never a SQL foreign key — `13-…`'s foundation-wide rule for foreign
        # identifiers. Opaque TEXT, which is exactly why the storage-key CHECK
        # below is load-bearing rather than redundant with the column type.
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column("artifact_content_hash", sa.Text(), nullable=False),
        # `13-…:376`: "classification". No enum is released for this value
        # anywhere in the shared foundation — only a token shape and the same
        # regulatory denylist `0021`/`0022` carry for open classifier columns.
        sa.Column("classification", sa.Text(), nullable=False),
        # `13-…:376`: "semantic role". Equally open — no released vocabulary.
        sa.Column("semantic_role", sa.Text(), nullable=False),
        # Rights snapshot, `13-…:754-757` verbatim, one column per fact named:
        # "source/document/version ID" (three identifiers, one bullet).
        sa.Column("rights_source_id", sa.Text(), nullable=False),
        sa.Column("rights_document_id", sa.Text(), nullable=False),
        sa.Column("rights_document_version_id", sa.Text(), nullable=False),
        # "rights-decision ID and evaluated purpose".
        sa.Column("rights_decision_id", sa.Text(), nullable=False),
        sa.Column("rights_evaluated_purpose", sa.Text(), nullable=False),
        # "decision version/hash and evaluation time".
        sa.Column("rights_decision_version", sa.Text(), nullable=False),
        sa.Column("rights_decision_hash", sa.Text(), nullable=False),
        sa.Column("rights_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        # "any expiry or territory constraint" — nullable because `13-…:757`
        # says "any", not "always".
        sa.Column("rights_expiry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rights_territory_constraint", sa.Text(), nullable=True),
        # `13-…:759-760`'s rights re-check, recorded as an honest empty set —
        # see the module docstring's `0021.gates_evaluated` comparison.
        sa.Column(
            "rights_checks_evaluated",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # `13-…:795-798`'s mechanism: a reference, and nothing about what it
        # permits — see the module docstring on why no region content is here.
        sa.Column("residency_policy_ref", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            f"owning_aggregate_type ~ '{_TOKEN_SHAPE}'", name="ck_artifact_records_aggregate_type_shape"
        ),
        # ⛔ THE enforcement clause. See the module docstring's "never a raw
        # storage key" section for what this refuses and why it is a CHECK
        # rather than a comment.
        sa.CheckConstraint(
            f"artifact_id ~ '{_NOT_STORAGE_KEY_SHAPE}'", name="ck_artifact_records_artifact_id_not_storage_key"
        ),
        sa.CheckConstraint(
            f"artifact_version ~ '{_NOT_STORAGE_KEY_SHAPE}'",
            name="ck_artifact_records_artifact_version_not_storage_key",
        ),
        sa.CheckConstraint(f"artifact_content_hash ~ '{HASH_SHAPE}'", name="ck_artifact_records_artifact_hash_shape"),
        sa.CheckConstraint(f"classification ~ '{_TOKEN_SHAPE}'", name="ck_artifact_records_classification_shape"),
        sa.CheckConstraint(
            f"classification !~ '{_REGULATORY_DENYLIST}'",
            name="ck_artifact_records_classification_not_regulatory",
        ),
        sa.CheckConstraint(f"semantic_role ~ '{_TOKEN_SHAPE}'", name="ck_artifact_records_semantic_role_shape"),
        sa.CheckConstraint("length(trim(rights_source_id)) > 0", name="ck_artifact_records_rights_source_id_not_blank"),
        sa.CheckConstraint(
            "length(trim(rights_document_id)) > 0", name="ck_artifact_records_rights_document_id_not_blank"
        ),
        sa.CheckConstraint(
            "length(trim(rights_document_version_id)) > 0",
            name="ck_artifact_records_rights_document_version_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(rights_decision_id)) > 0", name="ck_artifact_records_rights_decision_id_not_blank"
        ),
        sa.CheckConstraint(
            f"rights_evaluated_purpose ~ '{_TOKEN_SHAPE}'", name="ck_artifact_records_rights_purpose_shape"
        ),
        sa.CheckConstraint(
            f"rights_evaluated_purpose !~ '{_REGULATORY_DENYLIST}'",
            name="ck_artifact_records_rights_purpose_not_regulatory",
        ),
        sa.CheckConstraint(
            "length(trim(rights_decision_version)) > 0", name="ck_artifact_records_rights_decision_version_not_blank"
        ),
        sa.CheckConstraint(
            f"rights_decision_hash ~ '{HASH_SHAPE}'", name="ck_artifact_records_rights_decision_hash_shape"
        ),
        sa.CheckConstraint(
            "rights_expiry_at IS NULL OR rights_expiry_at > rights_evaluated_at",
            name="ck_artifact_records_rights_expiry_after_evaluation",
        ),
        sa.CheckConstraint(
            "rights_territory_constraint IS NULL OR length(trim(rights_territory_constraint)) > 0",
            name="ck_artifact_records_rights_territory_not_blank",
        ),
        # The closed set of seven, and no repeats — the SAME function `0021`
        # created for `gates_evaluated`. It still exists at this revision (only
        # `0021`'s own downgrade drops it) and is reused rather than redefined,
        # for the reason `0019.REGISTERED_EVENTS` gives about one shared truth.
        sa.CheckConstraint(
            f"rights_checks_evaluated <@ ARRAY[{rights_actions}]::text[]",
            name="ck_artifact_records_rights_checks_are_registered",
        ),
        sa.CheckConstraint(
            "er_text_array_is_a_set(rights_checks_evaluated)",
            name="ck_artifact_records_rights_checks_have_no_repeats",
        ),
        sa.CheckConstraint(
            "length(trim(residency_policy_ref)) > 0", name="ck_artifact_records_residency_policy_ref_not_blank"
        ),
    )
    op.create_index(
        "ix_artifact_records_owning_aggregate",
        "artifact_records",
        ["tenant_id", "owning_aggregate_type", "owning_aggregate_id"],
    )
    op.create_index(
        "ix_artifact_records_artifact",
        "artifact_records",
        ["tenant_id", "artifact_id", "artifact_version"],
    )

    op.execute(_tenant_rls("artifact_records"))
    op.execute(_worker_policy("artifact_records"))
    op.execute(_grant("artifact_records"))
    op.execute(
        _append_only(
            "artifact_records",
            "a rights snapshot that can be edited after the fact is not a snapshot, and a "
            "rights-change must open new impact work rather than rewrite what was checked",
        )
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_artifact_records_append_only ON artifact_records")
    op.execute("DROP FUNCTION IF EXISTS artifact_records_append_only()")
    op.execute("DROP POLICY IF EXISTS artifact_records_worker ON artifact_records")
    op.execute("DROP POLICY IF EXISTS artifact_records_tenant ON artifact_records")
    op.drop_index("ix_artifact_records_artifact", table_name="artifact_records")
    op.drop_index("ix_artifact_records_owning_aggregate", table_name="artifact_records")
    op.drop_table("artifact_records")
