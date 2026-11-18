"""The runtime role's live privileges must match what `alembic upgrade head` actually revokes.

## The gap this closes

`tests/conftest.py`'s blanket ``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL
TABLES`` runs AFTER `alembic upgrade head`, so it silently restores whatever any
migration revoked — unless the harness re-asserts that specific REVOKE itself.
`_privileges_revoked_by_0015` is that re-assertion, and it is good at its job:
mutating it to return ``{}`` turns 7 tests red, all `23001` instead of `42501`
(the trigger catching what the grant should have refused first).

It is also **pinned to one filename**:
``alembic/versions/0015_er_m015_the_ledger_inputs_are_append_only.py``. A future
migration — `0016`, say — that adds its own ``REVOKE ... FROM
evidence_review_app`` on a new or existing table is invisible to it. The blanket
grant would restore that privilege in every test environment, silently, with
every test in this file and in `test_the_ledger_inputs_are_append_only.py`
staying green, because none of them know the new table exists.

## Why this is not a second hand-maintained list

The obvious fix — regex the literal text ``REVOKE ... ON <table> FROM
evidence_review_app`` out of `alembic/versions/*.py` — does not reach `0015`
itself. Its REVOKEs are built from Python data (`FULLY_GUARDED`,
`NEWLY_GUARDED_FULL`, `DELETE_ONLY`) inside a list comprehension, so the literal
source text contains ``REVOKE {privileges} ON {table} FROM {APP_ROLE}`` with no
table name in it anywhere — there is nothing for a regex to find. Reading those
three module attributes by name would work for `0015` specifically, which is
exactly the filename-pinning this file exists to stop being true of the harness.

So this asks Postgres instead of the source text. `reference_revocations` runs
`alembic upgrade head` for real, against a scratch database that starts with the
schema empty — the SAME code path production and CI use, executed rather than
parsed — and reads back `information_schema.role_table_grants` for
`evidence_review_app`. Whatever a migration revoked is simply absent there,
regardless of whether the REVOKE that removed it was a literal string (`0011`)
or built from a loop over a list computed at import time (`0015`). A `0016` that
revokes on a new table is covered the moment it lands, because this test does
not know or care which migration file did it.

`test_live_grants_agree_with_what_the_migrations_revoke` then compares that
reference against the actual `DATABASE_URL` database's grants for the same
role. A mismatch in either direction is reported: a privilege the reference
revokes but the live role still holds (the conftest-restores-it defect this
item is about), or a privilege the live role lacks that the reference grants
(the harness enforcing a REVOKE the migrations no longer make, e.g. after a
`0013`-style re-grant that some hand-written re-assertion did not learn about).

## What this does NOT replace

`_privileges_revoked_by_0015` in `tests/conftest.py` stays. Something still has
to perform the re-assertion after the blanket grant — this file only checks
that whatever performs it got the whole set right, for every table any
migration ever touches, not only the ones somebody remembered to list.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed argv, no shell, no caller input
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

REPO = Path(__file__).resolve().parents[2]

APP_ROLE = "evidence_review_app"

#: Migrations run as the OWNER. `DATABASE_URL` may already be the non-owner
#: runtime role (`tests/conftest.py` repoints it there once it has provisioned
#: the role) — `MIGRATION_DATABASE_URL` is where the owner URL then lives.
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

#: The connection this file reads the LIVE grants from. Deliberately the owner
#: URL and not `DATABASE_URL`: `information_schema.role_table_grants` shows a
#: grant to anyone who is the grantor, the grantee, or holds admin on the
#: grantee role, and the owner granted every privilege in play here, so it can
#: always see them — including when `DATABASE_URL` was left unswapped because
#: the connected role could not create `evidence_review_app` (the locked-down
#: case `tests/conftest.py` documents). Reading through the owner means this
#: file reports the same verdict either way, instead of also depending on
#: whether provisioning happened to succeed in this run.
LIVE_URL = OWNER_URL

pytestmark = [
    pytest.mark.security,
    pytest.mark.slow,
    pytest.mark.skipif(
        not OWNER_URL.startswith("postgresql"),
        reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
    ),
]

ALL_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

#: A reference run below this many revoked tables did not measure the real
#: migration set — it measured an empty or half-applied schema, which agrees
#: with anything. At `0015`, 15 tables take the full `REVOKE UPDATE, DELETE`,
#: 3 more take `REVOKE DELETE` only, and `review_stages` takes `REVOKE UPDATE`
#: alone (its `DELETE` was re-granted by `0013`) — 19 tables carry at least one
#: revocation. Floored well under that so a deliberately relaxed future
#: privilege does not make this file need editing, and high enough that a
#: reference computed from a database at `base`, or one where the role was
#: never created so every conditional REVOKE no-opped, cannot pass.
MINIMUM_REVOKED_TABLES = 12

CAN_CREATE_DATABASE_SQL = text("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user")


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _alembic(direction: str, revision: str, url: str) -> None:
    """Run alembic as a SUBPROCESS, the same isolation `test_migration_round_trip.py` uses.

    In-process would re-exec `alembic/env.py` inside the interpreter already
    holding this suite's cached engine and settings, and the env-var repointing
    that needs is exactly the kind of global mutation that makes an unrelated
    later test fail.
    """
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=str(REPO),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 - alembic only wants a writable HOME
            "MIGRATION_DATABASE_URL": url,
            "DATABASE_URL": url,
            "ENABLE_AUTH": "false",
            "ENVIRONMENT": "test",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {direction} {revision} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def scratch_database() -> Iterator[str]:
    """A database this test owns outright — `evidence_review_app` is cluster-wide, this database is not.

    `CREATE ROLE` is cluster-level, so `evidence_review_app` (provisioned by
    `tests/conftest.py` before any test runs) already exists here the moment
    this database is created; every migration's `IF EXISTS (SELECT 1 FROM
    pg_roles ...)` guard sees it and actually runs its GRANT/REVOKE, exactly as
    it does against the shared suite database and in production. Only the
    per-database privilege state is scratch.
    """
    name = f"evidence_review_grantguard_{uuid.uuid4().hex[:12]}"
    admin = create_engine(OWNER_URL, future=True, isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            if not conn.execute(CAN_CREATE_DATABASE_SQL).scalar():
                pytest.skip(
                    f"role {conn.execute(text('SELECT current_user')).scalar_one()!r} cannot CREATE "
                    "DATABASE; this guard needs a scratch database to run a clean `alembic upgrade head` "
                    "against"
                )
            shared = conn.execute(text("SELECT current_database()")).scalar_one()
            assert name != shared, "refusing to treat the shared database as scratch"
            try:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            except ProgrammingError as exc:  # pragma: no cover - the pre-check catches the usual case
                if not isinstance(exc.orig, InsufficientPrivilege):
                    raise
                pytest.skip(f"cannot create a scratch database despite the privilege pre-check: {exc.orig}")
            created = True
        yield _with_database(OWNER_URL, name)
    finally:
        if created:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def reference_revocations(scratch_database: str) -> dict[str, frozenset[str]]:
    """`{table: {privileges NOT granted to evidence_review_app}}`, read straight off a fresh `head`.

    Every table-creating migration grants `SELECT, INSERT, UPDATE, DELETE` to
    `evidence_review_app` in the same migration that creates the table
    (verified below, not assumed: `never_granted` would catch a future table
    that skips this). So any privilege absent from
    `information_schema.role_table_grants` afterwards was taken away by a later
    REVOKE — there is no other way for it to be missing.
    """
    _alembic("upgrade", "head", scratch_database)
    engine = create_engine(scratch_database, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            tables = set(
                conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
                .scalars()
                .all()
            )
            granted: dict[str, set[str]] = {}
            for table, privilege in conn.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = :role AND table_schema = 'public'"
                ),
                {"role": APP_ROLE},
            ).all():
                granted.setdefault(table, set()).add(privilege)
    finally:
        engine.dispose()

    never_granted = tables - set(granted)
    assert not never_granted, (
        f"{sorted(never_granted)} hold NO grant at all to {APP_ROLE} on a fresh `head` — that is not a "
        "REVOKE, it is a table whose creating migration never GRANTed the runtime role anything. This "
        "file only knows how to read a subtracted privilege off a full baseline; a table with zero "
        "grants needs its own migration fix, not a change here"
    )

    return {
        table: frozenset(ALL_PRIVILEGES - granted.get(table, set()))
        for table in tables
        if granted.get(table, set()) != ALL_PRIVILEGES
    }


@pytest.fixture(scope="module")
def live_grants() -> dict[str, frozenset[str]]:
    """`{table: {privileges evidence_review_app actually holds}}` on the database the rest of the suite runs against."""
    engine = create_engine(LIVE_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            granted: dict[str, set[str]] = {}
            for table, privilege in conn.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = :role AND table_schema = 'public'"
                ),
                {"role": APP_ROLE},
            ).all():
                granted.setdefault(table, set()).add(privilege)
    finally:
        engine.dispose()
    return {table: frozenset(privileges) for table, privileges in granted.items()}


@pytest.mark.timeout(600)
def test_the_reference_is_not_vacuous(reference_revocations: dict[str, frozenset[str]]) -> None:
    """A reference with too few revocations measured an empty schema, not the real one.

    Without this floor, a scratch database that failed to provision the role
    (every conditional REVOKE no-ops) or a migration run that silently stopped
    partway would report zero tables revoked, and every comparison below would
    pass by having nothing left to check.
    """
    assert len(reference_revocations) >= MINIMUM_REVOKED_TABLES, (
        f"only {len(reference_revocations)} table(s) show a revoked privilege on a fresh `alembic "
        f"upgrade head` — expected at least {MINIMUM_REVOKED_TABLES}. Found: {sorted(reference_revocations)}. "
        "Either the migrations changed a lot, or the scratch database did not get the role provisioned "
        "and every REVOKE's `IF EXISTS` guard silently skipped"
    )


@pytest.mark.timeout(600)
def test_live_grants_agree_with_what_the_migrations_revoke(
    reference_revocations: dict[str, frozenset[str]], live_grants: dict[str, frozenset[str]]
) -> None:
    """For every table any migration revokes a privilege on, the live runtime role must match — exactly.

    This is `_privileges_revoked_by_0015` generalised: instead of trusting one
    migration's Python list, it trusts what Postgres itself concluded after
    running every migration. A future `0016` that revokes on a new table, or
    that revokes a privilege `tests/conftest.py` does not know to re-assert,
    fails HERE, on the table `0016` touches, with no edit to this file.
    """
    # The union, not just `reference_revocations`'s keys: a table the CURRENT
    # migrations no longer restrict, but that the live database still restricts
    # for some other reason (a hand-written re-assertion that was never updated
    # to match a relaxed REVOKE, `0013`'s own shape), is exactly the kind of
    # drift this file exists to catch, and it would be invisible to a loop that
    # only ever asked the reference side which tables to look at.
    restricted_tables = set(reference_revocations) | {
        table for table, privileges in live_grants.items() if privileges != ALL_PRIVILEGES
    }

    mismatches: dict[str, dict[str, list[str]]] = {}
    for table in sorted(restricted_tables):
        revoked = reference_revocations.get(table, frozenset())
        expected_granted = ALL_PRIVILEGES - revoked
        actual_granted = live_grants.get(table, frozenset())
        if actual_granted != expected_granted:
            mismatches[table] = {
                "migrations revoke": sorted(revoked),
                "live role still holds (should be revoked)": sorted(actual_granted & revoked),
                "live role is missing (migrations do not revoke this)": sorted(expected_granted - actual_granted),
            }

    assert not mismatches, (
        f"the live runtime role's privileges on {LIVE_URL.split('@')[-1]!r} disagree with what "
        f"`alembic upgrade head` actually revokes, for {len(mismatches)} table(s):\n{mismatches}\n"
        "If a privilege is 'still held', a REVOKE from the migrations was restored by the harness's "
        "blanket GRANT and never re-asserted — the exact defect this file exists to catch. If a "
        "privilege is 'missing', something is revoking more than the current migrations do."
    )
