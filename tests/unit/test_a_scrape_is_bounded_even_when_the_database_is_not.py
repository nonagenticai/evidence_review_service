"""A scrape is BOUNDED. Always, and whatever the database is doing.

## The measured defect

`RelayDepthCollector.collect()` wrapped its database work in `except Exception`
and failed closed. That is correct handling for a query that RAISES, and it is
no handling at all for one that never returns — **a hang raises nothing**, so
the guard could not fire.

It was not hypothetical. With `DATABASE_URL` unset, `Settings` falls back to
`postgresql+psycopg://…@db.example:5432/evidence_review`, and psycopg3
resolves host names in Python (`_conninfo_attempts._resolve_hostnames` ->
`socket.getaddrinfo`) BEFORE libpq is handed anything. Measured on a host whose
internal DNS resolvers are unreachable::

    socket.getaddrinfo("db.example", 5432)
    ERR gaierror [Errno 8] nodename nor servname provided, or not known
    elapsed 30.015876054763794

⚠️ `connect_timeout` cannot help there — libpq is never reached. That 30 s is
also the 30.03 s an audit measured for a scrape against the live worker, so this
is the production mechanism and not a local curiosity.

## Why a hanging scrape is worse than no scrape

The worker's pool is `DB_POOL_SIZE=2` + `DB_MAX_OVERFLOW=2` (1 + 1 in
production today) and a blocked scrape holds a slot for its whole duration. The
relay loop ticks every 5 s and `worker_heartbeat` calls the heartbeat stale at
`6x` the interval — 30 s. A scrape that can block for 30 s can therefore starve
the tick past the staleness bound and have the `livenessProbe` restart a
WORKING worker. The metric takes down the process it exists to observe.

## What these tests pin

⛔ They assert ELAPSED TIME, not the presence of a label. `query_failed` appears
in the body under every failure mode this module has; only the clock can tell
"refused promptly" from "answered eventually". Two of them therefore cost real
wall-clock seconds on purpose, and shortening them would mean measuring
something other than the property.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

import metrics
import worker_heartbeat
from config import Settings

#: How much longer than the budget a scrape may take before this suite calls it
#: unbounded. Generous: the thread hand-off, the GIL and a loaded CI runner all
#: land in here, and a test that fails on scheduling noise gets muted rather
#: than fixed. It is still far below the 30 s the defect actually cost.
SLACK_SECONDS = 2.0


@contextmanager
def _wedged(gate: threading.Event) -> Iterator[Any]:
    """A `Connect` that never yields a session and never raises.

    The whole class of failure the bound exists for, in one line: `getaddrinfo`
    on an unreachable resolver, a TCP connect into a black hole, a `SELECT`
    behind a lock. None of them can be interrupted from the calling thread, so
    the only ceiling is to stop waiting for them.
    """
    gate.wait()
    yield None  # pragma: no cover - reached only after the gate is released


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    """Released at teardown so the abandoned daemon thread can finish and exit."""
    event = threading.Event()
    try:
        yield event
    finally:
        event.set()


def _observable(collector: metrics.RelayDepthCollector) -> dict[str, float]:
    """`{reason: value}` for the one series that is always present."""
    found: dict[str, float] = {}
    for family in collector.collect():
        for sample in family.samples:
            if sample.name == metrics.DEPTH_OBSERVABLE:
                found[sample.labels["reason"]] = sample.value
    return found


def test_a_scrape_against_a_database_that_never_answers_returns_inside_the_budget(gate) -> None:  # noqa: ANN001
    """The headline property, at the PRODUCTION budget.

    No `budget_seconds` override: this test is worth having only if the number
    it measures is the number the worker ships with. It costs
    `DEPTH_SCRAPE_BUDGET_SECONDS` of wall clock, which is the point.

    The lower bound matters as much as the upper one. A collector that answered
    `query_failed` without attempting anything would satisfy "fast" and export a
    permanent lie; asserting the scrape actually waited its budget keeps the
    refusal honest.
    """
    collector = metrics.RelayDepthCollector(lambda: _wedged(gate))

    started = time.monotonic()
    found = _observable(collector)
    elapsed = time.monotonic() - started

    assert elapsed <= metrics.DEPTH_SCRAPE_BUDGET_SECONDS + SLACK_SECONDS, (
        f"the scrape took {elapsed:.2f}s against a database that never answered. The budget is "
        f"{metrics.DEPTH_SCRAPE_BUDGET_SECONDS}s and it is not advisory: an unbounded scrape holds "
        "one of the worker's two pool slots, which is how /metrics starves the relay tick past the "
        "30s staleness bound and gets a working worker restarted by its own livenessProbe."
    )
    assert elapsed >= metrics.DEPTH_SCRAPE_BUDGET_SECONDS * 0.8, (
        f"the scrape gave up after {elapsed:.2f}s, well short of the {metrics.DEPTH_SCRAPE_BUDGET_SECONDS}s "
        "budget. A collector that refuses without attempting is not bounded, it is broken: it would "
        "export query_failed against a perfectly healthy database."
    )
    assert found == {metrics.REASON_QUERY_FAILED: 0.0}, (
        f"a scrape that timed out exported {found}. It must be exactly "
        f'{metrics.DEPTH_OBSERVABLE}{{reason="{metrics.REASON_QUERY_FAILED}"}} 0 — the depth gauges '
        "ABSENT rather than zero, and a reason from the closed vocabulary saying which."
    )


def test_a_reading_that_has_not_come_back_does_not_get_a_second_connection(gate) -> None:  # noqa: ANN001
    """Single-flight. This is the half that protects the POOL, not the scrape.

    Bounding one scrape is not enough on its own. Prometheus scrapes on its own
    cadence and does not wait for the last answer; if every scrape started
    another reading, a database that is merely slow would have `/metrics` alone
    holding the worker's entire 2-connection pool within two scrapes — which is
    what an audit measured: a 30.03 s scrape with BOTH slots held.

    So while a reading is outstanding the next scrape answers immediately and
    starts nothing. Measured two ways, because either alone can pass for the
    wrong reason: the second scrape is fast (so it did not wait), and only one
    connection was ever opened (so it did not merely wait less).
    """
    opened: list[int] = []

    @contextmanager
    def _counting_connect() -> Iterator[Any]:
        opened.append(1)
        gate.wait()
        yield None  # pragma: no cover - reached only after the gate is released

    collector = metrics.RelayDepthCollector(_counting_connect, budget_seconds=0.5)

    assert _observable(collector) == {metrics.REASON_QUERY_FAILED: 0.0}

    started = time.monotonic()
    second = _observable(collector)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25, (
        f"the second scrape waited {elapsed:.2f}s while the first reading was still outstanding. "
        "It must refuse immediately: waiting again is what lets concurrent scrapes accumulate "
        "connections until the relay loop cannot get one."
    )
    assert second == {metrics.REASON_QUERY_FAILED: 0.0}, second
    assert opened == [1], (
        f"{len(opened)} connections were opened for 2 scrapes. At most ONE connection may ever be "
        "held on behalf of /metrics; the worker's pool is 2 (1 in production) and the relay tick "
        "needs one of them."
    )


def test_the_depth_read_bounds_its_statements_before_it_issues_any(gate) -> None:  # noqa: ANN001
    """`SET LOCAL statement_timeout`, and BEFORE the first real statement.

    The wall-clock deadline stops the SCRAPE waiting; it cannot stop the
    abandoned query. Without a server-side bound the reading the deadline walked
    away from keeps its connection — and the pool slot — for as long as Postgres
    is willing to run it, so the next scrape's single-flight refusal would be
    permanent rather than transient.

    `SET LOCAL` rather than `SET`: the engine is shared with the relay loop, and
    a session-scoped timeout would leak a 750 ms ceiling onto the worker's own
    batch work through a pooled connection.

    Ordering is asserted, not just presence: a bound applied after the visibility
    probe leaves the probe itself unbounded.
    """
    statements: list[str] = []

    class _Result:
        @staticmethod
        def scalar_one() -> bool:
            return False  # refuse visibility, so `_depths` is not reached

    class _Session:
        @staticmethod
        def execute(clause: Any, params: Any = None) -> _Result:  # noqa: ARG004
            statements.append(str(clause))
            return _Result()

    @contextmanager
    def _connect() -> Iterator[Any]:
        yield _Session()

    found = _observable(metrics.RelayDepthCollector(_connect))

    assert found == {metrics.REASON_ROW_LEVEL_SECURITY: 0.0}, found
    assert statements, "the collector issued no SQL at all, so this test measured nothing"
    assert "statement_timeout" in statements[0], (
        f"the first statement of a depth read was {statements[0]!r}. It must be the "
        "`SET LOCAL statement_timeout`, or every statement before it is unbounded server-side."
    )
    assert str(metrics.DEPTH_STATEMENT_TIMEOUT_MS) in statements[0], (
        f"the statement bound was set to something other than DEPTH_STATEMENT_TIMEOUT_MS "
        f"({metrics.DEPTH_STATEMENT_TIMEOUT_MS}ms): {statements[0]!r}"
    )


def test_the_budget_is_derived_from_what_the_heartbeat_tolerates() -> None:
    """The number is not free. It is pinned to the loop it must not starve.

    `DEPTH_SCRAPE_BUDGET_SECONDS` is chosen against two facts that live
    elsewhere and can change without anyone thinking about metrics: the relay's
    5 s tick interval (`worker._parse_interval`'s default) and the staleness
    bound `worker_heartbeat` derives from it. A budget that grew past one tick
    would let a scrape cost the loop a whole tick; one that reached the
    staleness bound would let `/metrics` alone trigger the `livenessProbe`.

    This test is why the justification cannot rot silently: raise the budget, or
    shorten the tick, and it fails with the reason attached.
    """
    tick = 5.0  # `worker._parse_interval(os.environ.get("WORKER_INTERVAL_SECONDS", "5"))`
    bound = worker_heartbeat.staleness_bound(tick, Settings().worker_heartbeat_stale_multiple)

    assert bound == 30.0, f"the staleness bound moved to {bound}s; re-derive the scrape budget against it"
    assert metrics.DEPTH_SCRAPE_BUDGET_SECONDS <= tick, (
        f"the scrape budget ({metrics.DEPTH_SCRAPE_BUDGET_SECONDS}s) exceeds one relay tick ({tick}s), "
        "so a single scrape can cost the loop more than one tick."
    )
    assert metrics.DEPTH_SCRAPE_BUDGET_SECONDS * 6 <= bound, (
        f"the scrape budget ({metrics.DEPTH_SCRAPE_BUDGET_SECONDS}s) is more than a sixth of the "
        f"{bound}s staleness bound. Six consecutive worst-case scrapes would then account for the "
        "entire budget the heartbeat has, and /metrics could restart a working worker on its own."
    )
    # 2s connect (libpq's floor) + 4 statements x DEPTH_STATEMENT_TIMEOUT_MS is
    # what the deadline is the backstop FOR. If the inner bounds ever sum past
    # it, the deadline stops being a backstop and becomes the only bound —
    # abandoning work that nothing else will stop.
    inner = Settings().db_connect_timeout_seconds + 4 * (metrics.DEPTH_STATEMENT_TIMEOUT_MS / 1000.0)
    assert inner <= metrics.DEPTH_SCRAPE_BUDGET_SECONDS, (
        f"connect_timeout + 4 statement timeouts is {inner}s, past the {metrics.DEPTH_SCRAPE_BUDGET_SECONDS}s "
        "deadline. The deadline would then always fire first and the abandoned reading would keep its "
        "connection with nothing server-side to end it."
    )


def test_the_engine_bounds_its_connect(monkeypatch) -> None:  # noqa: ANN001
    """`connect_timeout` reaches libpq. It had NO value at all, which means wait forever.

    The deadline above stops the scrape waiting; it does not stop the abandoned
    connect. Without this, a reading the deadline walked away from sits in
    `pool._create_connection` holding a slot until the kernel gives up on the
    TCP handshake — minutes, on a black-holed route — and single-flight then
    refuses every scrape for that whole time.

    ⚠️ It is NOT the bound that fixed the measured hang, and the docstrings say
    so: psycopg3 resolves the host in Python before libpq is handed anything, so
    `getaddrinfo` (30.016 s, measured) runs entirely outside it. Two different
    failure modes, two different bounds; this test pins the one Postgres can see.
    """
    import database

    captured: dict[str, Any] = {}

    def _spy(url: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "create_engine", _spy)

    settings = Settings()
    database.get_engine(settings)

    assert captured["connect_args"].get("connect_timeout") == settings.db_connect_timeout_seconds, (
        f"the engine was built with connect_args={captured.get('connect_args')!r}. Without "
        "`connect_timeout` libpq waits forever for a connect, holding a pool slot the relay tick "
        "needs — and the worker's pool is 2 (1 in production)."
    )
    assert settings.db_connect_timeout_seconds >= 2, (
        f"connect_timeout is {settings.db_connect_timeout_seconds}s. libpq's documented floor is 2s "
        "and it silently raises anything smaller, so a value below 2 states a bound that is not real."
    )
