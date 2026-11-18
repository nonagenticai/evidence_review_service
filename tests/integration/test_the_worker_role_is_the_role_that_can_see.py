"""The visibility probe's YES branch, exercised by the role production gives it.

`test_the_relay_depth_gauges_follow_the_database.py` covers both *outcomes* of
`RelayDepthCollector`'s probe — refused for `evidence_review_app`, allowed for
the owner connection — and that is not the same thing as covering both *branches
of the SQL*. `_VISIBILITY_SQL` is a disjunction:

    (NOT row_security_active(t.name))  OR  (<a permissive USING(true) applies to
                                             me>  AND  <no restrictive policy
                                             applies to me>)

The app-role test is false on both sides, and the owner test connects through
``MIGRATION_DATABASE_URL`` — a SUPERUSER, for whom `NOT row_security_active(...)`
is true and short-circuits the entire right-hand side. So every scrape the suite
ever performed answered the question using the FIRST disjunct. The second one —
the one that decides whether the real worker can see, and therefore whether this
service exports a queue depth at all — was never executed.

## ⛔ The two mutants that survived 1984 tests, and what they cost

Measured on this branch against a live Postgres, whole suite, twice:

    src/metrics.py:158  `p.qual = 'true'` -> `'MUTANT_NEVER_MATCHES'`   1984 passed
    src/metrics.py:169  `AND NOT EXISTS (… 'RESTRICTIVE' …)` -> `AND true`  1984 passed

Neither is exotic. The first is one plausible tightening away — `pg_get_expr`
parenthesises, so `p.qual = '(true)'` reads like a *correction* — and it fails in
the direction nothing alerts on: `relay_depth_observable{reason="row_level_security"} 0`
forever, all three depth gauges permanently ABSENT, and the metric simply stops
existing rather than lying. The second removes the fail-closed guard against a
RESTRICTIVE policy, which fails in the direction this whole module exists to
prevent — a confident zero from a role that has been narrowed back down.

`M4`/`M5` in the existing suite mutate `_can_see_every_row`'s RETURN VALUE, so
they prove the collector branches on the answer. They say nothing about the SQL
that computes it.

## The role that makes the branch reachable

`_VISIBILITY_SQL` run verbatim on the production primary (worker4, 2026-09-06):

    evidence_review_worker  | t
    evidence_review_app     | f
    postgres                | t

Only the first of those three is on the permissive branch: RLS *is* active for
it, and it reads `t` because migrations `0020`/`0022`/`0023` grant it
`PERMISSIVE / ALL / {evidence_review_worker} / true` on every relay table.
`postgres` reads `t` for the uninteresting reason. So the fixture below stands up
that role — the same shape `0023` produces, provisioned the same way
`test_the_preflight_checks_grants_not_only_policies.py` provisions it — and every
assertion here is made through it.

⚠️ The point is NOT that a boolean flips. `test_the_worker_role_exports_three_real_depths`
asserts the three gauges come back carrying the OWNER's numbers and then move
with a row written under a tenant the scrape never binds. A probe that returned
`True` while the depths stayed 0 would be the same false zero wearing a
different hat.

## ⛔ What must NOT be done to make this pass

`evidence_review_app` reading `f` is the specification, not a defect. Widening
the SQL until the app role reads `t` would delete the entire reason this
collector asks before it counts. The production measurement above is the
acceptance criterion in both directions, and
`test_the_two_roles_read_exactly_what_production_reads` pins both.
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import metrics  # noqa: E402
from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: Generated per run, never stored. Same argument as the preflight suite's: the
#: fixture ALTERs the password when the role already exists, so a fresh value
#: each run is harmless and nothing on disk is ever a credential.
WORKER_PASSWORD = secrets.token_urlsafe(24)

#: A fresh tenant per RUN. Nothing truncates between runs, so a stable label
#: would leave the previous run's rows inside this run's reach — and this module
#: reads gauges that count every tenant at once.
RUN = uuid.uuid4().hex[:12]
TENANT = tenant(f"tenant-worker-visibility-{RUN}")
TENANT_UUID = uuid.UUID(TENANT)

#: The restrictive policy `test_a_restrictive_policy_withdraws_the_depths`
#: installs and removes. Named distinctly so its teardown cannot drop one of the
#: schema's own.
_RESTRICTIVE_POLICY = "domain_outbox_mutant_restrictive"


def _swap_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _owner_url() -> str:
    """Where `tests/conftest.py` keeps the inherited owner/superuser URL."""
    from config import Settings

    return Settings().migration_url


@contextmanager
def _owner_session() -> Iterator[Session]:
    """A session on the owner connection. Built per call, disposed per call, so a
    test that leaves a transaction open cannot poison the next one."""
    engine = create_engine(_owner_url(), future=True, connect_args={"prepare_threshold": 0})
    session = Session(engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()
        engine.dispose()


def _owner_execute(statement: str, params: dict[str, Any] | None = None) -> Any:
    with _owner_session() as session:
        return session.execute(text(statement), params or {})


@pytest.fixture
def worker_connect():  # noqa: ANN201
    """A `Connect` for `RelayDepthCollector`, on the real `evidence_review_worker`.

    The database is put in the state migration `0023` produces when the role
    exists at migration time: every `<table>_worker` policy present, every worker
    grant present. `0023` has already run in this database and a revision runs
    exactly once, so the statements are re-issued here rather than re-running the
    migration — the same thing, for the same reason,
    `test_the_preflight_checks_grants_not_only_policies.py::worker_url` does it.

    ⛔ The list of tables is `worker.CROSS_TENANT_TABLES`, imported and never
    retyped. A second hand-maintained copy drifts in the direction of granting
    too much, and it would do so in the one place that checks.
    """
    import worker as worker_module

    owner_url = _owner_url()
    if not owner_url.startswith("postgresql"):  # pragma: no cover - guarded by pytestmark
        pytest.skip("no owner connection available")

    engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
    role = worker_module.WORKER_ROLE
    try:
        with engine.connect() as conn:
            privileged = conn.execute(
                text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
            ).scalar()
            if not privileged:
                pytest.skip(f"the role in MIGRATION_DATABASE_URL cannot create {role}; this test needs to provision it")
            database = conn.execute(text("SELECT current_database()")).scalar_one()

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

    worker_url = _swap_credentials(owner_url, role, WORKER_PASSWORD)
    worker_engine = create_engine(worker_url, future=True, connect_args={"prepare_threshold": 0})

    @contextmanager
    def _connect() -> Iterator[Session]:
        session = Session(worker_engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    try:
        yield _connect
    finally:
        worker_engine.dispose()
        # Whatever a test broke, put it back. The ROLE survives on purpose —
        # `0023`'s policies name it, and dropping it would drop them.
        engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                          IF EXISTS (
                            SELECT 1 FROM pg_policies
                             WHERE tablename = 'domain_outbox' AND policyname = '{_RESTRICTIVE_POLICY}'
                          ) THEN
                            EXECUTE 'DROP POLICY {_RESTRICTIVE_POLICY} ON domain_outbox';
                          END IF;
                        END
                        $$;
                        """  # nosec B608 - constant identifier
                    )
                )
                for table in worker_module.CROSS_TENANT_TABLES:
                    conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}"))
        finally:
            engine.dispose()


def _samples(connect) -> dict[str, float]:  # noqa: ANN001
    """Every sample `RelayDepthCollector` emits through `connect`, flattened.

    Same shape as the sibling depth suite's helper: an ABSENT depth series is a
    missing dict key rather than a zero, which is the distinction under test.
    """
    flat: dict[str, float] = {}
    for family in metrics.RelayDepthCollector(connect).collect():
        for sample in family.samples:
            key = sample.name
            if sample.labels:
                key = f"{sample.name}[{','.join(f'{k}={v}' for k, v in sorted(sample.labels.items()))}]"
            flat[key] = sample.value
    return flat


def _actor() -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id="lead-worker-visibility", role=ActorRole("lead"))


def _create_review(title: str) -> uuid.UUID:
    """One review, therefore exactly one `review.created.v1` in the outbox.

    Written by the APP role under a bound tenant — which is what makes the read
    below cross-tenant: the scrape binds no tenant at all.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="worker-visibility",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        return review.review_id


# ──────────────────────────────────────────────────────────────────────────
# The production measurement, pinned in both directions.
# ──────────────────────────────────────────────────────────────────────────


def test_the_two_roles_read_exactly_what_production_reads(worker_connect) -> None:  # noqa: ANN001
    """`evidence_review_worker` -> t, `evidence_review_app` -> f. Both halves.

    ⛔ The second assertion is not redundant with the first. A change that made
    the probe generous enough for the app role would satisfy the first and
    reintroduce the confident zero the collector exists to refuse — and it would
    do so while looking like a fix.

    The `row_security_active` control is what makes the first assertion mean
    "the permissive branch of `_VISIBILITY_SQL` returned true" rather than
    "something returned true". Without it a superuser connection passes this
    test on the `NOT row_security_active(...)` short-circuit, which is exactly
    how the branch went uncovered for the whole of PR #86.
    """
    from database import session_scope

    with worker_connect() as session:
        current_user = session.execute(text("SELECT current_user")).scalar_one()
        superuser = session.execute(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar_one()
        rls_active = session.execute(text("SELECT row_security_active('domain_outbox')")).scalar_one()
        worker_sees = metrics.RelayDepthCollector._can_see_every_row(session)

    assert current_user == "evidence_review_worker", (
        f"the fixture connected as {current_user!r}; every assertion below is about the role "
        "production runs the relay as"
    )
    assert not superuser, (
        f"{current_user} is SUPERUSER or BYPASSRLS in this database, so "
        "`NOT row_security_active(...)` short-circuits `_VISIBILITY_SQL` and the permissive branch "
        "is still not being executed. That is the exact hole this module was written to close."
    )
    assert rls_active, (
        "row-level security is NOT active for evidence_review_worker on domain_outbox, so this role "
        "reaches the probe's answer through the first disjunct like the owner connection does. The "
        "role must be a non-owner (migrations run as the owner; conftest provisions this role "
        "separately) for the permissive-policy branch to be the one under test."
    )
    assert worker_sees is True, (
        "evidence_review_worker was told it cannot read the relay tables across tenants. It can: "
        "migrations 0020/0022/0023 give it PERMISSIVE / ALL / USING (true) on every table in "
        "DEPTH_TABLES, and it holds no restrictive policy. Measured on the production primary "
        "(worker4) this role reads `t`. A probe that says `f` here exports "
        f'{metrics.DEPTH_OBSERVABLE}{{reason="{metrics.REASON_ROW_LEVEL_SECURITY}"}} 0 forever and '
        "all three depth gauges are permanently absent — the metric silently stops existing."
    )

    with session_scope() as app_session:
        app_user = app_session.execute(text("SELECT current_user")).scalar_one()
        app_sees = metrics.RelayDepthCollector._can_see_every_row(app_session)

    assert app_sees is False, (
        f"{app_user} was told it CAN read the relay tables across tenants. ⛔ This is the direction "
        "that must never be loosened: the app role holds only the tenant policy, a scrape binds no "
        "tenant, so every depth it reports is 0 no matter what is in the queue — the value a healthy "
        "drained queue has, stable, and unchanged through a relay that died a week ago."
    )


def test_the_worker_role_exports_three_real_depths(worker_connect) -> None:  # noqa: ANN001
    """Observable, and then the three gauges carrying the database's own numbers.

    ⚠️ `observable == 1.0` alone would be satisfied by a collector that emitted
    the flag and nothing else, and by three gauges hard-wired to 0. So this
    cross-checks each depth against the OWNER connection's count of the same
    thing, and then writes one outbox row under a tenant the scrape never binds
    and requires the worker's gauge to move by exactly one.
    """
    flat = _samples(worker_connect)

    observable = f"{metrics.DEPTH_OBSERVABLE}[reason={metrics.REASON_OBSERVABLE}]"
    assert flat.get(observable) == 1.0, (
        "evidence_review_worker did not report itself observable, so no depth was exported at all. "
        f"Samples: {sorted(flat)}"
    )
    for present in (metrics.OUTBOX_UNRELAYED, metrics.DELIVERIES_PENDING, metrics.DELIVERIES_POISONED):
        assert present in flat, f"{present} is missing on the role that is granted USING (true) on its tables"

    with _owner_session() as session:
        expected = metrics.RelayDepthCollector._depths(session)

    measured = (
        flat[metrics.OUTBOX_UNRELAYED],
        flat[metrics.DELIVERIES_PENDING],
        flat[metrics.DELIVERIES_POISONED],
    )
    assert measured == tuple(float(v) for v in expected), (
        f"the worker role exported {measured} where the owner connection counts {expected} for the "
        "same three facts at the same instant. The gauges are not reading the whole table."
    )

    before = flat[metrics.OUTBOX_UNRELAYED]
    _create_review("a-row-the-scrape-never-bound-a-tenant-for")
    after = _samples(worker_connect)[metrics.OUTBOX_UNRELAYED]
    assert after == before + 1, (
        f"one un-relayed outbox row moved the worker's gauge by {after - before}. The row was "
        "written under a bound tenant and the scrape binds none, so a gauge that did not move is a "
        "gauge reading only the rows of a tenant nobody set."
    )


# ──────────────────────────────────────────────────────────────────────────
# The fail-closed half: a RESTRICTIVE policy narrows the answer back.
# ──────────────────────────────────────────────────────────────────────────


def test_a_restrictive_policy_withdraws_the_depths(worker_connect) -> None:  # noqa: ANN001
    """Postgres ANDs restrictive policies in AFTER the permissive ones.

    So a role holding `USING (true)` PERMISSIVE can still be narrowed to nothing,
    and `_VISIBILITY_SQL`'s `AND NOT EXISTS (… 'RESTRICTIVE' …)` is what stops
    the collector reporting the resulting zero as a fact.

    This test makes the narrowing REAL rather than notional — `USING (false)`,
    so the worker's own `count(*)` collapses to 0 while the owner still sees rows
    — and asserts the collector refuses. Without the control on the counts, a
    green run here would be consistent with a restrictive policy that changed
    nothing, and the assertion would be about a `pg_policies` row rather than
    about a wrong number that nearly got exported.
    """
    import worker as worker_module

    _create_review("a-row-a-restrictive-policy-will-hide")

    with _owner_session() as session:
        owner_rows = session.execute(text("SELECT count(*) FROM domain_outbox")).scalar_one()
    assert owner_rows > 0, "no outbox rows exist, so hiding them proves nothing"

    _owner_execute(
        f"CREATE POLICY {_RESTRICTIVE_POLICY} ON domain_outbox AS RESTRICTIVE "  # nosec B608
        f"TO {worker_module.WORKER_ROLE} USING (false)"
    )
    try:
        with worker_connect() as session:
            worker_rows = session.execute(text("SELECT count(*) FROM domain_outbox")).scalar_one()
        assert worker_rows == 0, (
            f"the restrictive policy did not narrow anything — the worker still counts {worker_rows} "
            f"of the owner's {owner_rows} outbox rows. This test then measures nothing."
        )

        flat = _samples(worker_connect)
    finally:
        _owner_execute(f"DROP POLICY IF EXISTS {_RESTRICTIVE_POLICY} ON domain_outbox")  # nosec B608

    refused = f"{metrics.DEPTH_OBSERVABLE}[reason={metrics.REASON_ROW_LEVEL_SECURITY}]"
    assert flat.get(refused) == 0.0, (
        "a RESTRICTIVE policy hid every outbox row from evidence_review_worker and the collector "
        f"still reported itself observable. Samples: {sorted(flat)}. Postgres ANDs restrictive "
        "policies in after the permissive ones, so a USING (true) permissive policy is not on its "
        "own an answer to 'can I see every row'."
    )
    for absent in (metrics.OUTBOX_UNRELAYED, metrics.DELIVERIES_PENDING, metrics.DELIVERIES_POISONED):
        assert absent not in flat, (
            f"{absent} was exported while a restrictive policy hid the rows it counts. Its value is "
            f"0 against {owner_rows} real outbox rows — a confident zero, which is the one thing "
            "this collector exists to never produce."
        )

    # And the state is restored, so the next reader of this database is not
    # measuring the wreckage of this test.
    assert _samples(worker_connect).get(f"{metrics.DEPTH_OBSERVABLE}[reason={metrics.REASON_OBSERVABLE}]") == 1.0, (
        "the restrictive policy outlived the test that created it"
    )


# ──────────────────────────────────────────────────────────────────────────
# `DEPTH_TABLES` is a list, and a list can be wrong.
# ──────────────────────────────────────────────────────────────────────────


class _Recording:
    """A session that remembers the SQL it was asked to run.

    ⛔ It records what `_depths` ACTUALLY executes rather than re-listing the
    three statements here. `unrelayed_count` lives in `application.services.relay`
    and its `NOT EXISTS` against `research_domain_events` is the reason that table
    is in `DEPTH_TABLES` at all — a copy of it in this file would go stale in
    exactly the case the test exists to catch.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.statements: list[str] = []

    def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        self.statements.append(str(statement))
        if params is None:
            return self._session.execute(statement, **kwargs)
        return self._session.execute(statement, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


#: Postgres records a view's table dependencies from the rewrite rule it stores
#: for it — that is, from the PARSE TREE. Asking `pg_depend` about a throwaway
#: view over a statement is therefore a question about the query as written.
_RELATIONS_OF_VIEW = """
SELECT DISTINCT c.relname
  FROM pg_depend dep
  JOIN pg_rewrite r ON r.oid = dep.objid
  JOIN pg_class c ON c.oid = dep.refobjid
 WHERE dep.classid = 'pg_rewrite'::regclass
   AND dep.refclassid = 'pg_class'::regclass
   AND r.ev_class = CAST(:view AS regclass)
   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND c.oid <> CAST(:view AS regclass)
"""


def _relations_read_by(session: Session, statement: str, ordinal: int) -> set[str]:
    """Every table `statement` NAMES, read out of Postgres's own parse of it.

    ⛔ Not `EXPLAIN`. `EXPLAIN` reports the PLAN, and the planner removes a
    relation it can prove contributes nothing — measured here: a depth query
    given `JOIN domain_inbox i ON false` produced a plan with no `domain_inbox`
    in it, and an `EXPLAIN`-based version of this test passed while a fourth
    relay table sat in the query. That is the exact mutant this test is for, so
    the instrument that cannot see it is the wrong instrument.

    A temp view is created instead and its stored rewrite rule interrogated
    through `pg_depend`. The view is temporary and dropped immediately, so this
    leaves nothing behind; a statement carrying a bind parameter would fail
    loudly at `CREATE VIEW`, which is the correct outcome — none of the depth
    statements has one, and one that did could not be checked this way silently.
    """
    view = f"_ers_depth_probe_{ordinal}"
    session.execute(text(f"CREATE TEMP VIEW {view} AS {statement}"))  # nosec B608 - module-owned SQL
    try:
        return set(session.execute(text(_RELATIONS_OF_VIEW), {"view": view}).scalars().all())
    finally:
        session.execute(text(f"DROP VIEW {view}"))  # nosec B608 - name built above


def test_depth_tables_names_every_table_a_depth_query_reads() -> None:
    """`DEPTH_TABLES` is derived from the queries, not trusted.

    The visibility probe asks about the tables in `DEPTH_TABLES`. A relay table
    that a depth query reads but the list omits is invisible to the probe, so the
    collector would certify a reading it cannot actually see — and the docstring
    on `DEPTH_TABLES` says why that matters most for `research_domain_events`:
    an RLS-hidden log makes EVERY outbox row look un-relayed.

    Nothing enforced the correspondence. Adding a fourth table to a depth query
    and forgetting this tuple failed no test. This is that test, and it needs no
    DDL and no owner privilege beyond the read the suite already has.
    """
    with _owner_session() as session:
        recording = _Recording(session)
        metrics.RelayDepthCollector._depths(recording)
        assert recording.statements, "_depths executed no SQL, so this test measured nothing"

        read: set[str] = set()
        for ordinal, statement in enumerate(recording.statements):
            read |= _relations_read_by(session, statement, ordinal)

    listed = set(metrics.DEPTH_TABLES)
    assert read == listed, (
        f"the depth queries read {sorted(read)} and DEPTH_TABLES lists {sorted(listed)}.\n"
        f"  missing from DEPTH_TABLES: {sorted(read - listed)} — the visibility probe never asks "
        "about these, so the collector can certify a depth it is not allowed to see and export a "
        "confident zero.\n"
        f"  listed but never read: {sorted(listed - read)} — the probe refuses on a table no gauge "
        "depends on, which suppresses a true metric for no reason."
    )
