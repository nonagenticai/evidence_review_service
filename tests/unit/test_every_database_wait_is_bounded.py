"""Every database wait in this service is BOUNDED. Not just the one that was.

## The class, not the site

`d1707670` bounded `metrics.py` after measuring a 30.016 s scrape. Nothing else
was bounded, and the same mechanism was live everywhere else:

* `api/routers/health.ready()` — `session_scope()` -> `SELECT 1`, `except
  Exception` at the call site;
* `worker.tick()` — two unbounded `session_scope()`s on the relay loop;
* `worker.preflight()` — an unbounded `session_scope()` before the loop starts;
* every `actor_scope()` in `api/routers/` — no `statement_timeout` at all.

⛔ **The root cause is that `connect_timeout` is not a bound.** psycopg3 resolves
the host in Python (`_conninfo_attempts._resolve_hostnames` ->
`socket.getaddrinfo`) BEFORE libpq is handed anything, so a stalled resolver
blocks entirely outside it — measured at **30.016 s**. And a hang RAISES
NOTHING, so the `except Exception` guarding each of those sites could never
fire. The code looked defended and was not.

## ⛔ What is NOT bounded, and why the title overclaims

Three sites get a wall-clock deadline: the readiness probe, the relay tick, and
preflight. Those are the three that can restart or wedge a pod. The **52**
`actor_scope` call sites in `api/routers/` get the server-side
`statement_timeout` they never had and **no deadline** — so a stalled resolver
still blocks a route for the full 30.016 s, and because all 52 are plain `def`
handlers on the anyio limiter `app.py` caps to 15, fifteen of them stall
`health.ready()` out of ever being SCHEDULED, defeating its 3.0 s bound from
outside. `database.actor_scope` documents that residual in full, to the same
standard as the JWKS one in `auth.verifier`. It is deliberately not fixed here.

The defensible claim is "every database wait that can restart or wedge a pod is
bounded, and every remaining wait has a server-side ceiling it did not have".

## What these tests pin

⛔ **They assert ELAPSED TIME.** Every one of these paths reports the same thing
under a stall as under a refusal — `database: error: …`, a failed tick, a
preflight problem — so only the clock can tell "gave up promptly" from
"answered eventually". Asserting that a helper was called would pass against the
defect, because the helper was always called; it just never came back.

Two of them cost real wall-clock seconds at the PRODUCTION budget, deliberately.
A test that measures a budget the service does not ship is worth nothing.

## Production evidence, for the tick

`evidence-review-worker` restarted twice on 2026-09-07 between 18:50 and 19:00
UTC — exit 137 after `shutdown requested by signal 15`, an in-place liveness
kill. Eleven namespaces restarted in the same window, so the blip was fleet-wide
and not this service's doing. **The worker should still have survived it**: the
unbounded tick held past the 30 s heartbeat staleness bound and the
`livenessProbe` restarted a worker that was working.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from anyio import to_thread as anyio_to_thread

import bounded_db
import worker
import worker_heartbeat
from actor import Actor, ActorRole
from api.routers import health
from auth import verifier
from bounded_db import DatabaseBudgetExceededError, DatabaseBusyError, SingleFlight, run_bounded
from config import Settings

#: How much longer than the budget a bounded call may take before this suite
#: calls it unbounded. Generous: the thread hand-off, the GIL and a loaded CI
#: runner all land in here, and a test that fails on scheduling noise gets muted
#: rather than fixed. It is still far below the 30 s the defect actually cost.
SLACK_SECONDS = 2.0

#: How long a wedged fake database blocks before giving up on its own.
#:
#: ⚠️ Finite ON PURPOSE, and the value is chosen against the mutants rather than
#: against the tests. `metrics.py`'s suite blocks forever and relies on the
#: bound existing to get control back — which is correct for a passing run and
#: useless for a mutation run: delete the bound and the suite hangs rather than
#: failing, and a hang is not a kill you can count. 60 s is far above every
#: budget here (3.0, 15.0, 20.0), so it can never mask one; it is finite, so a
#: removed bound reports `elapsed 60s` against a stated budget instead of
#: wedging the session.
WEDGE_MAX_SECONDS = 60.0

#: ⛔ **A MANUALLY CROSS-CHECKED PIN. It is a copy, and it cannot detect drift.**
#:
#: The value it copies is the API Deployment's `readinessProbe.timeoutSeconds`:
#:
#: * file — `gitops-production/clusters/strato/services/evidence-review/`
#:   `deployment.yaml`, lines 149-160 (`timeoutSeconds: 5`, beside
#:   `periodSeconds: 10` and `failureThreshold: 3`)
#: * introduced by — `07017883d`, 2026-08-08, "Deploy Evidence Review 8810
#:   (evidence-review namespace) (#2478)"; unchanged since
#: * read at — `gitops-production` HEAD `985e030a6`, **checked 2026-09-08**
#:
#: ⚠️ **What this constant does NOT do, despite how it reads at the assert
#: sites.** It is not a read of the manifest. The manifest is in another
#: repository which is never checked out here — CI consumes
#: `the fleet's python-service archetype` as
#: a reusable WORKFLOW, not as a tree — so nothing in this suite can see the
#: file at runtime. If somebody drops `timeoutSeconds` to 2 over there, every
#: test below still passes and `READY_PROBE_BUDGET_SECONDS = 3.0` silently stops
#: being a bound. **That drift is undetectable from this repository.**
#:
#: ## Why a pin, and not a mechanism
#:
#: The two mechanisms available were both considered and both rejected:
#:
#: * **Read the manifest when a sibling checkout happens to exist** (dev
#:   machines have one; CI does not). It would run nowhere that gates a merge,
#:   while making the claim look live at the only place anyone reads it. A check
#:   that skips in CI is an unexercised claim wearing the costume of a passing
#:   one — strictly worse than an honest literal, because a literal at least
#:   admits what it is.
#: * **Vendor a copy of the manifest into this repo.** That moves the drift one
#:   directory over; the copy rots exactly as this number does, and now silently,
#:   because a stale YAML file reads as authoritative.
#:
#: ⛔ The mechanism that would actually close it belongs on the OTHER side, where
#: the value lives: a comment on `readinessProbe.timeoutSeconds` in
#: `deployment.yaml` naming `health.READY_PROBE_BUDGET_SECONDS` as its consumer,
#: so an operator editing it is told what it constrains. That is a change to
#: `gitops-production` and needs its own PR; it is recorded here rather than
#: implied to have been done.
#:
#: ## What the tests below therefore DO prove
#:
#: The half of the derivation that lives in THIS repository: that
#: `health.READY_PROBE_BUDGET_SECONDS` still fits inside the kubelet timeout as
#: last checked, and that its inner bounds still sum below it. Raising the
#: budget to 4.9 here is caught. Lowering the timeout over there is not.
PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS = 5.0

#: `WORKER_INTERVAL_SECONDS` as the worker Deployment declares it, and as
#: `worker._parse_interval` defaults it.
RELAY_TICK_INTERVAL_SECONDS = 5.0


@pytest.fixture
def gate() -> Iterator[threading.Event]:
    """Released at teardown so each abandoned daemon thread can finish and exit."""
    event = threading.Event()
    try:
        yield event
    finally:
        event.set()


@contextmanager
def _wedged(gate: threading.Event) -> Iterator[Any]:
    """A scope that never yields a session and never raises.

    The whole class of failure in one line: `getaddrinfo` against an unreachable
    resolver, a TCP connect into a black hole, a `SELECT` behind a lock. None of
    them can be interrupted from the calling thread, which is why the only
    ceiling is to stop waiting.
    """
    gate.wait(WEDGE_MAX_SECONDS)
    yield None  # pragma: no cover - reached only after the gate is released


@pytest.fixture
def fresh_probe_flight(monkeypatch: pytest.MonkeyPatch) -> SingleFlight:
    """A per-test single-flight guard for the readiness probe.

    `health._PROBE_FLIGHT` is module-level by design — it has to outlive the
    request that created it — so a test that abandons a reading would leave the
    real guard busy for every test after it. Replacing it is the isolation;
    returning it is what lets one test assert on it directly.
    """
    flight = SingleFlight()
    monkeypatch.setattr(health, "_PROBE_FLIGHT", flight)
    return flight


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode())


# ─────────────────────────────────────────────────────────────────────────────
# Site 1 — the readiness probe
# ─────────────────────────────────────────────────────────────────────────────


def test_the_readiness_probe_answers_inside_its_budget_against_a_database_that_never_answers(
    gate: threading.Event, fresh_probe_flight: SingleFlight, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline property for site 1, at the PRODUCTION budget.

    No budget override: this test is worth having only if the number it measures
    is the number the pod ships with. It costs `READY_PROBE_BUDGET_SECONDS` of
    wall clock, which is the point.

    The lower bound matters as much as the upper one. A probe that reported
    `not_ready` without attempting anything would satisfy "fast" and be a
    permanent lie, so this asserts it actually waited its budget.
    """
    monkeypatch.setattr(health, "session_scope", lambda *a, **k: _wedged(gate))  # noqa: ARG005

    started = time.monotonic()
    response = health.ready()
    elapsed = time.monotonic() - started

    assert elapsed <= health.READY_PROBE_BUDGET_SECONDS + SLACK_SECONDS, (
        f"the readiness probe took {elapsed:.2f}s against a database that never answered. The budget "
        f"is {health.READY_PROBE_BUDGET_SECONDS}s and it is not advisory: `ready()` is a plain `def`, "
        "so it holds one of the anyio threadpool tokens that `app.py` caps to `pool_size + "
        "max_overflow` — a stalled probe starves the request handling it is supposed to report on."
    )
    assert elapsed >= health.READY_PROBE_BUDGET_SECONDS * 0.8, (
        f"the probe gave up after {elapsed:.2f}s, well short of its {health.READY_PROBE_BUDGET_SECONDS}s "
        "budget. A probe that refuses without attempting is not bounded, it is broken: it would report "
        "not_ready against a perfectly healthy database."
    )
    assert response.status_code == 503, response.status_code
    body = _body(response)
    assert body["checks"]["database"] == f"error: {DatabaseBudgetExceededError.__name__}", (
        f"the probe reported {body['checks']['database']!r}. A bounded refusal must NAME itself, or an "
        "operator cannot tell a database that said no from one that said nothing."
    )


def test_a_second_probe_does_not_open_a_second_connection_while_one_is_outstanding(
    gate: threading.Event, fresh_probe_flight: SingleFlight, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flight. The half that protects the POOL rather than the probe.

    Bounding one probe is not enough. kubelet re-probes every 10 s and does not
    wait for the last answer, and the deadline does not stop the abandoned
    thread — it keeps its connection until `connect_timeout` ends it. Against a
    resolver that takes 30 s to fail, an unguarded probe therefore has three
    connections outstanding before the first is returned, out of a pool of 15
    that `app.py` has also made the ceiling on concurrent request handlers.

    Measured two ways, because either alone passes for the wrong reason: the
    second probe is fast (so it did not wait) and only one scope was ever opened
    (so it did not merely wait less).
    """
    opened: list[int] = []

    def _counting_scope(*_args: Any, **_kwargs: Any) -> Any:
        opened.append(1)
        return _wedged(gate)

    monkeypatch.setattr(health, "session_scope", _counting_scope)
    monkeypatch.setattr(health, "READY_PROBE_BUDGET_SECONDS", 0.5)

    assert _body(health.ready())["checks"]["database"] == f"error: {DatabaseBudgetExceededError.__name__}"

    started = time.monotonic()
    second = health.ready()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25, (
        f"the second probe waited {elapsed:.2f}s while the first reading was still outstanding. It "
        "must refuse immediately: waiting again is what lets kubelet's own cadence accumulate "
        "connections until no request handler can get one."
    )
    assert _body(second)["checks"]["database"] == f"error: {DatabaseBusyError.__name__}", (
        f"the refused probe reported {_body(second)['checks']['database']!r}. `DatabaseBusyError` is a "
        "materially different fact from `DatabaseBudgetExceededError` — this caller waited for nothing — "
        "and collapsing them hides that the pod is shedding probes rather than timing out on them."
    )
    assert opened == [1], (
        f"{len(opened)} scopes were opened for 2 probes. At most ONE connection may ever be held on "
        "behalf of /health/ready."
    )


def test_the_probe_recovers_after_the_thread_it_needs_could_not_be_started(
    fresh_probe_flight: SingleFlight, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ A guard acquired without a worker thread would never be released again.

    `run_bounded` acquires the single-flight and then starts the daemon thread
    whose `finally` is the ONLY release. Under thread exhaustion
    `Thread.start()` raises `RuntimeError: can't start new thread` — the
    acquire has happened, the `finally` never will, and the guard is busy for
    the life of the process.

    ## Why that is worse than the stall it guards

    Measured on the branch tip before this was fixed, against a database that
    answers instantly::

        probe under thread exhaustion -> 503 error: RuntimeError
        guard busy after : True
        probe #1 after threads recover -> 503 error: DatabaseBusyError
        probe #2 after threads recover -> 503 error: DatabaseBusyError
        probe #3 after threads recover -> 503 error: DatabaseBusyError

    ⚠️ And nothing collects the pod. `/health/live` is `async` and touches
    nothing, so it keeps returning 200 while `/health/ready` is permanently 503;
    kubelet marks the pod NotReady and never restarts it. It sits there, out of
    the Service, blaming the database.

    That shape is NEW with this change — before it, the same exhaustion drained
    the fifteen anyio tokens and killed the API, which at least got the pod
    replaced — which is why it is fixed rather than noted.

    ## What is asserted

    The OBSERVABLE outcome: a later probe against a healthy database reports
    `ok`. Asserting that `release()` was called would pass against a
    re-acquisition bug and says nothing about what an operator would see.
    """
    answered: list[int] = []

    @contextmanager
    def _healthy_scope(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
        answered.append(1)

        class _Session:
            def execute(self, *_a: Any, **_k: Any) -> None:
                return None

        yield _Session()

    monkeypatch.setattr(health, "session_scope", _healthy_scope)

    class _NoThreads:
        """`bounded_db`'s view of `threading`, with the interpreter out of OS threads.

        The module global is replaced rather than `threading.Thread` itself:
        patching the real attribute would deny a thread to pytest, the anyio
        backend and every other test running beside this one for the duration.
        """

        Event = threading.Event
        Lock = threading.Lock

        class Thread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("can't start new thread")

    monkeypatch.setattr(bounded_db, "threading", _NoThreads)
    starved = health.ready()
    monkeypatch.setattr(bounded_db, "threading", threading)

    assert starved.status_code == 503, (
        f"the probe returned {starved.status_code} while it could not start the thread its bound "
        "depends on. A probe that cannot run its own check must not report ready."
    )
    assert _body(starved)["checks"]["database"] == "error: RuntimeError", (
        f"the starved probe reported {_body(starved)['checks']['database']!r}. The failure to get a "
        "thread must reach the operator as itself, not be laundered into a database verdict."
    )
    assert answered == [], "no scope should have been opened: the thread that would open one never ran."

    recovered = health.ready()

    assert _body(recovered)["checks"]["database"] == "ok", (
        f"a probe against a healthy database reported {_body(recovered)['checks']['database']!r} after "
        "an earlier probe failed to start its thread. The single-flight guard was acquired and never "
        "released, so /health/ready is now permanently 503 while /health/live stays 200 — the pod is "
        "NotReady forever and kubelet has no reason to replace it."
    )
    assert recovered.status_code == 200, recovered.status_code
    assert not fresh_probe_flight.busy, (
        "the guard is still busy after a probe that never started a worker thread. Its only release "
        "lives in that thread's `finally`, so nothing will ever run it."
    )


def test_the_probe_budget_fits_inside_the_kubelet_timeout_it_is_derived_from() -> None:
    """The half of the derivation this repository can actually enforce.

    ⚠️ **This test does not prevent manifest drift, and an earlier version of
    this docstring said it did.** `READY_PROBE_BUDGET_SECONDS` is chosen against
    `readinessProbe.timeoutSeconds: 5` in
    `gitops-production/clusters/strato/services/evidence-review/deployment.yaml`
    — another repository, never checked out here, unreadable at runtime in CI.
    :data:`PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS` is a manually
    cross-checked COPY of it, and that constant carries the provenance, the date
    it was checked, and why a copy is the honest form. Read it before trusting
    this test for more than it does.

    What IS enforced: the local half. A budget at or above the pinned timeout is
    not a bound at all — kubelet gives up first and the diagnosis `ready()`
    writes into its body is never read by anything — so raising
    `READY_PROBE_BUDGET_SECONDS` in this repository fails here with the reason
    attached. Lowering `timeoutSeconds` in the other one does not.
    """
    assert health.READY_PROBE_BUDGET_SECONDS < PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS, (
        f"the probe budget ({health.READY_PROBE_BUDGET_SECONDS}s) is not below the pinned kubelet "
        f"probe timeout ({PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS}s). kubelet would abandon the "
        "request first, so the `database: error: …` this handler writes would never be delivered — a "
        "bound whose report nothing waits to read."
    )
    # The handler does a second thing after the database check — the export-seal
    # import and count — plus the JSON render and the round trip. Leaving under
    # half the kubelet budget for those is the margin, not a rounding.
    assert health.READY_PROBE_BUDGET_SECONDS <= PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS * 0.7, (
        f"the probe budget ({health.READY_PROBE_BUDGET_SECONDS}s) leaves under 30% of the pinned "
        f"{PINNED_KUBELET_READY_PROBE_TIMEOUT_SECONDS}s kubelet timeout for the export-seal check, the "
        "JSON render and the round trip that follow it."
    )
    inner = Settings().db_connect_timeout_seconds + health.READY_PROBE_STATEMENT_TIMEOUT_MS / 1000.0
    assert inner <= health.READY_PROBE_BUDGET_SECONDS, (
        f"connect_timeout + one statement timeout is {inner}s, past the "
        f"{health.READY_PROBE_BUDGET_SECONDS}s deadline. The deadline would then always fire first and "
        "the abandoned reading would keep its connection with nothing server-side to end it."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sites 2 and 3 — every scope bounds its statements, before the first one
# ─────────────────────────────────────────────────────────────────────────────


class _RecordingSession:
    """Records the SQL a scope issues, in order."""

    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def execute(self, clause: Any, params: Any = None) -> Any:  # noqa: ARG002
        self._statements.append(str(clause))
        return None

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`database`'s scopes, wired to a session that records instead of connecting."""
    import database

    statements: list[str] = []
    monkeypatch.setattr(database, "get_session_factory", lambda *a, **k: lambda: _RecordingSession(statements))  # noqa: ARG005
    return statements


def test_session_scope_bounds_its_statements_before_it_issues_any(recorded: list[str]) -> None:
    """`SET LOCAL statement_timeout`, and BEFORE the caller's first statement.

    There was **no `statement_timeout` anywhere in this service outside
    `metrics.py`**, which means a statement could run for as long as Postgres
    was willing to run it, holding a pool slot the whole time.

    Ordering is asserted, not just presence: a bound applied after the caller's
    first `execute` leaves that statement unbounded, and the first statement of
    a transaction is the one that pays for re-establishing a stale pooled
    connection.
    """
    import database

    with database.session_scope(Settings()) as session:
        session.execute("SELECT 1")

    assert recorded, "the scope issued no SQL at all, so this test measured nothing"
    assert "statement_timeout" in recorded[0], (
        f"the first statement of a session_scope was {recorded[0]!r}. It must be the "
        "`SET LOCAL statement_timeout`, or every statement before it is unbounded server-side."
    )
    assert str(Settings().db_statement_timeout_ms) in recorded[0], (
        f"the bound was set to something other than Settings.db_statement_timeout_ms "
        f"({Settings().db_statement_timeout_ms}ms): {recorded[0]!r}"
    )
    assert "SET LOCAL" in recorded[0], (
        f"the bound was issued as {recorded[0]!r}. It must be `SET LOCAL`: a session-scoped `SET` on a "
        "PgBouncer transaction-pooled connection is handed to the NEXT client with the timeout still "
        "on it, which is a bound that leaks onto unrelated work."
    )


def test_actor_scope_bounds_its_statements_before_even_binding_the_actor(recorded: list[str]) -> None:
    """The ~40 route call sites, covered in the one place they all go through.

    Every handler in `api/routers/` opens `actor_scope`. Bounding it there is
    what gives all of them a server-side ceiling in a single edit, instead of
    forty ad-hoc ones that the forty-first route would not have.

    Before `bind_actor`, specifically: binding the actor is itself three round
    trips, and a bound applied after them leaves the tenant GUC handshake — the
    part that runs on every single request — unbounded.
    """
    import database

    actor = Actor(tenant_id=uuid.uuid4(), subject_id="s", role=ActorRole.REVIEWER)
    with database.actor_scope(actor, Settings()):
        pass

    assert recorded, "the scope issued no SQL at all, so this test measured nothing"
    assert "statement_timeout" in recorded[0], (
        f"the first statement of an actor_scope was {recorded[0]!r}. `bind_actor`'s three `set_config` "
        "round trips run on every request in this service and must be inside the bound, not before it."
    )
    assert any("set_config" in statement for statement in recorded[1:]), (
        "actor_scope stopped binding the actor. The statement bound must be ADDED before the binding, "
        "never substituted for it: an unbound `app.current_tenant` makes every RLS policy match "
        "nothing, which reads as an empty database rather than as an error."
    )


def test_a_scope_override_is_what_the_bounded_paths_pass(recorded: list[str]) -> None:
    """The tighter values are real, not decoration.

    The probe and the relay tick each pass a `statement_timeout_ms` far below
    the service default, derived from what they must fit inside. If the override
    were ignored the default would silently apply, and the sum that makes each
    deadline a backstop rather than the only bound would no longer hold.
    """
    import database

    with database.session_scope(Settings(), statement_timeout_ms=123):
        pass

    assert "123" in recorded[0], (
        f"the scope ignored its statement_timeout_ms override and issued {recorded[0]!r}. The bounded "
        "paths would then all be running at the 30s service default."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Site 5 — the relay tick, and the preflight before it
# ─────────────────────────────────────────────────────────────────────────────


def test_a_relay_tick_against_a_database_that_never_answers_returns_inside_the_budget(
    gate: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site with production evidence, at the PRODUCTION budget.

    No override: 15 s of wall clock, because the property is worth nothing
    measured against a number the worker does not ship. This is the tick that
    got a working worker restarted twice on 2026-09-07.

    It must RAISE rather than return a null result. `run()` catches it, leaves
    `completed` False and does not advance the heartbeat — which is what any
    other failed tick does. A tick that returned `(0, 0, 0)` after giving up
    would advance the heartbeat and report a relay that is draining an empty
    queue, when the truth is that it never reached the queue at all.
    """
    monkeypatch.setattr(worker, "session_scope", lambda *a, **k: _wedged(gate))  # noqa: ARG005

    started = time.monotonic()
    with pytest.raises(DatabaseBudgetExceededError):
        worker.tick(Settings())
    elapsed = time.monotonic() - started

    assert elapsed <= worker.RELAY_TICK_BUDGET_SECONDS + SLACK_SECONDS, (
        f"the relay tick took {elapsed:.2f}s against a database that never answered. Past the "
        f"{worker_heartbeat.staleness_bound(RELAY_TICK_INTERVAL_SECONDS, Settings().worker_heartbeat_stale_multiple)}s "
        "staleness bound the livenessProbe restarts a worker that is working — measured in production "
        "on 2026-09-07, twice, during a blip that eleven other namespaces survived."
    )
    assert elapsed >= worker.RELAY_TICK_BUDGET_SECONDS * 0.8, (
        f"the tick gave up after {elapsed:.2f}s, well short of its {worker.RELAY_TICK_BUDGET_SECONDS}s "
        "budget. A tick that abandons without attempting would fail the heartbeat against a healthy "
        "database, which is the restart it exists to prevent."
    )


def test_the_two_transactions_of_a_tick_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget per transaction would state one bound and deliver two.

    `tick` runs the relay and the reaper in separate transactions on purpose — a
    reaper failure must not roll back a relay that already succeeded. But it is
    the PAIR that has to fit inside the heartbeat's staleness bound, so the
    deadline is around both. Bounding each separately would pass every "is it
    bounded" test while allowing `2 x RELAY_TICK_BUDGET_SECONDS`, which is
    30 s — exactly the number the whole finding is about.

    Measured by making the FIRST transaction consume most of the budget and
    asserting the second one inherits what is left rather than starting fresh.
    """
    budget = 2.0
    spent_by_the_first = 1.8
    monkeypatch.setattr(worker, "RELAY_TICK_BUDGET_SECONDS", budget)

    entered: list[int] = []

    @contextmanager
    def _slow_then_wedged(*_args: Any, **_kwargs: Any) -> Iterator[Any]:
        entered.append(1)
        if len(entered) == 1:
            time.sleep(spent_by_the_first)
            yield _RecordingSession([])
        else:
            time.sleep(WEDGE_MAX_SECONDS)
            yield None  # pragma: no cover - the deadline fires first

    monkeypatch.setattr(worker, "session_scope", _slow_then_wedged)
    monkeypatch.setattr(worker.relay, "relay_pending", lambda _s: worker.relay.RelayResult(0, 0))

    started = time.monotonic()
    with pytest.raises(DatabaseBudgetExceededError):
        worker.tick(Settings())
    elapsed = time.monotonic() - started

    # ⚠️ The slack here is 1.0 s rather than SLACK_SECONDS, and that is what
    # makes the test able to fail. A per-transaction deadline would take
    # `spent_by_the_first + budget` = 3.8 s; the generous 2 s slack would have
    # admitted that as "bounded" and the mutant would have survived.
    assert elapsed <= budget + 1.0, (
        f"the tick took {elapsed:.2f}s for a {budget}s budget after its first transaction had already "
        f"spent {spent_by_the_first}s of it. The second transaction started its own deadline instead "
        "of inheriting the remainder, so the real worst case is twice the stated budget — which at "
        "the shipped 15s budget is the 30s staleness bound exactly."
    )
    assert entered == [1, 1], f"the tick opened {len(entered)} transactions; it must open exactly 2"


def test_the_relay_tick_budget_leaves_room_for_a_retry_inside_the_staleness_bound() -> None:
    """The number is derived from the heartbeat, and the derivation is the test.

    `RELAY_TICK_BUDGET_SECONDS` is chosen against two facts that live elsewhere
    and can move without anyone thinking about the relay: the 5 s tick interval
    (`worker._parse_interval`'s default, and the worker Deployment's
    `WORKER_INTERVAL_SECONDS`) and the staleness bound `worker_heartbeat`
    derives from it.

    "Under the staleness bound" is not enough on its own, and that is the point
    of this test rather than a simple `<`. A stalled tick does not advance the
    heartbeat; only the NEXT successful one does. So the budget must leave room
    for the failure, the sleep after it, and a retry — all inside the bound.
    """
    interval = RELAY_TICK_INTERVAL_SECONDS
    bound = worker_heartbeat.staleness_bound(interval, Settings().worker_heartbeat_stale_multiple)

    assert bound == 30.0, f"the staleness bound moved to {bound}s; re-derive the tick budget against it"
    assert worker.RELAY_TICK_BUDGET_SECONDS < bound, (
        f"the tick budget ({worker.RELAY_TICK_BUDGET_SECONDS}s) is not below the {bound}s staleness "
        "bound, so one stalled tick can still get a working worker restarted."
    )
    # t0 last success -> +interval the tick starts -> +budget it is abandoned ->
    # +interval the retry starts and, against a recovered database, completes in
    # milliseconds. That instant must be inside the bound.
    retry_completes_at = 2 * interval + worker.RELAY_TICK_BUDGET_SECONDS
    assert retry_completes_at < bound, (
        f"a stalled tick plus one retry advances the heartbeat at {retry_completes_at}s, past the "
        f"{bound}s bound. The budget ({worker.RELAY_TICK_BUDGET_SECONDS}s) is under the bound but "
        "leaves no room for the retry that is the only thing that can advance the heartbeat — so a "
        "single blip still restarts the pod, which is the entire defect."
    )


def test_preflight_reports_a_database_that_never_answers_instead_of_hanging(
    gate: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung start-up is a pod that never says why it was killed.

    Unbounded, `preflight` blocks before the first heartbeat is ever written;
    the `livenessProbe` kills the pod at roughly `failureThreshold: 4` x
    `periodSeconds: 15`, and the log contains nothing about the database.

    Bounded, it becomes a PROBLEM in the list preflight already returns — which
    matters because preflight's contract is to report every reason at once. A
    raise here would have discarded the interval problem collected above it and
    made an operator fix one thing, restart, and learn about the next.
    """
    monkeypatch.setattr(worker, "session_scope", lambda *a, **k: _wedged(gate))  # noqa: ARG005
    monkeypatch.setattr(worker, "PREFLIGHT_BUDGET_SECONDS", 0.5)
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", "not-a-number")

    started = time.monotonic()
    problems = worker.preflight(Settings())
    elapsed = time.monotonic() - started

    assert elapsed <= 0.5 + SLACK_SECONDS, (
        f"preflight took {elapsed:.2f}s against a database that never answered. Unbounded it does not "
        "fail, it HANGS, and a pod killed while hanging logs nothing about why."
    )
    assert any("did not answer within" in problem for problem in problems), (
        f"preflight returned {problems!r}. An unreachable database is a reason this process cannot do "
        "its job and must be reported as one."
    )
    assert any("not a number" in problem for problem in problems), (
        f"preflight returned {problems!r}, losing the WORKER_INTERVAL_SECONDS problem it had already "
        "collected. Reporting every problem at once is the whole contract; a budget failure that "
        "escapes as an exception discards the others."
    )


# ─────────────────────────────────────────────────────────────────────────────
# The mechanism itself
# ─────────────────────────────────────────────────────────────────────────────


def test_a_bounded_call_is_transparent_to_what_the_work_returns_and_raises() -> None:
    """The deadline adds exactly ONE outcome and changes no other.

    Every call site already handles database errors. If `run_bounded` swallowed
    a `ProgrammingError` into a timeout, or returned `None` where the work
    returned a row, it would have replaced an unbounded wait with a wrong
    answer — which is worse, because a wrong answer is not visible.
    """
    assert run_bounded(lambda: {"rows": 3}, budget_seconds=5.0, name="t") == {"rows": 3}
    assert run_bounded(lambda: None, budget_seconds=5.0, name="t") is None

    sentinel = ValueError("the database said no")

    def _raises() -> None:
        raise sentinel

    with pytest.raises(ValueError) as caught:
        run_bounded(_raises, budget_seconds=5.0, name="t")
    assert caught.value is sentinel, (
        "run_bounded re-raised something other than the exception the work raised. A caller that "
        "branches on the database error it expects would stop seeing it."
    )


def test_the_single_flight_guard_is_released_by_the_thread_that_owns_it(gate: threading.Event) -> None:
    """Released in the WORKER thread's `finally`, never in the caller's.

    The caller leaves first by construction — that is what the deadline is. A
    release on the caller's side would therefore re-admit the next call while
    the abandoned one still holds its connection, which is precisely the
    accumulation single-flight exists to prevent.

    So: still busy immediately after the caller gives up, free once the
    abandoned work actually finishes.
    """
    flight = SingleFlight()

    def _wedged_work() -> None:
        gate.wait(WEDGE_MAX_SECONDS)

    with pytest.raises(DatabaseBudgetExceededError):
        run_bounded(_wedged_work, budget_seconds=0.3, name="t", single_flight=flight)

    assert flight.busy, (
        "the guard was released when the CALLER gave up, while the abandoned thread still held its "
        "connection. The next call would then open a second one — the accumulation this prevents."
    )

    gate.set()
    deadline = time.monotonic() + 5.0
    while flight.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not flight.busy, (
        "the guard was never released by the worker thread, so every later call refuses forever. A "
        "permanent refusal is not a bound, it is an outage with a tidier message."
    )


def test_a_start_that_raises_after_the_thread_is_live_admits_no_second_caller(
    gate: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ `Thread.start()` returning is NOT "the runner owns the release" yet.

    The branch that releases the guard when `start()` raises was justified by
    the claim that `start()` is the last statement in its `try`, so once it
    returns `_runner` owns the release and the branch cannot also run. **CPython
    does not provide that invariant.** `threading.py:1003-1011` in the
    interpreter this suite runs on (3.14.3) is::

        try:
            _start_joinable_thread(self._bootstrap, handle=..., daemon=...)
        except Exception:
            with _active_limbo_lock:
                del _limbo[self]
            raise
        self._started.wait()          # <- line 1011, OUTSIDE the try

    `_started.wait()` is outside the `try`, so `start()` can raise with the
    runner **already live and already doing the work**. A release keyed on
    "start() raised" then frees the guard while a connection is outstanding, and
    the next caller opens a second one — the exact accumulation `SingleFlight`
    exists to prevent. Measured on the tip before this fix::

        work still outstanding                                  = True
        guard busy while work outstanding                       = False
        a SECOND caller acquired while the first is outstanding = True

    ## Why this is modelled rather than provoked

    Reaching line 1011 for real needs `_started.wait()` to be interrupted, which
    means a `KeyboardInterrupt` delivered to the main thread inside a window of
    microseconds. Modelling the raise is the same fact without the flake, and it
    is honest about being a model: what is asserted below is the OBSERVABLE
    consequence, not that any particular line ran.

    ⚠️ Not reachable from the deployed API *today* — the only `single_flight=`
    caller is `health.ready()`, always on an anyio worker thread, and
    `KeyboardInterrupt` only reaches the main thread. It becomes reachable the
    moment a main-thread caller passes a guard; `worker.preflight()` is the
    obvious candidate. This pins it before that happens, not after.
    """
    flight = SingleFlight()
    entered = threading.Event()
    opened: list[int] = []

    def _outstanding_work() -> None:
        opened.append(1)
        entered.set()
        gate.wait(WEDGE_MAX_SECONDS)

    real_thread = threading.Thread

    class _StartRaisesAfterTheRunnerIsLive(real_thread):  # type: ignore[valid-type,misc]
        """`start()` raising from `threading.py:1011`, with the runner live."""

        def start(self) -> None:
            real_thread.start(self)
            raise RuntimeError("modelled: start() raised at threading.py:1011 (_started.wait)")

    class _LateFailingStart:
        Event = threading.Event
        Lock = threading.Lock
        Thread = _StartRaisesAfterTheRunnerIsLive

    monkeypatch.setattr(bounded_db, "threading", _LateFailingStart)
    with pytest.raises(RuntimeError):
        run_bounded(_outstanding_work, budget_seconds=5.0, name="t", single_flight=flight)
    monkeypatch.setattr(bounded_db, "threading", threading)

    # Either the runner won the ownership claim (it is doing the work, so the
    # guard must be HELD) or the caller won it (the runner stood down without
    # touching the database, so there is nothing outstanding to guard). Both are
    # correct; what must never happen is work outstanding with the guard free.
    entered.wait(1.0)
    outstanding = bool(opened) and not gate.is_set()

    assert not (outstanding and not flight.busy), (
        "the guard was released while the work was still outstanding. `start()` raised AFTER the "
        "runner was live, and the release branch assumed that could not happen — so the abandoned "
        "thread holds a connection that the guard no longer accounts for."
    )
    assert not (outstanding and flight.acquire()), (
        "a second caller acquired the guard while an earlier operation was still outstanding. That "
        "is one more connection per call against a stalled dependency, which is how the pool every "
        "request handler shares gets exhausted."
    )
    if not opened:
        assert not flight.busy, (
            "the runner stood down without opening a connection, and the caller did not release the "
            "guard it took. Nothing will ever release it now: the guard is busy for the life of the "
            "process and every later probe reports the database busy."
        )


def test_a_failure_between_acquiring_the_guard_and_starting_the_thread_is_not_a_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is taken, then things are allocated. Both must be inside one `try`.

    `acquire()` used to sit OUTSIDE the `try` that releases on failure, so
    everything between them — the `Event`, the two result lists, the closure —
    could fail with the guard already held and no thread yet born to release it.
    Same permanent leak as a failed `start()`, one line higher, and with the
    same consequence: `/health/ready` 503s `DatabaseBusyError` forever against a
    database answering in milliseconds while `/health/live` keeps returning 200,
    so kubelet leaves the pod NotReady and never replaces it.

    Modelled at `threading.Event()`, which is the first allocation after the
    acquire. On the tip before this fix: `guard busy after = True`,
    `a later caller can acquire = False`.

    What is asserted is the observable outcome — a LATER call runs its work and
    returns — not that `release()` was called.
    """
    flight = SingleFlight()

    class _EventAllocationFails:
        Lock = threading.Lock
        Thread = threading.Thread

        @staticmethod
        def Event() -> threading.Event:  # noqa: N802 - mirrors `threading.Event`.
            raise MemoryError("modelled: allocation failed after the guard was acquired")

    monkeypatch.setattr(bounded_db, "threading", _EventAllocationFails)
    with pytest.raises(MemoryError):
        run_bounded(lambda: "unreachable", budget_seconds=5.0, name="t", single_flight=flight)
    monkeypatch.setattr(bounded_db, "threading", threading)

    assert run_bounded(lambda: "answered", budget_seconds=5.0, name="t", single_flight=flight) == "answered", (
        "a later call was refused after an earlier one failed between acquiring the guard and "
        "starting its thread. The guard was acquired and no thread was ever born to release it, so "
        "it stays busy for the life of the process — a permanent outage reported as a busy database."
    )


def test_the_bounded_thread_is_a_daemon_so_a_wedged_call_cannot_hold_the_interpreter(
    gate: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`daemon=True`, not a `ThreadPoolExecutor`.

    `concurrent.futures` registers an `atexit` hook that JOINS its workers, so a
    single wedged operation would keep the process alive for as long as it
    stayed wedged. A bounded call turned into an unbounded shutdown is the same
    defect wearing a hat — and on the worker it would mean SIGTERM followed by
    SIGKILL and exit 137, which is the signature this whole change is about.
    """
    started: list[threading.Thread] = []
    real = threading.Thread

    def _spy(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = real(*args, **kwargs)
        started.append(thread)
        return thread

    monkeypatch.setattr(bounded_db.threading, "Thread", _spy)

    with pytest.raises(DatabaseBudgetExceededError):
        run_bounded(lambda: gate.wait(WEDGE_MAX_SECONDS), budget_seconds=0.2, name="t")

    assert started, "run_bounded ran the work on the calling thread; nothing can abandon that"
    assert started[0].daemon, (
        "the bounded work runs on a non-daemon thread. Interpreter shutdown joins it, so a wedged "
        "database call would hold the process open until the kernel gave up on the socket."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Site 4 — the JWKS fetch, deliberately NOT bounded. This pins WHY.
# ─────────────────────────────────────────────────────────────────────────────


async def test_verifying_a_token_borrows_no_token_from_the_pool_the_database_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The premise the site-4 decision rests on, measured rather than asserted.

    `auth/verifier._build` has the SAME hole as every site above:
    `PyJWKClient(timeout=10)` reaches `urlopen` -> `socket.create_connection`,
    which calls `getaddrinfo` first, and `getaddrinfo` takes no timeout. The
    unknown-`kid` branch does it twice. It is left unbounded on purpose, because
    a wall-clock abandon on the auth path turns a transient resolver stall
    during a key rotation into a refused authentication — see `_build`'s
    docstring for the full trade.

    ⛔ That decision is only correct while a stalled JWKS fetch costs nothing
    the database paths need. `verify_token_async` uses `asyncio.to_thread`,
    which is `loop.run_in_executor(None, …)` — asyncio's own executor, NOT the
    anyio threadpool that `app.py` caps to `pool_size + max_overflow`. So a
    stalled verification borrows no token, starves no handler and holds no
    connection.

    Move it to `run_in_threadpool` (or to a plain `def` dependency) and that
    stops being true silently: auth would then be able to exhaust the same
    limiter the readiness probe and every route share, and the reasoning in
    `_build` would be wrong with nothing saying so. This test is what says so.
    """
    limiter = anyio_to_thread.current_default_thread_limiter()
    borrowed_while_verifying: list[int] = []

    inside = threading.Event()
    release = threading.Event()

    def _blocking_verify(_token: str, _settings: Settings) -> dict[str, Any]:
        inside.set()
        release.wait(WEDGE_MAX_SECONDS)
        return {"sub": "s"}

    monkeypatch.setattr(verifier, "verify_token", _blocking_verify)

    task = asyncio.ensure_future(verifier.verify_token_async("t", Settings()))
    while not inside.is_set():
        await asyncio.sleep(0.01)
    borrowed_while_verifying.append(limiter.borrowed_tokens)
    release.set()
    await task

    assert borrowed_while_verifying == [0], (
        f"a token verification in flight had {borrowed_while_verifying[0]} anyio threadpool token(s) "
        "borrowed. It must borrow none: `app.py` caps that limiter to the connection pool, so an "
        "unbounded JWKS fetch on it could starve every database handler in the process — and site 4 "
        "is left unbounded precisely because it cannot. Bound the JWKS fetch, or move this back off "
        "the anyio pool."
    )
