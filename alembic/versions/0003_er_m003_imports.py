"""ER-M003 imports: batches, rows, record identity/versions, and the sealed corpus.

Revision ID: 0003_er_m003
Revises: 0002_er_m002
Create Date: 2026-08-08

## The sealed corpus, and why the seal is a trigger

`01-…:330-334` and `ER-V011` require every `CorpusVersion` to seal its membership,
`member_count`, the canonical member-ordering rule and `member_manifest_hash` **in
the same transaction**, and forbid any job or projection from masquerading as the
membership authority. A service that computes the hash and writes it alongside the
members satisfies that only as long as every writer remembers to. So the count and
the hash are recomputed by a DEFERRED constraint trigger at commit and the
transaction is refused on disagreement — which makes `DM-AT-05` a database
constraint rather than a test that a future code path can route around.

**Deviation, recorded deliberately.** The plan named
`research_contracts.canonical.engine.canonical_sha256` for
`member_manifest_hash`. A Postgres trigger cannot reproduce RFC 8785 JSON
canonicalization, so choosing that function would have meant giving up the
in-transaction recomputation — trading the strong guarantee for the prettier
provenance. The manifest is therefore defined as SHA-256 over the newline-joined
member `review_record_version_id`s in the declared ordering: expressible in SQL
and in Python, byte-identical in both, with `tests/integration/test_corpus_seal.py`
pinning the agreement. RFC 8785 hashing is still used where the plan actually
needs a schema-declared canonical hash — the **export** seal in ER-M005, via
`compute_content_hash(instance, schema_ref)`, which is a different artifact
answering a different question.

## Identifiers are attributes, never keys

`16-…:141-143`. DOI/PMID/PMCID/arXiv live on a NON-unique index. Pre-dedup
duplicates are the input to ER-003, not a constraint violation: a corpus that
cannot hold the same DOI twice has already deduplicated itself, silently and
without an attributable resolution.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_er_m003"
down_revision = "0002_er_m002"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"

#: The only ordering rule this release implements. Stored per corpus version
#: rather than assumed, because a manifest hash is meaningless without the
#: ordering it was taken over, and a later rule must be able to coexist with
#: versions sealed under this one.
ORDERING_RULE = "record_version_id_asc"

#: `record_type` is our discriminator. It is NOT `work_type`: that name is the
#: guarded activation discriminator of the unaccepted regulatory model, and
#: `ER-002.7` requires a body carrying it to be REJECTED rather than dropped.
RECORD_TYPES = ("journal_article", "preprint", "report", "dataset", "other")


def _tenant_rls(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = current_setting('{TENANT_GUC}', true))
          WITH CHECK (tenant_id = current_setting('{TENANT_GUC}', true));
    """


def _grant(table: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; DML grant on {table} skipped.';
          END IF;
        END
        $$;
    """


def _immutable_trigger(table: str, reason: str) -> str:
    fn = f"{table}_immutable"
    return f"""
        CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '{table} is immutable: {reason}'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_{fn}
          BEFORE UPDATE OR DELETE ON {table}
          FOR EACH ROW EXECUTE FUNCTION {fn}();
    """


def upgrade() -> None:
    # ---- import batches ---------------------------------------------------
    op.create_table(
        "import_batches",
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        # Discovery provenance, pinned WHEN PRESENT and never required.
        # discovery-engine `20b498e` supplies per_source_counts / raw_total /
        # deduped_total on a session export; a RIS file supplies none of that,
        # and blocking on it would make the common path depend on the rare one.
        # Identified counts are derived from our own `import_rows` regardless.
        sa.Column("source_ref", postgresql.JSONB(), nullable=True),
        # sha256 of the exact submitted bytes. This is what makes an import
        # replayable and an "it imported differently this time" claim decidable.
        sa.Column("raw_payload_digest", sa.Text(), nullable=False),
        sa.Column("submitted_by_subject_id", sa.Text(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "source_kind IN ('discovery_session','ris','bibtex','csv','canonical_json')",
            name="ck_import_batch_source_kind",
        ),
        sa.CheckConstraint("btrim(submitted_by_subject_id) <> ''", name="ck_import_batch_submitter_not_blank"),
    )
    op.execute(_tenant_rls("import_batches"))
    op.execute(_grant("import_batches"))

    # ---- import rows: the original representation, always retained --------
    op.create_table(
        "import_rows",
        sa.Column("import_row_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        # `ER-002.4` — the original representation is retained whatever happens
        # to the normalized record. A quarantined row keeps its bytes so the
        # quarantine is reviewable rather than a count.
        sa.Column("raw_representation", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.import_batch_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("import_batch_id", "row_index", name="uq_import_row_index"),
        sa.CheckConstraint("status IN ('accepted','rejected','quarantined')", name="ck_import_row_status"),
        # An accepted row produced a record; a rejected or quarantined one did
        # not, and must say why. Both halves, so neither "accepted with no
        # record" nor "quarantined for no stated reason" can be written.
        sa.CheckConstraint(
            "(status = 'accepted' AND review_record_id IS NOT NULL AND quarantine_reason IS NULL) "
            "OR (status <> 'accepted' AND review_record_id IS NULL AND btrim(coalesce(quarantine_reason,'')) <> '')",
            name="ck_import_row_outcome_consistent",
        ),
    )
    op.execute(_tenant_rls("import_rows"))
    op.execute(_grant("import_rows"))

    # ---- review records: stable identity ---------------------------------
    op.create_table(
        "review_records",
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
    )
    op.execute(_tenant_rls("review_records"))
    op.execute(_grant("review_records"))
    # `01-…:103` — never delete an original during deduplication. The identity
    # row is the thing a survivorship field ultimately resolves to, so it is
    # immutable outright: corrections create a new VERSION, not an edit here.
    op.execute(_immutable_trigger("review_records", "corrections create a new review_record_version"))

    # ---- review record versions: immutable content -----------------------
    op.create_table(
        "review_record_versions",
        sa.Column("review_record_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("schema_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("publication_year", sa.Integer(), nullable=True),
        sa.Column("authors", postgresql.JSONB(), nullable=True),
        sa.Column("container_title", sa.Text(), nullable=True),
        sa.Column("doi", sa.Text(), nullable=True),
        sa.Column("pmid", sa.Text(), nullable=True),
        sa.Column("pmcid", sa.Text(), nullable=True),
        sa.Column("arxiv_id", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        # NFKC + casefold + punctuation-strip + whitespace-collapse of `title`.
        # Persisted rather than computed on read so the dedup baseline is a
        # deterministic function of stored bytes (`ER-003.1` input digest).
        sa.Column("normalized_title", sa.Text(), nullable=True),
        sa.Column("normalized_doi", sa.Text(), nullable=True),
        sa.Column("normalized_pmid", sa.Text(), nullable=True),
        # `ER-002.4` — which import row and which source field each normalized
        # value came from.
        sa.Column("field_provenance", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_import_row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"], ["review_record_versions.review_record_version_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_import_row_id"], ["import_rows.import_row_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("review_record_id", "version_number", name="uq_record_version_number"),
        sa.CheckConstraint(
            "record_type IN (" + ", ".join(f"'{t}'" for t in RECORD_TYPES) + ")",
            name="ck_record_version_record_type",
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_record_version_number_positive"),
        # A version chain is linear: at most one successor per version.
        sa.UniqueConstraint("supersedes_version_id", name="uq_record_version_supersession_linear"),
    )
    op.execute(_tenant_rls("review_record_versions"))
    op.execute(_grant("review_record_versions"))
    op.execute(_immutable_trigger("review_record_versions", "a correction is a new version, never an edit"))

    # NON-unique, deliberately: pre-dedup duplicates are the input to ER-003.
    op.create_index("ix_record_versions_doi", "review_record_versions", ["review_id", "normalized_doi"])
    op.create_index("ix_record_versions_pmid", "review_record_versions", ["review_id", "normalized_pmid"])
    op.create_index(
        "ix_record_versions_title_year",
        "review_record_versions",
        ["review_id", "normalized_title", "publication_year"],
    )
    op.create_index("ix_record_versions_record", "review_record_versions", ["review_record_id", "version_number"])

    # ---- corpus versions: sealed --------------------------------------------
    op.create_table(
        "corpus_versions",
        sa.Column("corpus_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("member_ordering_rule", sa.Text(), nullable=False),
        sa.Column("member_manifest_hash", sa.Text(), nullable=False),
        sa.Column("created_by_subject_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["corpus_versions.corpus_version_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("review_id", "version_number", name="uq_corpus_version_number"),
        sa.UniqueConstraint("supersedes_version_id", name="uq_corpus_version_supersession_linear"),
        sa.CheckConstraint("member_count >= 0", name="ck_corpus_member_count_non_negative"),
        sa.CheckConstraint(
            f"member_ordering_rule = '{ORDERING_RULE}'",
            name="ck_corpus_ordering_rule_known",
        ),
        sa.CheckConstraint(
            "member_manifest_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_corpus_manifest_hash_shape",
        ),
    )
    op.execute(_tenant_rls("corpus_versions"))
    op.execute(_grant("corpus_versions"))
    op.execute(
        _immutable_trigger("corpus_versions", "any add, remove or replace of a member creates a NEW corpus version")
    )

    op.create_table(
        "corpus_version_members",
        sa.Column("corpus_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("review_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_record_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("corpus_version_id", "review_record_id", name="pk_corpus_version_members"),
        sa.ForeignKeyConstraint(["corpus_version_id"], ["corpus_versions.corpus_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.review_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_record_id"], ["review_records.review_record_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["review_record_version_id"],
            ["review_record_versions.review_record_version_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("corpus_version_id", "member_ordinal", name="uq_corpus_member_ordinal"),
        sa.CheckConstraint("member_ordinal >= 1", name="ck_corpus_member_ordinal_positive"),
    )
    op.execute(_tenant_rls("corpus_version_members"))
    op.execute(_grant("corpus_version_members"))
    op.execute(_immutable_trigger("corpus_version_members", "membership is sealed at the version it belongs to"))

    # ---- THE SEAL ---------------------------------------------------------
    # A DEFERRED constraint trigger, so it fires once at COMMIT with the whole
    # membership visible. A per-row trigger cannot do this job: the first member
    # inserted would always disagree with a count of N.
    #
    # It re-derives three things and refuses the transaction on any disagreement:
    #   * the count,
    #   * the ordinals — contiguous 1..n AND in the declared order, because a
    #     hash over a permuted-but-complete set would otherwise still match if
    #     the ordinals were merely relabelled,
    #   * the manifest hash.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION corpus_versions_verify_seal() RETURNS trigger AS $$
        DECLARE
          cv record;
          actual_count int;
          actual_hash text;
          ordering_ok boolean;
        BEGIN
          SELECT * INTO cv FROM corpus_versions WHERE corpus_version_id = NEW.corpus_version_id;
          IF NOT FOUND THEN
            -- The version row was rolled back; nothing to verify.
            RETURN NULL;
          END IF;

          SELECT count(*) INTO actual_count
            FROM corpus_version_members m
           WHERE m.corpus_version_id = cv.corpus_version_id;

          IF actual_count <> cv.member_count THEN
            RAISE EXCEPTION 'corpus % sealed member_count % but holds % members',
              cv.corpus_version_id, cv.member_count, actual_count
              USING ERRCODE = 'restrict_violation';
          END IF;

          -- Ordinals must be exactly 1..n in the declared order. `record_version_id_asc`
          -- means ascending lexicographic member version id.
          SELECT bool_and(ok) INTO ordering_ok FROM (
            SELECT m.member_ordinal = row_number() OVER (
                     ORDER BY m.review_record_version_id::text ASC
                   ) AS ok
              FROM corpus_version_members m
             WHERE m.corpus_version_id = cv.corpus_version_id
          ) t;

          IF actual_count > 0 AND NOT coalesce(ordering_ok, false) THEN
            RAISE EXCEPTION 'corpus % member ordinals do not follow ordering rule %',
              cv.corpus_version_id, cv.member_ordering_rule
              USING ERRCODE = 'restrict_violation';
          END IF;

          SELECT 'sha256:' || encode(
                   sha256(convert_to(coalesce(string_agg(m.review_record_version_id::text, E'\\n'
                                              ORDER BY m.review_record_version_id::text ASC), ''), 'UTF8')),
                   'hex')
            INTO actual_hash
            FROM corpus_version_members m
           WHERE m.corpus_version_id = cv.corpus_version_id;

          IF actual_hash IS DISTINCT FROM cv.member_manifest_hash THEN
            RAISE EXCEPTION 'corpus % sealed manifest % but its members hash to %',
              cv.corpus_version_id, cv.member_manifest_hash, actual_hash
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_corpus_members_verify_seal
          AFTER INSERT ON corpus_version_members
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION corpus_versions_verify_seal();
        """
    )

    # A corpus version with a count but no member rows would never fire the
    # AFTER INSERT trigger above, so an empty membership claiming N members
    # would commit. This closes that hole from the other side.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION corpus_versions_verify_seal_on_version() RETURNS trigger AS $$
        DECLARE
          actual_count int;
          actual_hash text;
        BEGIN
          SELECT count(*) INTO actual_count
            FROM corpus_version_members m WHERE m.corpus_version_id = NEW.corpus_version_id;
          IF actual_count <> NEW.member_count THEN
            RAISE EXCEPTION 'corpus % sealed member_count % but holds % members',
              NEW.corpus_version_id, NEW.member_count, actual_count
              USING ERRCODE = 'restrict_violation';
          END IF;
          SELECT 'sha256:' || encode(
                   sha256(convert_to(coalesce(string_agg(m.review_record_version_id::text, E'\\n'
                                              ORDER BY m.review_record_version_id::text ASC), ''), 'UTF8')),
                   'hex')
            INTO actual_hash
            FROM corpus_version_members m WHERE m.corpus_version_id = NEW.corpus_version_id;
          IF actual_hash IS DISTINCT FROM NEW.member_manifest_hash THEN
            RAISE EXCEPTION 'corpus % sealed manifest % but its members hash to %',
              NEW.corpus_version_id, NEW.member_manifest_hash, actual_hash
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_corpus_versions_verify_seal
          AFTER INSERT ON corpus_versions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION corpus_versions_verify_seal_on_version();
        """
    )

    # Cross-corpus membership: a member row must belong to the same review as
    # its corpus version and its record. The FKs alone permit a member of
    # review A's record inside review B's corpus.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION corpus_version_members_same_review() RETURNS trigger AS $$
        DECLARE
          cv_review uuid;
          rec_review uuid;
          ver_record uuid;
        BEGIN
          SELECT review_id INTO cv_review FROM corpus_versions WHERE corpus_version_id = NEW.corpus_version_id;
          SELECT review_id INTO rec_review FROM review_records WHERE review_record_id = NEW.review_record_id;
          SELECT review_record_id INTO ver_record
            FROM review_record_versions WHERE review_record_version_id = NEW.review_record_version_id;

          IF cv_review IS DISTINCT FROM NEW.review_id OR rec_review IS DISTINCT FROM NEW.review_id THEN
            RAISE EXCEPTION 'corpus member crosses reviews: corpus %, record %, member declared %',
              cv_review, rec_review, NEW.review_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          IF ver_record IS DISTINCT FROM NEW.review_record_id THEN
            RAISE EXCEPTION 'member version % does not belong to record %',
              NEW.review_record_version_id, NEW.review_record_id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_corpus_version_members_same_review
          BEFORE INSERT ON corpus_version_members
          FOR EACH ROW EXECUTE FUNCTION corpus_version_members_same_review();
        """
    )

    op.create_index("ix_import_rows_batch", "import_rows", ["import_batch_id", "row_index"])
    op.create_index("ix_import_rows_review_status", "import_rows", ["review_id", "status"])
    op.create_index("ix_corpus_members_record", "corpus_version_members", ["review_id", "review_record_id"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_corpus_version_members_same_review ON corpus_version_members")
    op.execute("DROP FUNCTION IF EXISTS corpus_version_members_same_review()")
    op.execute("DROP TRIGGER IF EXISTS trg_corpus_versions_verify_seal ON corpus_versions")
    op.execute("DROP FUNCTION IF EXISTS corpus_versions_verify_seal_on_version()")
    op.execute("DROP TRIGGER IF EXISTS trg_corpus_members_verify_seal ON corpus_version_members")
    op.execute("DROP FUNCTION IF EXISTS corpus_versions_verify_seal()")
    for table in (
        "corpus_version_members",
        "corpus_versions",
        "review_record_versions",
        "review_records",
        "import_rows",
        "import_batches",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_immutable()")
    op.drop_table("corpus_version_members")
    op.drop_table("corpus_versions")
    op.drop_table("review_record_versions")
    op.drop_table("review_records")
    op.drop_table("import_rows")
    op.drop_table("import_batches")
