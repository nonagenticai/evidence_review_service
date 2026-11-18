"""upgrade -> downgrade base -> upgrade head must land on the same schema.

The downgrades in `alembic/versions` are correct today — that was verified by
hand. What did not exist was anything that would notice when they stop being
correct, and a broken downgrade is discovered at the worst possible moment: mid
rollback, on the database holding the review corpus, with the deployment already
failing.

The guarantee is deliberately not "the tables come back". It is that the objects
the isolation guarantees are MADE OF come back: `FORCE ROW LEVEL SECURITY` on
every tenant table, one policy per table, and the triggers that enforce
append-only history. A downgrade that recreated the tables but dropped the FORCE
flag would leave a schema that looks restored and silently enforces nothing.

## Why this cannot run against the shared database

`downgrade base` drops every table. Run against the database the rest of the
suite uses, it would delete the fixtures out from under a parallel test and
leave the schema at base if it failed midway. So it creates a scratch database
of its own, migrates that, and drops it in a `finally`. The name carries a
random suffix, so two concurrent runs — the fleet routinely has several agent
sessions on one host — cannot collide.

Alembic is invoked as a SUBPROCESS rather than through `alembic.command`. Running
it in-process would re-exec `alembic/env.py` inside the interpreter that already
holds this repository's cached engine and settings, and the env var repointing
that requires is exactly the kind of global mutation that makes a later,
unrelated test fail somewhere else.

Skipped when no postgres is reachable, and also when the connected role cannot
CREATE DATABASE. That second case is not hypothetical: `tests/conftest.py`
documents the locked-down database — where the connected role is not privileged
enough to provision `evidence_review_app` — as a supported way to run this suite,
and in that configuration `DATABASE_URL` names an unprivileged role. Erroring
there would report a broken test where the truth is "this environment cannot host
the check".

NOT marked `integration` — this repository has no deselected lane, because a lane
CI never runs is advertised coverage rather than real coverage.
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

#: Migrations run as the OWNER. `DATABASE_URL` is the non-owner runtime role and
#: cannot create a database, let alone a table.
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not OWNER_URL.startswith("postgresql"),
        reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
    ),
]

#: Floors, not expected values. Asserting only `before == after` would pass on
#: two empty snapshots — which is exactly what a silently-failed upgrade
#: produces. The real counts are 25 tables, 25 forced, 25 policies, 35 triggers;
#: these are set low enough that adding or removing one table does not touch
#: this file, and high enough that an empty or half-applied schema cannot pass.
MINIMUM_TABLES = 20
MINIMUM_POLICIES = 20
MINIMUM_TRIGGERS = 20

SNAPSHOT_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM pg_tables
        WHERE schemaname = 'public' AND tablename <> 'alembic_version')                     AS tables,
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relforcerowsecurity)           AS forced_rls,
      (SELECT count(*) FROM pg_policies WHERE schemaname = 'public')                        AS policies,
      (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND NOT t.tgisinternal)                                  AS triggers
    """
)

TABLE_NAMES_SQL = text(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' ORDER BY 1"
)


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _alembic(direction: str, revision: str, url: str) -> None:
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=str(REPO),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 - alembic only wants a writable HOME
            "MIGRATION_DATABASE_URL": url,
            # env.py prefers the owner URL, but set both so a future change to
            # that preference order cannot silently point this at the shared
            # database the rest of the suite is using.
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


#: CREATEDB is a role ATTRIBUTE, so it is not acquired through role membership —
#: asking `pg_roles` about `current_user` is the whole answer, and asking before
#: issuing any DDL turns an unrunnable environment into a clean skip instead of a
#: traceback. The `InsufficientPrivilege` catch below still stands behind it, for
#: the cases this query cannot see (a database-level or connection-limit refusal).
CAN_CREATE_DATABASE_SQL = text("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user")


@pytest.fixture()
def scratch_database() -> Iterator[str]:
    """A database this test owns outright, dropped even when the test fails."""
    name = f"evidence_review_roundtrip_{uuid.uuid4().hex[:12]}"
    admin = create_engine(OWNER_URL, future=True, isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            if not conn.execute(CAN_CREATE_DATABASE_SQL).scalar():
                pytest.skip(
                    f"role {conn.execute(text('SELECT current_user')).scalar_one()!r} cannot CREATE "
                    "DATABASE; this round trip needs a scratch database of its own because "
                    "`downgrade base` would otherwise drop the shared schema"
                )
            shared = conn.execute(text("SELECT current_database()")).scalar_one()
            # The whole point of the scratch database. If this ever equals the
            # shared one, `downgrade base` below deletes the suite's fixtures.
            assert name != shared, "refusing to run a destructive round trip against the shared database"
            try:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            except ProgrammingError as exc:  # pragma: no cover - the pre-check catches the usual case
                if not isinstance(exc.orig, InsufficientPrivilege):
                    raise
                pytest.skip(f"cannot create a scratch database despite the privilege pre-check: {exc.orig}")
            created = True
        yield _with_database(OWNER_URL, name)
    finally:
        # Only if we got as far as creating it — a skipped run must not issue DDL
        # it has already been told it is not allowed to issue.
        if created:
            with admin.connect() as conn:
                # `WITH (FORCE)` because a failed alembic run can leave a connection
                # behind, and a leaked scratch database would then survive to
                # confuse the next run.
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _snapshot(url: str) -> dict[str, object]:
    engine = create_engine(url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            counts = conn.execute(SNAPSHOT_SQL).mappings().one()
            names = conn.execute(TABLE_NAMES_SQL).scalars().all()
    finally:
        engine.dispose()
    return {**dict(counts), "table_names": list(names)}


@pytest.mark.timeout(600)
def test_downgrade_to_base_and_back_reproduces_the_schema(scratch_database: str) -> None:
    _alembic("upgrade", "head", scratch_database)
    before = _snapshot(scratch_database)

    assert before["tables"] >= MINIMUM_TABLES, f"upgrade produced only {before['tables']} tables: {before}"
    assert before["forced_rls"] == before["tables"], (
        "every tenant table must carry FORCE ROW LEVEL SECURITY; "
        f"{before['tables'] - before['forced_rls']} do not: {before}"
    )
    assert before["policies"] >= MINIMUM_POLICIES, before
    assert before["triggers"] >= MINIMUM_TRIGGERS, before

    _alembic("downgrade", "base", scratch_database)
    emptied = _snapshot(scratch_database)
    # Without this the test is vacuous: if `downgrade base` were a no-op, the
    # re-upgrade would have nothing to do and the two snapshots would match for
    # the wrong reason.
    assert emptied["tables"] == 0, f"downgrade base left {emptied['table_names']} behind"
    assert emptied["policies"] == 0 and emptied["triggers"] == 0, emptied

    _alembic("upgrade", "head", scratch_database)
    after = _snapshot(scratch_database)

    assert after == before
