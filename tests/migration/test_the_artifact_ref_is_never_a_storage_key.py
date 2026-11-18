"""`artifact_records` — the artifact ref has the PORT's shape, proven both ways.

## ⚠️ What `0025` claimed, and what it actually did

This file was written for `0025`, whose two `^[^/]+$` CHECKs were named
`…_not_storage_key` and whose docstring said they refused "`s3://…`,
`bucket/path`, an opaque blob". Measured, they refused only values containing a
SLASH. `test_the_new_checks_refuse_every_value_0025_let_through` below carries
the five values that INSERTed successfully under `0025` — a colon-delimited
`s3:bucket:key`, a bare bucket name, a backslash-separated key, a
percent-encoded `s3://…` URI and a long opaque base64 blob — as live INSERTs, so
the gap is a measurement in this suite and not a claim in a docstring.

`0026` replaced both with POSITIVE format constraints taken from
`src/adapters/artifact_store.py:ArtifactRef` (`artifact_id: uuid.UUID`,
`version: int`): `ck_artifact_records_artifact_id_is_a_uuid` and
`ck_artifact_records_artifact_version_is_a_positive_integer`. The file name is
kept because "never a storage key" is still the REASON — it is simply now a
consequence of admitting exactly two formats rather than a rule of its own.

## What this proves and why it needs a scratch database

`13-…:376`: "Artifact Service ID/version/hash, classification, rights snapshot,
semantic role and owning aggregate; never a raw storage key". A rule enforced
only in prose is the defect this table exists to prevent — see the migration's
own module docstring — so this file inserts the EXACT negative fixture
`research_contracts` ships
(`fixtures/foundation/v1/invalid/typed-ref-artifact-carrying-a-storage-key.json`)
and asserts the database refuses it, then inserts the shape of the matching
POSITIVE fixture (`fixtures/foundation/v1/valid/typed-ref-artifact.json`) and
asserts the database accepts it. A CHECK never seen to accept proves nothing —
`0021`'s own posture — so both directions are asserted, not just the refusal.

`artifact_records` is append-only (`0025`'s `trg_artifact_records_append_only`):
a row inserted here cannot be deleted, only superseded by a fresh table via a
downgrade. So the acceptance test runs against the `scratch_database` fixture
`tests/migration/test_migration_round_trip.py` already built for exactly this
problem — `downgrade base` is destructive, and this file's insert is
irreversible in-place — rather than against the shared database the rest of the
suite depends on staying clean.

## The round trip

`downgrade` must not merely run without raising; the table, both its indexes and
its RLS policy must be gone, and `upgrade head` must bring back the same claimed
columns. Asserting only "the alembic command exited zero" would pass on a
downgrade that silently no-ops, which is the same failure mode
`test_downgrade_to_base_and_back_reproduces_the_schema` polices generally — this
file checks it narrowly, for the one table this task added.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.migration.test_migration_round_trip import _alembic, scratch_database  # noqa: F401 - fixture re-export

OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not OWNER_URL.startswith("postgresql"),
        reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
    ),
]

TABLE = "artifact_records"

#: Every column `0025` claims for `13-…:376` (Artifact Service ID/version/hash,
#: classification, rights snapshot, semantic role, owning aggregate),
#: `13-…:754-757` (the rights-snapshot fields) and `13-…:795-798` (the
#: residency-policy reference). Read back from `information_schema`, not
#: assumed to still match the migration.
CLAIMED_COLUMNS: frozenset[str] = frozenset(
    {
        "artifact_record_id",
        "tenant_id",
        "owning_aggregate_type",
        "owning_aggregate_id",
        "artifact_id",
        "artifact_version",
        "artifact_content_hash",
        "classification",
        "semantic_role",
        "rights_source_id",
        "rights_document_id",
        "rights_document_version_id",
        "rights_decision_id",
        "rights_evaluated_purpose",
        "rights_decision_version",
        "rights_decision_hash",
        "rights_evaluated_at",
        "rights_expiry_at",
        "rights_territory_constraint",
        "rights_checks_evaluated",
        "residency_policy_ref",
        "created_by_subject_id",
        "created_at",
    }
)

#: A fully-populated legitimate row, shaped like `research_contracts`'
#: `fixtures/foundation/v1/valid/typed-ref-artifact.json` for the artifact
#: identity fields (`entity_id`/`version_id`/`content_hash`), plus this table's
#: own required rights and residency columns.
_VALID_ARTIFACT_ID = "44444444-4444-4444-8444-444444444444"
#: ⚠️ Was a UUID until `0026`. `ArtifactRef.version` is an `int` and Artifact
#: Service numbers versions from 1, so the version column's legitimate value is
#: `'1'`, not a second identifier. `0025` accepted the UUID only because
#: `^[^/]+$` accepted almost everything.
_VALID_ARTIFACT_VERSION = "1"
_VALID_CONTENT_HASH = "sha256:" + "4" * 64

#: The exact negative fixture: `research_contracts`
#: `fixtures/foundation/v1/invalid/typed-ref-artifact-carrying-a-storage-key.json`
#: smuggles this string in beside a pin. Byte-identical here.
_STORAGE_KEY = "s3://artifact-store/runs/2026/07/output.parquet?expires=3600"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "artifact_record_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "owning_aggregate_type": "research_object_version",
        "owning_aggregate_id": str(uuid.uuid4()),
        "artifact_id": _VALID_ARTIFACT_ID,
        "artifact_version": _VALID_ARTIFACT_VERSION,
        "artifact_content_hash": _VALID_CONTENT_HASH,
        "classification": "restricted",
        "semantic_role": "primary_output",
        "rights_source_id": "src-1",
        "rights_document_id": "doc-1",
        "rights_document_version_id": "docver-1",
        "rights_decision_id": "rd-1",
        "rights_evaluated_purpose": "display",
        "rights_decision_version": "v1",
        "rights_decision_hash": "sha256:" + "5" * 64,
        "rights_evaluated_at": datetime.now(tz=UTC),
        "residency_policy_ref": "urn:datacompany:residency-policy:eu-west",
        "created_by_subject_id": "subject-1",
    }
    row.update(overrides)
    return row


def _set_tenant(conn, tenant_id: object) -> None:  # noqa: ANN001
    """Bind `app.current_tenant` for the current transaction.

    Needed on EVERY connection that touches `artifact_records`, reads included —
    see `_insert` for why. On the read path the failure looks different and
    worse: `current_setting('app.current_tenant')` returns `''` when unset, so
    the policy's `::uuid` cast raises `InvalidTextRepresentation` rather than a
    permission error, which reads like malformed test data rather than a missing
    GUC.
    """
    conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": tenant_id})


def _insert(conn, **overrides: object) -> dict[str, object]:  # noqa: ANN001
    """INSERT one row, returning it so callers can bind the same tenant to reads."""
    row = _row(**overrides)
    # ⛔ Do not delete this `set_config` as redundant — it is the line that lets
    # every INSERT below reach the CHECK constraints this file is about.
    #
    # `0025`'s `_tenant_rls()` puts FORCE ROW LEVEL SECURITY on this table, and
    # FORCE binds the table OWNER too: only a SUPERUSER or a BYPASSRLS role
    # escapes the policy. The policy is `tenant_id = current_setting(
    # 'app.current_tenant')::uuid`, so with the GUC unset the WITH CHECK is NULL
    # and Postgres raises SQLSTATE 42501 — "new row violates row-level security
    # policy for table artifact_records", surfacing as
    # `sqlalchemy.exc.ProgrammingError` — BEFORE any `ck_artifact_records_*`
    # CHECK is evaluated. `pytest.raises(IntegrityError, ...)` does not catch
    # that, and the acceptance inserts do not survive it either.
    #
    # This stayed invisible because CI's `ci_test` role is created from
    # `POSTGRES_USER` on the official `postgres:18` image and is therefore a
    # cluster superuser (measured: `rolsuper=t`, `rolbypassrls=t`), which
    # bypasses RLS outright. Measured against a plain non-superuser owner, all
    # sixteen insert-based tests in this file fail without this line and pass
    # with it. `is_local => true` (transaction-scoped) matches every sibling RLS
    # test, e.g. `tests/security/test_blinding_layers.py`.
    _set_tenant(conn, row["tenant_id"])
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    conn.execute(text(f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders})"), row)  # nosec B608 - fixed columns
    return row


def test_the_table_carries_every_column_13_dash_376_754_757_and_795_798_claim() -> None:
    engine = create_engine(OWNER_URL, future=True)
    try:
        with engine.connect() as conn:
            found = set(
                conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": TABLE},
                ).scalars()
            )
    finally:
        engine.dispose()
    missing = CLAIMED_COLUMNS - found
    assert missing == set(), f"{TABLE} is missing {sorted(missing)} — measured columns: {sorted(found)}"


def test_force_row_level_security_is_on() -> None:
    engine = create_engine(OWNER_URL, future=True)
    try:
        with engine.connect() as conn:
            forced = conn.execute(
                text(
                    "SELECT relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = :t"
                ),
                {"t": TABLE},
            ).scalar_one()
    finally:
        engine.dispose()
    assert forced is True, f"{TABLE} does not carry FORCE ROW LEVEL SECURITY"


def test_the_check_refuses_the_exact_negative_fixtures_storage_key() -> None:
    """`typed-ref-artifact-carrying-a-storage-key.json`, byte-identical, refused.

    Runs in its own transaction against the shared database: Postgres aborts a
    transaction on a constraint violation, so nothing commits and there is no
    row left for the append-only trigger to have to refuse cleaning up.
    """
    engine = create_engine(OWNER_URL, future=True)
    try:
        with pytest.raises(IntegrityError, match="ck_artifact_records_artifact_id_is_a_uuid"):
            with engine.begin() as conn:
                _insert(conn, artifact_id=_STORAGE_KEY)
    finally:
        engine.dispose()


def test_the_check_also_refuses_a_storage_key_shaped_artifact_version() -> None:
    """The sibling CHECK on `artifact_version` — the other column, its own shape.

    Not "the same shape" any more: since `0026` the version column asserts a
    positive decimal integer, so a `bucket/path` fails it for not being an
    integer rather than for carrying a slash.
    """
    engine = create_engine(OWNER_URL, future=True)
    try:
        with pytest.raises(IntegrityError, match="ck_artifact_records_artifact_version_is_a_positive_integer"):
            with engine.begin() as conn:
                _insert(conn, artifact_version="bucket/path/to/version")
    finally:
        engine.dispose()


def test_the_check_accepts_the_matching_positive_fixtures_shape(scratch_database: str) -> None:  # noqa: F811
    """`typed-ref-artifact.json`'s `entity_id`/`content_hash` shape, accepted.

    A CHECK that has only ever been exercised on its refusal path proves nothing
    about its acceptance path — the same posture the module docstring takes.
    Run against the scratch database because the row this inserts is committed
    and `artifact_records` is append-only: there is no way to remove it from the
    shared database afterward, only to downgrade a database nothing else needs.
    """
    _alembic("upgrade", "head", scratch_database)
    engine = create_engine(scratch_database, future=True)
    try:
        marker = str(uuid.uuid4())
        with engine.begin() as conn:
            row = _insert(conn, artifact_record_id=marker)
        with engine.connect() as conn, conn.begin():
            # The read is subject to the same FORCE RLS policy as the write, and
            # this is a FRESH connection, so the transaction-scoped GUC set
            # during the INSERT is long gone. Bind the tenant the row was
            # written under, or the policy's `::uuid` cast of an empty
            # `current_setting` raises before any row is considered.
            _set_tenant(conn, row["tenant_id"])
            found = conn.execute(
                text(f"SELECT 1 FROM {TABLE} WHERE artifact_record_id = :id"),  # nosec B608 - fixed table
                {"id": marker},
            ).scalar()
    finally:
        engine.dispose()
    assert found == 1, "a legitimate artifact_ref-shaped row was refused"


def test_downgrade_removes_the_table_and_upgrade_restores_every_column(scratch_database: str) -> None:  # noqa: F811
    """`0025 downgrade` then `0025 upgrade` round-trips this table specifically.

    Narrower than `test_downgrade_to_base_and_back_reproduces_the_schema`, which
    proves the whole-schema counts agree — this asserts the one thing a silent
    no-op downgrade would still pass: that the table, its policy and its indexes
    are actually GONE before `upgrade head` is asked to recreate them.
    """
    _alembic("upgrade", "head", scratch_database)
    engine = create_engine(scratch_database, future=True)
    try:
        with engine.connect() as conn:
            before = set(
                conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": TABLE},
                ).scalars()
            )
        assert CLAIMED_COLUMNS <= before

        _alembic("downgrade", "0024_er_m024_log_catalogue", scratch_database)
        with engine.connect() as conn:
            gone = conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
                {"t": TABLE},
            ).scalar_one()
            policy_gone = conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t"),
                {"t": TABLE},
            ).scalar_one()
        assert gone == 0, f"{TABLE} still exists after downgrade"
        assert policy_gone == 0, f"a policy on {TABLE} survived its own table's downgrade"

        _alembic("upgrade", "head", scratch_database)
        with engine.connect() as conn:
            after = set(
                conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": TABLE},
                ).scalars()
            )
        assert after == before, f"round trip changed the column set: {before.symmetric_difference(after)}"
    finally:
        engine.dispose()


#: Every value that INSERTed successfully under `0025`'s `^[^/]+$`, measured on
#: a scratch database at `0025` head before `0026` was written. None contains a
#: slash; none is an artifact ID. They are kept as data rather than prose so
#: that a future widening of the CHECK fails here instead of being argued about.
#:
#: `bucket\\path\\file` is the Windows-separator spelling of the same key
#: `_STORAGE_KEY` carries; the percent-encoded entry is `_STORAGE_KEY`'s scheme
#: and host after a single round of URL encoding, which is how a locator most
#: plausibly arrives through an HTTP layer that escaped it.
_ACCEPTED_BY_0025: tuple[tuple[str, str], ...] = (
    ("an s3 locator with colon separators", "s3:bucket:key"),
    ("a bare bucket name", "artifact-store"),
    ("a backslash-separated storage key", "bucket\\path\\file"),
    ("a percent-encoded s3 URI", "s3%3A%2F%2Fartifact-store%2Fx"),
    # ⚠️ DERIVED, not written as a literal. A 168-character base64 constant in
    # the source is indistinguishable from a leaked credential to
    # detect-secrets, which flagged the hard-coded version of this line as a
    # "Base64 High Entropy String" and failed CI. Baselining it would have
    # buried a real-looking secret behind a hash a reviewer cannot interpret;
    # computing it keeps the property under test — a long opaque blob that is
    # not a UUID — with nothing secret-shaped in the file.
    ("a long opaque base64 blob", base64.b64encode(b"an opaque storage locator " * 8).decode()),
)


@pytest.mark.parametrize(("description", "value"), _ACCEPTED_BY_0025, ids=[d for d, _ in _ACCEPTED_BY_0025])
def test_the_new_checks_refuse_every_value_0025_let_through(description: str, value: str) -> None:
    """The five measured `0025` bypasses, refused by name.

    Each runs in its own transaction against the shared database: a constraint
    violation aborts the transaction, so nothing commits and the append-only
    trigger is never asked to clean up a row.

    The `match=` is the CONSTRAINT NAME, not a substring of the value: a test
    that only asserted `IntegrityError` would pass if some unrelated NOT NULL
    or the append-only trigger refused the row for a different reason.
    """
    engine = create_engine(OWNER_URL, future=True)
    try:
        with pytest.raises(IntegrityError, match="ck_artifact_records_artifact_id_is_a_uuid"):
            with engine.begin() as conn:
                _insert(conn, artifact_id=value)
    finally:
        engine.dispose()


#: The other column, same posture. `ArtifactRef.version` is an `int`; these are
#: the spellings that are not one, plus the two that are the same `int` twice.
_REFUSED_VERSIONS: tuple[tuple[str, str], ...] = (
    ("a storage-key-shaped version", "bucket/path/to/version"),
    ("a uuid, which 0025 accepted here", "55555555-5555-4555-8555-555555555555"),
    ("a leading zero, a second spelling of 1", "01"),
    ("zero, which Artifact Service never issues", "0"),
    ("a negative version", "-1"),
    ("a thousands separator", "1,000"),
    ("a semver-ish version", "1.0"),
)


@pytest.mark.parametrize(("description", "value"), _REFUSED_VERSIONS, ids=[d for d, _ in _REFUSED_VERSIONS])
def test_the_version_check_admits_only_a_positive_decimal_integer(description: str, value: str) -> None:
    engine = create_engine(OWNER_URL, future=True)
    try:
        with pytest.raises(IntegrityError, match="ck_artifact_records_artifact_version_is_a_positive_integer"):
            with engine.begin() as conn:
                _insert(conn, artifact_version=value)
    finally:
        engine.dispose()


def test_the_uuid_check_pins_one_spelling_of_one_identifier() -> None:
    """Uppercase hex and the unhyphenated form are refused.

    `uuid.UUID` parses both, so this is the one place the CHECK is deliberately
    NARROWER than the port's constructor. The reason is
    `ix_artifact_records_artifact` and the absence of any normalising writer:
    two spellings of one artifact are two index entries and two rows that no
    equality join will bring back together. `0026`'s docstring records this as
    the single intentional narrowing; everything else about the CHECK is exactly
    `uuid.UUID`'s own permissiveness, including nil and non-RFC-4122 values.
    """
    engine = create_engine(OWNER_URL, future=True)
    try:
        for spelling in ("44444444-4444-4444-8444-44444444444A", "44444444444444448444444444444444"):
            with pytest.raises(IntegrityError, match="ck_artifact_records_artifact_id_is_a_uuid"):
                with engine.begin() as conn:
                    _insert(conn, artifact_id=spelling)
    finally:
        engine.dispose()


def test_the_superseded_0025_constraints_are_gone_by_name() -> None:
    """`0026` DROPped both, it did not merely add beside them.

    Two CHECKs on one column both hold, so leaving `^[^/]+$` in place would be
    harmless at runtime and invisible to every INSERT-based test above — and
    would leave a constraint whose NAME still asserts a property the estate no
    longer relies on. Read from `pg_constraint`, so this measures the live
    database rather than the migration's source.
    """
    engine = create_engine(OWNER_URL, future=True)
    try:
        with engine.connect() as conn:
            names = set(
                conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t) AND contype = 'c'"),
                    {"t": TABLE},
                ).scalars()
            )
    finally:
        engine.dispose()
    superseded = {
        "ck_artifact_records_artifact_id_not_storage_key",
        "ck_artifact_records_artifact_version_not_storage_key",
    }
    enforced = {
        "ck_artifact_records_artifact_id_is_a_uuid",
        "ck_artifact_records_artifact_version_is_a_positive_integer",
    }
    assert names & superseded == set(), f"`0025`'s superseded CHECKs survive: {sorted(names & superseded)}"
    assert enforced <= names, f"`0026`'s CHECKs are missing: {sorted(enforced - names)}"


def test_0026_round_trips_and_restores_0025s_constraint_names(scratch_database: str) -> None:  # noqa: F811
    """`downgrade -1` puts `0025`'s two names back; `upgrade head` re-inverts them.

    A downgrade that dropped the new CHECKs without restoring the old ones would
    still leave the schema loadable and would still let `upgrade head` succeed —
    it would just silently un-guard the two columns at `0025`, which is the
    revision the estate would be sitting on during a rollback. So the names are
    asserted at BOTH ends, not only after the return trip.
    """
    _alembic("upgrade", "head", scratch_database)
    engine = create_engine(scratch_database, future=True)

    def _checks() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t) AND contype = 'c'"),
                    {"t": TABLE},
                ).scalars()
            )

    try:
        at_head = _checks()
        assert "ck_artifact_records_artifact_id_is_a_uuid" in at_head
        assert "ck_artifact_records_artifact_version_is_a_positive_integer" in at_head

        _alembic("downgrade", "0025_er_m025_artifact_records", scratch_database)
        at_0025 = _checks()
        assert "ck_artifact_records_artifact_id_not_storage_key" in at_0025, (
            "downgrade dropped `0026`'s CHECK without restoring `0025`'s — the column is now unguarded"
        )
        assert "ck_artifact_records_artifact_version_not_storage_key" in at_0025
        assert "ck_artifact_records_artifact_id_is_a_uuid" not in at_0025

        _alembic("upgrade", "head", scratch_database)
        assert _checks() == at_head, "the round trip did not reproduce the constraint set"
    finally:
        engine.dispose()
