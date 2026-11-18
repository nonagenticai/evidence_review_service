"""`T1`/`T2`: preflight must verify the PRIVILEGE, not a proxy for the privilege.

## The defect, as measured

`worker.preflight` asked `pg_policies` whether `<table>_worker` existed and
treated a row's presence as "this worker can do its job". A row-level-security
policy and a `GRANT` are two independent gates that fail in opposite directions:
the policy decides which ROWS a statement may see, the grant decides whether the
statement RUNS at all.

With the `event_deliveries_worker` policy present and `INSERT` revoked on
`event_deliveries`, the audit measured: `preflight()` returned `[]`, the worker
started, the relay copied the outbox row into the durable log, fanned out to
**0** consumers, could not write the poisoned row that records a fan-out failure
(it needs the same revoked `INSERT`), reported `unrelayed_count() == 0` because
progress is derived from the log rather than from delivery, and logged
`relayed=1 deliveries=0`. No exception, no poisoned row, no counter out of place
— and the event is permanently undeliverable, because the outbox row will never
again satisfy the relay's `NOT EXISTS`.

That state is reachable rather than contrived: `0023`'s `ELSIF` branch returns as
soon as it finds the policy already there, so it issues its `GRANT` **only** on
the run that CREATES the policy. Any database whose policy arrived by another
route — `0020`/`0022` with the role present, a restore, a hand-fix — can hold
every policy and no grant.

## Why this is an integration test and not a fake session

The thing under test is what Postgres answers, not what the code asks. A fake
session would encode my belief about `has_table_privilege` and pass whether or
not that belief is right; and the specific claim being made — "a policy can be
present while the grant is absent" — is a claim ABOUT Postgres. So the fixture
puts a real database into exactly the measured state (policy present, `INSERT`
revoked) and connects as the real `evidence_review_worker` role.

## The controls

* The reproduction asserts that NO policy problem is reported in the same run.
  Without that, the test would pass against a preflight that had simply become
  noisier, and it would not show that the OLD check was green here.
* After the grant is restored, `preflight` must return `[]`. Without that, a
  preflight that refused everything unconditionally would satisfy the test.
* A separate test drops one policy while leaving every grant in place, and
  asserts the policy half still fires — the two gates must both be live, not one
  swapped for the other.

## ⚠️ The role this creates is cluster-wide, and is deliberately never dropped

`evidence_review_worker` is a Postgres ROLE, so it is visible to every database
in the cluster; only its grants are per-database. Creating it is idempotent and
matches what `tests/conftest.py` already does for `evidence_review_app`. Dropping
it is NOT done here: another database in the same cluster may hold objects or
grants that depend on it, and a test that removes a shared principal to tidy up
after itself is how one suite breaks another.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: ⚠️ GENERATED per run, not written as a literal. A quoted string next to the
#: name `PASSWORD` is indistinguishable from a leaked credential to
#: detect-secrets, which flagged the hard-coded version of this line as a
#: "Secret Keyword" and failed CI. Baselining it would bury a real-looking
#: secret behind a hash a reviewer cannot interpret, and adding an inline
#: allowlist pragma would teach the next reader that the pattern is fine here.
#: Generating it removes the literal entirely and is the better test anyway:
#: the fixture ALTERs the role's password when the role already exists, so a
#: fresh value each run is harmless and nothing on disk is ever a credential.
WORKER_PASSWORD = secrets.token_urlsafe(24)


def _swap_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
def worker_url() -> Iterator[str]:
    """A database in the state migration `0023` produces when it works.

    Every worker policy present, every worker grant present, connected as
    `evidence_review_worker`. Each test then breaks exactly one of the two gates
    and asserts preflight notices — which is only meaningful because this
    baseline is asserted clean first.
    """
    import worker as worker_module
    from config import Settings

    owner_url = Settings().migration_url
    if not owner_url.startswith("postgresql"):  # pragma: no cover - guarded by pytestmark
        pytest.skip("no owner connection available")

    engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            privileged = conn.execute(
                text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
            ).scalar()
            if not privileged:
                pytest.skip(
                    f"the role in MIGRATION_DATABASE_URL cannot create {worker_module.WORKER_ROLE}; "
                    "this test needs to provision it"
                )
            database = conn.execute(text("SELECT current_database()")).scalar_one()

        role = worker_module.WORKER_ROLE
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        CREATE ROLE {role} LOGIN PASSWORD '{WORKER_PASSWORD}'
                          NOSUPERUSER NOCREATEDB NOCREATEROLE;
                      ELSE
                        ALTER ROLE {role} LOGIN PASSWORD '{WORKER_PASSWORD}'
                          NOSUPERUSER NOCREATEDB NOCREATEROLE;
                      END IF;
                    END
                    $$;
                    """  # nosec B608 - constant identifiers, no caller input
                )
            )
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {role}'))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for table in worker_module.CROSS_TENANT_TABLES:
                # The same statements `0023.upgrade()` issues, applied here
                # because `0023` has already run in this database and a revision
                # runs exactly once — which is `T2`'s whole point.
                conn.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_policies
                             WHERE tablename = '{table}' AND policyname = '{table}_worker'
                          ) THEN
                            EXECUTE 'CREATE POLICY {table}_worker ON {table} TO {role} '
                                    'USING (true) WITH CHECK (true)';
                          END IF;
                        END
                        $$;
                        """  # nosec B608 - constant identifiers, no caller input
                    )
                )
                conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))
    finally:
        engine.dispose()

    yield _swap_credentials(owner_url, worker_module.WORKER_ROLE, WORKER_PASSWORD)

    # Restore the baseline for whatever runs next in this database. The ROLE
    # survives on purpose — see the module docstring.
    engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            for table in worker_module.CROSS_TENANT_TABLES:
                conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {worker_module.WORKER_ROLE}"))
    finally:
        engine.dispose()


def _preflight_as_worker(worker_url: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`worker.preflight`, run on a connection owned by this test.

    `database.get_engine` memoises one process-wide engine, so calling
    `preflight(Settings(database_url=...))` would silently reuse whatever engine
    the rest of the session already built — and the check under test is entirely
    about WHICH ROLE is connected. Substituting the scope is the only way to be
    sure the SQL runs as `evidence_review_worker`.
    """
    import worker as worker_module
    from config import Settings

    engine = create_engine(worker_url, future=True, connect_args={"prepare_threshold": 0})

    # `statement_timeout_ms` mirrors the real `database.session_scope`;
    # `preflight` passes `PREFLIGHT_STATEMENT_TIMEOUT_MS`. This double connects
    # to a REAL database, so it could honour the bound — it deliberately does
    # not: the point of this suite is which GRANTs preflight notices, and a
    # `SET LOCAL` here would add a statement whose failure would be reported as
    # a missing privilege.
    @contextmanager
    def _scope(_settings: Settings, *, statement_timeout_ms: int | None = None) -> Iterator[Session]:
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(worker_module, "session_scope", _scope)
    try:
        return worker_module.preflight(Settings())
    finally:
        engine.dispose()


def _owner_execute(statement: str) -> None:
    from config import Settings

    engine = create_engine(Settings().migration_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


def test_a_revoked_insert_is_reported_even_though_the_policy_is_there(
    worker_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect. Before the fix this returned `[]`."""
    import worker as worker_module

    # The control comes FIRST: with both gates intact preflight must be silent,
    # otherwise the assertion below proves nothing about what it detected.
    assert _preflight_as_worker(worker_url, monkeypatch) == []

    _owner_execute(f"REVOKE INSERT ON event_deliveries FROM {worker_module.WORKER_ROLE}")
    problems = _preflight_as_worker(worker_url, monkeypatch)

    assert problems, (
        "preflight reported a healthy environment while the relay's only write to "
        "event_deliveries was forbidden; every event relayed here becomes permanently "
        "undeliverable with no exception and no poisoned row"
    )
    privilege_problems = [p for p in problems if "event_deliveries (INSERT)" in p]
    assert privilege_problems, f"no problem named the revoked INSERT on event_deliveries: {problems}"

    # The policy half was, and still is, perfectly green here. That is exactly
    # why checking it proved nothing.
    assert not [p for p in problems if "worker policy is absent" in p], (
        "the policy proxy fired, so this run does not demonstrate the gap between a present policy and an absent grant"
    )

    # And it is not a stuck refusal: restoring the grant restores silence.
    _owner_execute(f"GRANT INSERT ON event_deliveries TO {worker_module.WORKER_ROLE}")
    assert _preflight_as_worker(worker_url, monkeypatch) == []


def test_every_verb_the_worker_executes_is_checked(worker_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `(table, verb)` in `REQUIRED_PRIVILEGES` is individually load-bearing.

    A check that happened to look only at `SELECT`, or only at the four
    outbox/inbox tables, would pass the test above (it revokes an `INSERT` on
    `event_deliveries`) while leaving the reaper's `UPDATE durable_jobs` and the
    attempt seal's `INSERT job_attempts` unguarded. So every declared pair is
    revoked in turn and must be named.
    """
    import worker as worker_module

    for table, privileges in worker_module.REQUIRED_PRIVILEGES.items():
        for privilege in privileges:
            _owner_execute(f"REVOKE {privilege} ON {table} FROM {worker_module.WORKER_ROLE}")
            problems = _preflight_as_worker(worker_url, monkeypatch)
            assert any(f"{table} ({privilege})" in p for p in problems), (
                f"revoking {privilege} on {table} left preflight silent: {problems}"
            )
            _owner_execute(f"GRANT {privilege} ON {table} TO {worker_module.WORKER_ROLE}")

    assert _preflight_as_worker(worker_url, monkeypatch) == []


def test_the_policy_half_of_the_check_is_still_live(worker_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The privilege check is ADDITIVE, not a replacement.

    A grant with no policy is the mirror-image failure: every statement runs and
    every one of them reads zero rows across tenants, which is the "looks
    healthy, does nothing" condition `worker.py` was written to end.
    """
    import worker as worker_module

    _owner_execute("DROP POLICY domain_outbox_worker ON domain_outbox")
    try:
        problems = _preflight_as_worker(worker_url, monkeypatch)
        assert any("worker policy is absent" in p and "domain_outbox" in p for p in problems), (
            f"dropping domain_outbox_worker left preflight silent: {problems}"
        )
    finally:
        _owner_execute(
            f"CREATE POLICY domain_outbox_worker ON domain_outbox TO {worker_module.WORKER_ROLE} "
            "USING (true) WITH CHECK (true)"
        )
    assert _preflight_as_worker(worker_url, monkeypatch) == []
