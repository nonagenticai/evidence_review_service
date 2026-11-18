"""ER-M021 evidence snapshots: a run can say exactly what it was allowed to read.

Revision ID: 0021_er_m021
Revises: 0020_er_m020
Create Date: 2026-09-03

`ER-007` (`14-…:343-367`). The spec names three tables — `evidence_snapshots`,
`evidence_snapshot_members`, `snapshot_artifacts` — under the group slug
`ER-M010`; that slug was consumed long ago, so the migration keeps the sequential
number and the mapping convention `0017` documents.

## What a snapshot is for

`14-…:358` — "Generated clients let `8808`, `8813` and `8811` retrieve metadata
and authorized artifact refs. Consumers pin the snapshot ID/hash and never
dereference 'latest.'" Without this table, a downstream analysis can only say it
ran "against the review", and the review moves. A published number whose inputs
cannot be named again is not reproducible, and re-deriving it later silently
answers a different question.

## The manifest grammar is RELEASED, not invented here

`schema_ref` is pinned by CHECK to `urn:datacompany:evidence-snapshot-manifest:1`,
released in `research_contracts` 0.6.0. `13-…:§3` freezes cross-service shapes in
that repository precisely so a producer and a consumer cannot each invent one:
they would agree until the first disagreement, and the disagreement surfaces as
a reproducibility failure in a scientific run rather than as a validation error.
This is the same posture `0019` took for the event envelope.

## ⚠️ The gate ledger, and why it is a COLUMN rather than a comment

`14-…:345` requires snapshot creation to reject five things: "unresolved
critical proposals, conflicts, stale mandatory evidence, denied rights and an
uncommitted profile/protocol."

Two of the five are implementable against this service today. The other three
have no subject matter here at all — measured on `origin/main` at `babef21`:
there is no `proposal_decisions` table and no `ProposalBundle` contract
(`domain/policies/event_catalog.py` says so), no rights or licence model, and no
notion of mandatory evidence going stale. Building a gate over an absent concept
is how `a-control-whose-premise-is-unmet-manufactures-evidence` happens: the
check passes, the snapshot looks fully vetted, and nothing was examined.

So `gates_evaluated` records, per snapshot, exactly which gates ran. A consumer
deciding whether it may bind a snapshot reads that array instead of assuming
five. `ck_evidence_snapshots_gate_ledger_is_honest` refuses a row that claims a
gate this migration does not know about, and `domain/policies/snapshot_gates.py`
holds the same closed set with the work package that owes each missing one.

The alternative — writing the three unimplementable gates as no-op passes — is
strictly worse than leaving them out, because it is indistinguishable from
having run them.

## Members are ONE heterogeneous set, and the digest is over the sorted set

`14-…:344` lists six member kinds. `evidence_snapshot_members` carries
`owner_context` + `entity_type` per row, which discriminates them, exactly as
the released `urn:datacompany:pinned-version-ref:1` does. Six typed tables would need
six digests and a migration on the day a seventh kind appears.

`16-…:§3.5` is explicit that "RFC 8785 preserves array order; it does not
canonicalize a mathematical set", so `member_manifest_hash` is computed over the
members SORTED by the released order key
(`owner_context, entity_type, entity_id, version_id`). Two producers holding
identical evidence must compute the same digest or the manifest means nothing.
`uq_snapshot_member_identity` makes the set property a database fact rather than
an application convention.

## `snapshot_artifacts` stores a digest and NOT the bytes

`13-…:§11` gives artifacts to an `ArtifactStore` that is `FND-009` and does not
exist. Rather than invent one, or inline a blob into a row nobody can delete,
the artifact row records `payload_digest`, `byte_size`, `media_type` and
`serializer_version`, and the service REPRODUCES the bytes on demand from the
manifest.

That is not a workaround, it is the stronger design for this particular
completion criterion. `14-…:364` requires that "the snapshot reconstructs
byte-identical canonical JSON … under the same serializer version". A stored
blob can only be handed back; a reproduced one is re-derived from the pinned
members every time and compared to the digest, so a member that drifted is
caught by the check rather than hidden behind a cached copy of the old answer.

## Immutable, and superseded rather than edited

`14-…:356` — "It is immutable after creation; changed evidence creates a new
snapshot with an impact link." All three tables take `0019`'s append-only
trigger. The impact link is `supersedes_snapshot_id`, which is the SAME edge the
released manifest carries as `supersedes_version_id`; a second spelling would
have meant two ways to say one thing. `uq_snapshot_supersession_linear` and
`uq_snapshot_one_chain_per_review` are `0018`'s acceptance idiom: one root per
review, one successor per node. Those two ALONE are not the idiom — `0018` backs
them with a trigger, and the first draft of this migration did not, which left a
chain free to cross reviews. `evidence_snapshots_supersede_within_one_review` is
that trigger; with it, exactly one chain per review whose tail is the current
snapshot.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021_er_m021_evidence_snapshots"
down_revision = "0020_er_m020_durable_jobs"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

#: Released in `research_contracts` 0.6.0. Pinned as a CHECK for the reason
#: `0019` pins the envelope: a row whose declared grammar is a document nobody
#: released is the exact defect the release exists to have repaired.
MANIFEST_SCHEMA_REF = "urn:datacompany:evidence-snapshot-manifest:1"

#: The five gates `14-…:345` names, as the closed set the ledger may contain.
#: Mirrored by `src/domain/policies/snapshot_gates.py`, which also records the
#: work package that owes each unimplemented one. Two independent spellings held
#: equal by a test, for the reason `0019.REGISTERED_EVENTS` gives.
SNAPSHOT_GATES: tuple[str, ...] = (
    "unresolved_conflicts",
    "uncommitted_protocol",
    "unresolved_critical_proposals",
    "denied_rights",
    "stale_mandatory_evidence",
)

#: The member kinds this service can pin TODAY. `research_object_version`,
#: `extraction_dataset_version` and `observation_version` are exactly
#: `models/evidence.ACCEPTANCE_SUBJECTS` — a snapshot may only pin what somebody
#: accepted. `review_record_version` is the source pin and is accepted
#: transitively, through the corpus version its research object cites.
#:
#: `research_assertion_version` is NOT here and that is not an oversight:
#: `ACCEPTANCE_SUBJECTS` does not include it, so no assertion has ever been
#: accepted and a snapshot pinning one would be pinning an unaccepted input,
#: which `09-…:329` refuses outright. Appraisal and terminology versions have no
#: table in this service at all.
MEMBER_ENTITY_TYPES: tuple[str, ...] = (
    "research_object_version",
    "extraction_dataset_version",
    "observation_version",
    "review_record_version",
)

#: Read by `tests/conftest.py` — see `0017.APPEND_ONLY_TABLES`.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "evidence_snapshots",
    "evidence_snapshot_members",
    "snapshot_artifacts",
)

_TOKEN_SHAPE = r"^[a-z][a-z0-9_]*$"
#: ⚠️ HYPHEN, not underscore, and NOT `_TOKEN_SHAPE`.
#: `urn:datacompany:identity-ref:1` constrains `owner_context` to
#: `^[a-z][a-z0-9-]*$`, and this column mirrors a released contract rather than
#: this service's own token convention. The first draft reused `_TOKEN_SHAPE`
#: and the two disagreed on exactly one character, so every snapshot build
#: passed schema validation and then died on a CHECK — the manifest and the
#: table would have been describing the same member with two different spellings
#: of its owner.
_OWNER_CONTEXT_SHAPE = r"^[a-z][a-z0-9-]*$"
_MEDIA_TYPE_SHAPE = r"^[a-z]+/[a-zA-Z0-9.+_-]+$"
_REGULATORY_DENYLIST = "regulatory|legal_|normative|consolidation"


#: A CHECK cannot contain a subquery — Postgres refuses `cannot use subquery in
#: check constraint` outright — so "this array names no gate twice" needs a
#: function, the way `0019` needed one for the recursive payload bound. Kept
#: `IMMUTABLE` because a CHECK may only call an immutable expression.
_DISTINCT_GUARD = """
    CREATE OR REPLACE FUNCTION er_text_array_is_a_set(a text[]) RETURNS boolean AS $$
      SELECT a IS NULL
          OR cardinality(a) = (SELECT count(DISTINCT x) FROM unnest(a) AS x);
    $$ LANGUAGE sql IMMUTABLE;
"""


def _tenant_rls(table: str) -> str:
    """Verbatim from `0019`/`0020`, and kept per-migration for the same reason."""
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
    op.execute(_DISTINCT_GUARD)

    gates = ", ".join(f"'{g}'" for g in SNAPSHOT_GATES)
    entity_types = ", ".join(f"'{t}'" for t in MEMBER_ENTITY_TYPES)

    op.create_table(
        "evidence_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reviews.review_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "supersedes_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_snapshots.snapshot_id"),
            nullable=True,
        ),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        # `14-…:352`: "accepts exact dataset/decision/appraisal revisions,
        # terminology/profile/protocol pins AND INTENDED USE". Required, because
        # `14-…:366` forbids a downstream run binding "an unresolved or
        # non-publication snapshot" — a check that cannot be made against a
        # snapshot which never declared what it was for.
        sa.Column("intended_use", sa.Text(), nullable=False),
        sa.Column(
            "protocol_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("review_protocol_versions.protocol_version_id"),
            nullable=False,
        ),
        sa.Column(
            "corpus_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("corpus_versions.corpus_version_id"),
            nullable=True,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("member_manifest_hash", sa.Text(), nullable=False),
        # `corpus_versions` carries this and so does a snapshot, for the same
        # reason: a digest computed under a different order key is not merely a
        # different number, it is not COMPARABLE, and a consumer that recomputed
        # under its own ordering would report tampering where there is none.
        sa.Column("member_ordering_rule", sa.Text(), nullable=False),
        # The snapshot's OWN canonical hash, over the manifest with this column
        # removed — `16-…:§3.5`, "removed from its own hash view and recomputed,
        # never trusted".
        sa.Column("content_hash", sa.Text(), nullable=False),
        # See the module docstring. An array rather than five booleans: a
        # boolean set can only say false, which reads as "gate failed" where the
        # truth is "gate does not exist".
        sa.Column(
            "gates_evaluated",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # Which durable job built it. `14-…:353` routes creation through
        # `GET /review-jobs/{job_id}`, so a snapshot that cannot name its job
        # cannot be traced back to the request that asked for it.
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("durable_jobs.job_id"),
            nullable=False,
        ),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"schema_ref = '{MANIFEST_SCHEMA_REF}'", name="ck_evidence_snapshots_manifest_pinned"),
        sa.CheckConstraint("version_number >= 1", name="ck_evidence_snapshots_version_positive"),
        sa.CheckConstraint(
            "(version_number = 1) = (supersedes_snapshot_id IS NULL)",
            name="ck_evidence_snapshots_first_version_supersedes_nothing",
        ),
        sa.CheckConstraint(f"intended_use ~ '{_TOKEN_SHAPE}'", name="ck_evidence_snapshots_intended_use_shape"),
        sa.CheckConstraint(
            f"intended_use !~ '{_REGULATORY_DENYLIST}'",
            name="ck_evidence_snapshots_intended_use_not_regulatory",
        ),
        sa.CheckConstraint("member_count >= 0", name="ck_evidence_snapshots_member_count"),
        sa.CheckConstraint(f"member_manifest_hash ~ '{HASH_SHAPE}'", name="ck_evidence_snapshots_member_hash_shape"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_evidence_snapshots_content_hash_shape"),
        sa.CheckConstraint(
            f"member_ordering_rule ~ '{_TOKEN_SHAPE}'", name="ck_evidence_snapshots_ordering_rule_shape"
        ),
        # The ledger may only name gates this migration knows about, and may not
        # name one twice. A free-text array would let a caller write
        # `['all_gates_passed']` and look thoroughly vetted.
        sa.CheckConstraint(
            f"gates_evaluated <@ ARRAY[{gates}]::text[]",
            name="ck_evidence_snapshots_gate_ledger_is_honest",
        ),
        sa.CheckConstraint(
            "er_text_array_is_a_set(gates_evaluated)",
            name="ck_evidence_snapshots_gate_ledger_has_no_repeats",
        ),
        sa.UniqueConstraint("review_id", "version_number", name="uq_snapshot_version_per_review"),
    )

    op.execute(
        """
        -- `0018`'s acceptance idiom verbatim: one root per review, one successor
        -- per node, therefore exactly one linear chain whose TAIL is the current
        -- snapshot. Naming it "one live snapshot" would be the mistake `0018`
        -- records having made.
        CREATE UNIQUE INDEX uq_snapshot_one_chain_per_review
          ON evidence_snapshots (review_id)
          WHERE supersedes_snapshot_id IS NULL;

        CREATE UNIQUE INDEX uq_snapshot_supersession_linear
          ON evidence_snapshots (supersedes_snapshot_id)
          WHERE supersedes_snapshot_id IS NOT NULL;

        -- One snapshot per job. `snapshots.request_snapshot` reads the job's
        -- snapshot back with `scalar_one_or_none()` on the idempotent path, so
        -- a second row for one job would not merely be wrong — it would raise
        -- `MultipleResultsFound` and make that review's snapshot route return
        -- 500 permanently, with no way to repair it through the API because
        -- the table is append-only.
        CREATE UNIQUE INDEX uq_snapshot_one_per_job
          ON evidence_snapshots (job_id);
        """
    )
    op.create_index("ix_evidence_snapshots_review", "evidence_snapshots", ["tenant_id", "review_id", "created_at"])
    # A consumer that pinned a hash asks "is this still the snapshot I bound?".
    # Without this index that question is a scan of every snapshot in the tenant.
    op.create_index("ix_evidence_snapshots_content_hash", "evidence_snapshots", ["content_hash"])

    op.create_table(
        "evidence_snapshot_members",
        sa.Column("member_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_snapshots.snapshot_id"),
            nullable=False,
        ),
        # The four fields of the released order key, in its order. Named
        # identically to `urn:datacompany:pinned-version-ref:1` rather than
        # translated: a member row and a manifest member must be recognisably
        # the same thing, or the digest computed over one cannot be checked
        # against the other.
        sa.Column("owner_context", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("member_schema_ref", sa.Text(), nullable=False),
        # The pinned target's own hash. `16-…:§3.5`: a nested target hash REMAINS
        # INCLUDED in the enclosing canonical view, which is what makes the
        # manifest sensitive to a member whose CONTENT changed and not only to
        # one that was added or removed.
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"owner_context ~ '{_OWNER_CONTEXT_SHAPE}'", name="ck_snapshot_member_owner_shape"),
        sa.CheckConstraint(f"entity_type IN ({entity_types})", name="ck_snapshot_member_entity_type"),
        sa.CheckConstraint("version_number >= 1", name="ck_snapshot_member_version_positive"),
        sa.CheckConstraint(f"content_hash ~ '{HASH_SHAPE}'", name="ck_snapshot_member_content_hash_shape"),
        # Set semantics as a database fact. `16-…:§3.5` requires a set-like
        # collection to reject duplicates before it is sorted and hashed; an
        # application that merely promises to is one bug away from a digest
        # nobody else can reproduce.
        sa.UniqueConstraint(
            "snapshot_id",
            "owner_context",
            "entity_type",
            "entity_id",
            name="uq_snapshot_member_identity",
        ),
    )
    op.create_index(
        "ix_snapshot_members_order",
        "evidence_snapshot_members",
        ["snapshot_id", "owner_context", "entity_type", "entity_id", "version_id"],
    )

    op.create_table(
        "snapshot_artifacts",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        # Over the RAW BYTES, never the canonical envelope. `16-…:§3.5`:
        # `content_hash` and `payload_digest` "MUST NOT be substituted for one
        # another", and this row describes bytes.
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        # `14-…:364` requires byte-identical reconstruction "under the same
        # serializer version". A row that does not say which serializer produced
        # it makes that criterion uncheckable.
        sa.Column("serializer_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"artifact_kind ~ '{_TOKEN_SHAPE}'", name="ck_snapshot_artifact_kind_shape"),
        sa.CheckConstraint(f"media_type ~ '{_MEDIA_TYPE_SHAPE}'", name="ck_snapshot_artifact_media_type_shape"),
        sa.CheckConstraint(f"payload_digest ~ '{HASH_SHAPE}'", name="ck_snapshot_artifact_digest_shape"),
        sa.CheckConstraint("byte_size >= 0", name="ck_snapshot_artifact_byte_size"),
        sa.UniqueConstraint("snapshot_id", "artifact_kind", name="uq_snapshot_artifact_kind"),
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(_tenant_rls(table))
        op.execute(_grant(table))

    # ⚠️ `0018`'s idiom is TWO PARTIAL UNIQUES PLUS A TRIGGER, and the first
    # draft here shipped only the uniques while the docstring claimed the idiom
    # "verbatim".
    #
    # The uniques give "one root per review" and "one successor per node". They
    # do NOT say the successor is in the same review: `supersedes_snapshot_id`
    # is a plain FK to `evidence_snapshots.snapshot_id`. A row in review R2
    # superseding R1's tail satisfies every constraint, leaves R2 with no root
    # at all, and `_entity_of` — which walks the chain without filtering by
    # review — then hands R2's chain R1's root as its `entity_id`. That is
    # precisely the identity collision `entity_id` exists to prevent, arriving
    # by the one route the uniques do not cover.
    #
    # No service path can do this today. Unenforced is not the same as
    # unreachable, and `0018` learned that about acceptances first.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION evidence_snapshots_supersede_within_one_review()
        RETURNS trigger AS $$
        DECLARE
          prior RECORD;
        BEGIN
          IF NEW.supersedes_snapshot_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT review_id, tenant_id INTO prior
            FROM evidence_snapshots WHERE snapshot_id = NEW.supersedes_snapshot_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'snapshot % supersedes % which does not exist',
              NEW.snapshot_id, NEW.supersedes_snapshot_id
              USING ERRCODE = 'foreign_key_violation';
          END IF;
          IF prior.review_id <> NEW.review_id OR prior.tenant_id <> NEW.tenant_id THEN
            RAISE EXCEPTION
              'snapshot % (review %) cannot supersede % (review %): a chain belongs to one review',
              NEW.snapshot_id, NEW.review_id, NEW.supersedes_snapshot_id, prior.review_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_evidence_snapshots_supersede_within_one_review
          BEFORE INSERT ON evidence_snapshots
          FOR EACH ROW EXECUTE FUNCTION evidence_snapshots_supersede_within_one_review();
        """
    )

    op.execute(
        _append_only(
            "evidence_snapshots",
            "a snapshot that can be edited after a run bound its hash is a snapshot that "
            "cannot answer what that run was allowed to read",
        )
    )
    op.execute(
        _append_only(
            "evidence_snapshot_members",
            "removing a member after the manifest digest was published would make the "
            "digest unverifiable rather than wrong, which is harder to notice",
        )
    )
    op.execute(
        _append_only(
            "snapshot_artifacts",
            "an artifact row is a claim about bytes that already exist",
        )
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_evidence_snapshots_supersede_within_one_review ON evidence_snapshots")
    op.execute("DROP FUNCTION IF EXISTS evidence_snapshots_supersede_within_one_review()")
    op.execute("DROP INDEX IF EXISTS uq_snapshot_one_per_job")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
    op.drop_table("snapshot_artifacts")
    op.drop_index("ix_snapshot_members_order", table_name="evidence_snapshot_members")
    op.drop_table("evidence_snapshot_members")
    op.drop_index("ix_evidence_snapshots_content_hash", table_name="evidence_snapshots")
    op.drop_index("ix_evidence_snapshots_review", table_name="evidence_snapshots")
    op.execute("DROP INDEX IF EXISTS uq_snapshot_supersession_linear")
    op.execute("DROP INDEX IF EXISTS uq_snapshot_one_chain_per_review")
    op.drop_table("evidence_snapshots")
    op.execute("DROP FUNCTION IF EXISTS er_text_array_is_a_set(text[])")
