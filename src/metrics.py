"""Prometheus export for Evidence Review 8810.

## The measured gap

`up{namespace="evidence-review"}` was EMPTY in the estate's Prometheus. Every
series carrying that namespace came from kube-state-metrics observing the
workloads from outside; nothing was scraped from either process, because neither
served `/metrics` and this repo held no `prometheus_client` import at all. The
consequence is not "no dashboard": it is that no alert about this service could
be written, and `kag`'s `kag_callback_outbox_dead_total` is the estate precedent
for exactly the alert this service lacked.

## ⛔ Two processes, and only one of them can tell the truth

`evidence-review` runs the API; `evidence-review-worker` runs `worker.run()`.
They are separate Deployments and separate processes, and that fact decides the
whole shape of this module.

**The counters cannot cross the process boundary.** `worker._Counters` lives in
the worker's memory. A `Counter` registered in the API process would read `0`
forever — a flat line that reads as *"the relay is healthy and idle"* when the
truth is *"this process has never relayed anything and never will"*. That is
strictly worse than no metric, so the counters are registered ONLY when a
`_Counters` instance is handed in (see :func:`build_registry`), which only
`worker.run()` does. In the API's registry those series are **absent**, and an
absent series cannot be misread.

**The queue depths cannot cross the ROLE boundary.** The depths genuinely are a
database fact, so on the face of it the API could query them on scrape. It
cannot, and the reason is the same one `worker.py`'s preflight exists for:
`domain_outbox`, `research_domain_events` and `event_deliveries` all carry
`FORCE ROW LEVEL SECURITY` with a policy comparing
`current_setting('app.current_tenant', true)::uuid`. A scrape has no request,
therefore no actor, therefore no tenant GUC; the comparand is `NULL`, every
comparison is `NULL`, and `count(*)` is **0 — always, and indistinguishably from
an empty queue.** The one role that sees across tenants is
`evidence_review_worker`, which migrations `0020`/`0022`/`0023` grant an additive
`USING (true)` policy to.

So :class:`RelayDepthCollector` does not assume it can see; it **asks**, on every
scrape, and emits the three depth gauges only when the answer is yes. When the
answer is no it emits no depth sample at all and says why, in
`evidence_review_relay_depth_observable{reason=...} 0`. The same code is then
correct in both processes and in every role configuration — which matters more
than it sounds, because `README.md` records that **nothing binds a process to a
role**: the API and the worker read the same `DATABASE_URL`, and only the worker
checks what it got.

## ⛔ Why the visibility check asks about ROWS and not about a role NAME

The obvious check is `current_user = 'evidence_review_worker'`. It is a proxy,
and this subsystem has been bitten twice by checking a proxy for a property —
`worker.preflight` reads a policy where it needed a GRANT, and `0020` read a
NOTICE where it needed a role. The property here is *"a `SELECT` I issue now
returns every row in this table"*, and Postgres can be asked that directly:
either row-level security is not active for me on the table at all, or a
PERMISSIVE `USING (true)` policy applies to a role I hold — and no RESTRICTIVE
policy narrows it back.

The check fails CLOSED in every ambiguous case (an unknown policy shape, a
restrictive policy, a query that raises). Suppressing a true metric costs a
dashboard; exporting a false zero costs an operator the hours between "the queue
is empty" and "the queue has been stuck since Tuesday".
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, Protocol

from prometheus_client import CollectorRegistry, Gauge, PlatformCollector, ProcessCollector
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import text

import worker_heartbeat
from config import Settings, get_settings
from database import session_scope
from log_config import get_logger

logger = get_logger(__name__)

#: Every metric this service exports starts here. One prefix, so a
#: `{__name__=~"evidence_review_.*"}` selector is the whole surface.
PREFIX = "evidence_review"

# --------------------------------------------------------------------------
# Names. Declared as constants because the alert rules in `gitops-production`
# will name them verbatim, and a metric renamed in code and not in a rule is an
# alert that silently stops firing — which is the failure this module exists to
# end, wearing a different hat.
# --------------------------------------------------------------------------

#: `CounterMetricFamily` appends `_total` itself, so these are the BASE names.
#: The exported series are `<base>_total`.
TICKS = f"{PREFIX}_worker_ticks"
RELAYED = f"{PREFIX}_relay_events_relayed"
DELIVERIES = f"{PREFIX}_relay_deliveries_created"
POISONED = f"{PREFIX}_relay_deliveries_poisoned"
REAPED = f"{PREFIX}_worker_leases_reaped"
ERRORS = f"{PREFIX}_worker_tick_errors"

OUTBOX_UNRELAYED = f"{PREFIX}_relay_outbox_unrelayed_rows"
DELIVERIES_PENDING = f"{PREFIX}_relay_deliveries_pending_rows"
DELIVERIES_POISONED = f"{PREFIX}_relay_deliveries_poisoned_rows"
DEPTH_OBSERVABLE = f"{PREFIX}_relay_depth_observable"

HEARTBEAT_AGE = f"{PREFIX}_worker_heartbeat_age_seconds"
HEARTBEAT_BOUND = f"{PREFIX}_worker_heartbeat_staleness_bound_seconds"
HEARTBEAT_STATUS = f"{PREFIX}_worker_heartbeat_status"

BUILD_INFO = f"{PREFIX}_build_info"

#: The closed vocabulary of `DEPTH_OBSERVABLE`'s `reason` label. Closed on
#: purpose: a label whose values are invented at the call site cannot be alerted
#: on, because nobody can enumerate what it might say.
REASON_OBSERVABLE = "observable"
REASON_ROW_LEVEL_SECURITY = "row_level_security"
REASON_QUERY_FAILED = "query_failed"

#: The three tables a depth query reads. `research_domain_events` is in the list
#: even though no gauge counts it: `unrelayed_count` derives its answer from a
#: `NOT EXISTS` against the log, so a log that is RLS-hidden makes EVERY outbox
#: row look un-relayed. A visibility check that omitted it would license the
#: single most misleading reading this module can produce.
DEPTH_TABLES = ("domain_outbox", "research_domain_events", "event_deliveries")

#: Does a `SELECT` issued right now, by this role, on this table, return every
#: row in it?
#:
#: Two ways for that to be true and one way it must be refused:
#:
#: * `NOT row_security_active(table)` — RLS does not apply to me here at all
#:   (superuser, `BYPASSRLS`, or an owner of a table without `FORCE`).
#: * a PERMISSIVE policy for `ALL`/`SELECT` whose `USING` is literally `true`,
#:   granted to `public` or to a role I hold. This is the `<table>_worker`
#:   policy `0020`/`0022`/`0023` create.
#: * …and NO RESTRICTIVE policy applying to me, because Postgres ANDs those in
#:   after the permissive ones and any one of them can narrow the result again.
#:
#: The role membership test joins `pg_roles` before calling `pg_has_role`
#: deliberately: `pg_policies.roles` contains the pseudo-role `public`, and
#: `pg_has_role(current_user, 'public', 'USAGE')` raises `role "public" does not
#: exist`. The join yields only real roles, so the function is never handed a
#: name it cannot resolve.
_VISIBILITY_SQL = """
SELECT bool_and(
         (NOT row_security_active(t.name))
         OR (
              EXISTS (
                SELECT 1
                  FROM pg_policies p
                 WHERE p.schemaname = 'public'
                   AND p.tablename = t.name
                   AND p.permissive = 'PERMISSIVE'
                   AND p.cmd IN ('ALL', 'SELECT')
                   AND p.qual = 'true'
                   AND (
                        'public' = ANY (p.roles)
                        OR EXISTS (
                             SELECT 1
                               FROM unnest(p.roles) AS r
                               JOIN pg_roles g ON g.rolname = r
                              WHERE pg_has_role(current_user, g.oid, 'USAGE')
                           )
                       )
              )
              AND NOT EXISTS (
                SELECT 1
                  FROM pg_policies p
                 WHERE p.schemaname = 'public'
                   AND p.tablename = t.name
                   AND p.permissive = 'RESTRICTIVE'
              )
            )
       )
  FROM unnest(CAST(:tables AS text[])) AS t(name)
"""

#: `event_deliveries` rows a consumer has not acknowledged. `13-…:§8.2` puts the
#: obligation to make progress on the consumer, so a pending count that only
#: grows is the shape of a consumer that has stopped claiming.
_PENDING_SQL = "SELECT count(*) FROM event_deliveries WHERE state = 'pending'"

#: `event_deliveries` rows the relay gave up on. `13-…:§8.2` says these "page the
#: owning team"; until this module existed the ONLY record of one was a WARNING
#: line in a log nobody tails. This is the counterpart of kag's
#: `kag_callback_outbox_dead_total`.
_POISON_SQL = "SELECT count(*) FROM event_deliveries WHERE poison_reason IS NOT NULL"


class _Counted(Protocol):
    """The shape :class:`WorkerCounterCollector` reads.

    A structural type rather than an import of `worker._Counters`: `worker`
    imports THIS module at start-up, so naming its class here would be a cycle,
    and the six fields are the whole of the contract either way.
    """

    ticks: int
    relayed: int
    deliveries: int
    poisoned: int
    reaped: int
    errors: int


#: `(base name, attribute, help)`. One list, so a counter cannot be added to the
#: worker's tally and forgotten here.
_COUNTERS: tuple[tuple[str, str, str], ...] = (
    (TICKS, "ticks", "Relay + lease-reaper ticks this worker process has COMPLETED."),
    (RELAYED, "relayed", "Outbox rows this worker has moved into the durable event log."),
    (DELIVERIES, "deliveries", "Per-consumer delivery rows this worker has created."),
    (POISONED, "poisoned", "Deliveries this worker recorded as poisoned; each is an event a consumer never receives."),
    (REAPED, "reaped", "Durable-job leases this worker has reaped from workers that stopped heartbeating."),
    (ERRORS, "errors", "Ticks that RAISED. A tick that raises does not advance the heartbeat."),
)


class WorkerCounterCollector(Collector):
    """The worker's own tally, read at scrape time.

    ⛔ It READS `worker._Counters` rather than mirroring it into
    `prometheus_client.Counter` objects incremented beside every `record()`
    call. Mirroring makes two sources of truth for one number, and the failure
    mode of two sources of truth is that they drift silently and the exported
    one is the one nobody checks. There is one tally; this renders it.

    The values are cumulative since process start and monotonic within it, which
    is what a Prometheus counter means. A restart resets them to zero, which is
    also what a Prometheus counter means.
    """

    def __init__(self, counters: _Counted) -> None:
        self._counters = counters

    def collect(self) -> Iterator[Metric]:
        for name, attribute, documentation in _COUNTERS:
            yield CounterMetricFamily(name, documentation, value=float(getattr(self._counters, attribute)))


#: What a depth query needs from a connection. Narrower than `Session` on
#: purpose, so a test can hand in a connection to a DIFFERENT role — which is
#: the only way to exercise both sides of the visibility branch, since the
#: suite's own runtime role is `evidence_review_app` and cannot see across
#: tenants by construction.
Connect = Callable[[], AbstractContextManager[Any]]


#: ⛔ The wall-clock budget for EVERYTHING one scrape does against the database.
#:
#: ## Why a deadline and not just a timeout
#:
#: `collect()` used to wrap its query in `except Exception` and fail closed.
#: That is the right handling for a query that RAISES, and it is no handling at
#: all for a query that never returns: a hang raises nothing, so the guard could
#: not fire. Measured: with `DATABASE_URL` unset, the collector fell back to
#: `Settings`' default DSN and `socket.getaddrinfo("db.example", 5432)`
#: blocked for **30.016 s** before raising `gaierror` — psycopg3 resolves host
#: names in Python (`_conninfo_attempts._resolve_hostnames`) BEFORE libpq is
#: handed anything, so `connect_timeout` never got a chance to apply and the
#: scrape simply stopped. That is the same 30.03 s an audit measured against the
#: live worker.
#:
#: A `/metrics` that can hang is worse than no `/metrics`, and the reason is
#: specific to this service rather than general hygiene. The worker's pool is
#: `DB_POOL_SIZE=2` + `DB_MAX_OVERFLOW=2` (1 + 1 in production today); a scrape
#: blocked inside `pool._create_connection` holds a slot for the whole time, and
#: an audit saw both slots held at once. The relay loop ticks every 5 s and
#: `worker_heartbeat` calls the heartbeat stale at `6 x` the interval — 30 s.
#: So an unbounded scrape can starve the tick past the staleness bound and have
#: the `livenessProbe` restart a WORKING worker: the metric takes down the
#: process it exists to observe.
#:
#: ## Why 5.0 s
#:
#: * It is exactly ONE tick interval (5 s), and one sixth of the 30 s staleness
#:   bound. A scrape that burns the entire budget on every scrape can therefore
#:   cost the loop at most one of the six missed ticks the heartbeat tolerates —
#:   it cannot, by itself, make the heartbeat stale.
#: * It is half of Prometheus's default `scrape_timeout` (10 s), so a bounded
#:   failure is REPORTED as `relay_depth_observable{reason="query_failed"} 0`
#:   rather than as a scrape timeout, which would yield `up 0` and lose the
#:   reason label — the one series this module guarantees is always present.
#: * It is the sum of the two inner bounds, which are what actually stop the
#:   abandoned work: `connect_timeout` 2 s (libpq's floor,
#:   `Settings.db_connect_timeout_seconds`) plus 4 statements x 750 ms.
#:
#: The deadline is not redundant with those two. It is the only bound that
#: covers name resolution, which neither libpq nor Postgres can see.
DEPTH_SCRAPE_BUDGET_SECONDS = 5.0

#: `statement_timeout`, in milliseconds, for the duration of one depth read.
#: Applied with `SET LOCAL`, so it is scoped to this transaction and cannot leak
#: into the relay's own work through the shared engine.
#:
#: 750 ms x the 4 statements a scrape issues (the visibility probe and the three
#: depth queries) is 3 s, which with the 2 s connect bound sums to
#: :data:`DEPTH_SCRAPE_BUDGET_SECONDS`. All four are indexed counts against the
#: relay tables and run in single-digit milliseconds; 750 ms is ~100x headroom,
#: and a depth query that genuinely needs longer than that is reporting a
#: database in trouble, which is exactly when `query_failed` is the true answer
#: and a silently-missing gauge is not.
DEPTH_STATEMENT_TIMEOUT_MS = 750


class _DepthReading:
    """One scrape's database work, on a thread the scrape is allowed to LEAVE.

    ⛔ The thread is the mechanism, not an optimisation. `getaddrinfo`,
    `connect()` and `execute()` are all uninterruptible from the calling thread;
    the only way to put a hard ceiling on them is to stop waiting. So the work
    runs on a daemon thread and `collect()` waits on an `Event` for at most
    :data:`DEPTH_SCRAPE_BUDGET_SECONDS`.

    `daemon=True` rather than a `ThreadPoolExecutor`: `concurrent.futures`
    registers an `atexit` hook that JOINS its workers, so a wedged reading would
    hold the interpreter open for as long as it stayed wedged — turning a
    bounded scrape into an unbounded shutdown, which is the same defect wearing
    a hat. A daemon thread is abandoned at exit.
    """

    __slots__ = ("depths", "done", "error", "reason")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.reason: str | None = None
        self.depths: tuple[int, int, int] | None = None
        self.error: BaseException | None = None

    def run(self, connect: Connect) -> None:
        reason: str | None = None
        depths: tuple[int, int, int] | None = None
        try:
            with connect() as session:
                # The statement bound goes on before anything else runs, so the
                # very first statement is covered too.
                # nosec B608 - a module-owned int constant; `statement_timeout`
                # takes no bind parameter, so this cannot be parameterised.
                session.execute(text(f"SET LOCAL statement_timeout = {int(DEPTH_STATEMENT_TIMEOUT_MS)}"))
                if RelayDepthCollector._can_see_every_row(session):
                    reason = REASON_OBSERVABLE
                    depths = RelayDepthCollector._depths(session)
                else:
                    reason = REASON_ROW_LEVEL_SECURITY
        except BaseException as exc:  # noqa: BLE001 - a collector that raises kills the whole scrape.
            self.error = exc
        else:
            self.reason = reason
            self.depths = depths
        finally:
            self.done.set()


class RelayDepthCollector(Collector):
    """Queue depth, queried on scrape, emitted only when it is true.

    Three gauges — un-relayed outbox rows, `pending` deliveries, poisoned
    deliveries — plus `evidence_review_relay_depth_observable`, which is the
    only one of the four that is ALWAYS present. That asymmetry is the design:
    the flag says whether this process can see, and the depths exist only when
    it can, so there is no configuration in which a reader can mistake "I am
    not allowed to look" for "there is nothing there".

    ⛔ **And it is BOUNDED.** The query runs on a thread this collector is
    willing to abandon after :data:`DEPTH_SCRAPE_BUDGET_SECONDS`, and while a
    reading is outstanding the next scrape refuses instead of opening a second
    connection. Both halves are load-bearing and neither is about latency: the
    first is the only thing that can stop a hang (`_DepthReading.run`'s `except`
    catches a query that RAISES; a query that never returns raises nothing), and
    the second is what keeps `/metrics` from holding more than one of the
    worker's two pool slots. See :data:`DEPTH_SCRAPE_BUDGET_SECONDS` for the
    30.016 s measurement that put them here.
    """

    def __init__(self, connect: Connect, *, budget_seconds: float = DEPTH_SCRAPE_BUDGET_SECONDS) -> None:
        self._connect = connect
        self._budget = budget_seconds
        self._lock = threading.Lock()
        self._pending: _DepthReading | None = None

    def collect(self) -> Iterator[Metric]:
        observable = GaugeMetricFamily(
            DEPTH_OBSERVABLE,
            "1 when this process can read the relay tables across tenants, so the depth gauges below "
            "are the whole truth. 0 when it cannot, in which case they are ABSENT rather than zero.",
            labels=["reason"],
        )
        reason, depths = self._read()
        if reason != REASON_OBSERVABLE or depths is None:
            observable.add_metric([reason], 0.0)
            yield observable
            return

        observable.add_metric([REASON_OBSERVABLE], 1.0)
        yield observable
        yield GaugeMetricFamily(
            OUTBOX_UNRELAYED,
            "domain_outbox rows whose event_id is not yet in research_domain_events. The relay's own "
            "backlog: if this only grows, the relay is not moving.",
            value=float(depths[0]),
        )
        yield GaugeMetricFamily(
            DELIVERIES_PENDING,
            "event_deliveries rows in state 'pending'. Fan-out happened; no consumer has acknowledged.",
            value=float(depths[1]),
        )
        yield GaugeMetricFamily(
            DELIVERIES_POISONED,
            "event_deliveries rows carrying a poison_reason. Each is an event a consumer will NEVER "
            "receive (13-…:§8.2 requires the owning team to be paged).",
            value=float(depths[2]),
        )

    def _read(self) -> tuple[str, tuple[int, int, int] | None]:
        """One bounded reading. NEVER waits longer than the budget, for any reason.

        ⛔ Single-flight, and that is the half that protects the pool rather than
        the scrape. A reading that has not come back still owns a connection; if
        every subsequent scrape started another, a database that is merely slow
        would drain a 2-connection pool in two scrapes and the relay loop would
        be starved by its own metrics. So while a reading is outstanding, every
        scrape answers `query_failed` immediately and starts nothing — at most
        ONE connection is ever held on behalf of `/metrics`.
        """
        with self._lock:
            outstanding = self._pending
            if outstanding is not None and not outstanding.done.is_set():
                logger.warning(
                    "metrics.relay_depth.still_running",
                    budget_seconds=self._budget,
                    detail="a previous scrape's database read has not returned; not starting another",
                )
                return REASON_QUERY_FAILED, None
            reading = _DepthReading()
            self._pending = reading
            threading.Thread(target=reading.run, args=(self._connect,), name="ers-relay-depth", daemon=True).start()

        if not reading.done.wait(self._budget):
            logger.warning("metrics.relay_depth.timed_out", budget_seconds=self._budget)
            return REASON_QUERY_FAILED, None
        if reading.error is not None:
            exc = reading.error
            logger.warning("metrics.relay_depth.failed", error=f"{type(exc).__name__}: {exc}")
            return REASON_QUERY_FAILED, None
        if reading.reason != REASON_OBSERVABLE:
            return REASON_ROW_LEVEL_SECURITY, None
        return REASON_OBSERVABLE, reading.depths

    @staticmethod
    def _can_see_every_row(session: Any) -> bool:
        """See `_VISIBILITY_SQL`. `None` (no tables) is refused, like everything else."""
        answer = session.execute(text(_VISIBILITY_SQL), {"tables": list(DEPTH_TABLES)}).scalar_one()
        return bool(answer)

    @staticmethod
    def _depths(session: Any) -> tuple[int, int, int]:
        """`(un-relayed outbox rows, pending deliveries, poisoned deliveries)`.

        The first reuses `relay.unrelayed_count` rather than restating its
        `NOT EXISTS`. A second spelling of "un-relayed" is a second answer to the
        question the relay's own progress is derived from, and the two would
        disagree the first time either changed.
        """
        from application.services.relay import unrelayed_count

        return (
            unrelayed_count(session),
            int(session.execute(text(_PENDING_SQL)).scalar_one()),
            int(session.execute(text(_POISON_SQL)).scalar_one()),
        )


class HeartbeatCollector(Collector):
    """Freshness of the last COMPLETED tick, read from the heartbeat file.

    ⛔ Emits NOTHING when the file is absent. The path
    (`WORKER_HEARTBEAT_PATH`) is per-pod state written by `worker.run()` in the
    worker's own container, so in the API's container there is no file and there
    never will be — and a `status=2` ("no heartbeat") exported from a process
    that is not a worker is a permanent false alarm, which trains an operator to
    ignore the one metric that is supposed to wake them.

    Where a file DOES exist, all three are emitted, including the failing
    states: an existing-but-stale or existing-but-unreadable heartbeat is
    precisely the observation this exists to make.

    `status` carries `worker_heartbeat`'s own exit codes verbatim (0 alive,
    1 stale, 2 absent, 3 unreadable), so the alert and the `livenessProbe`
    cannot come to disagree about what "stale" means — there is one function
    deciding it.

    ⛔ `status` and `age` are read off ONE clock — `worker_heartbeat._now`, the
    seam that module documents as existing so the clock can be driven — and this
    is a correction rather than a preference. The first version took its own
    `now` callable and left `check()` on the wall clock, so the two numbers could
    describe different instants: `test_a_fresh_heartbeat_reports_alive_with_its_age_and_its_bound`
    exported `age=2.0` beside `status=1` (stale) from the same scrape. A metric
    that contradicts itself is worse than either half alone, because whichever
    one the reader believes, they are being told the other is wrong.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def collect(self) -> Iterator[Metric]:
        path = worker_heartbeat.heartbeat_path(self._settings)
        if not path.exists():
            return

        code, _ = worker_heartbeat.check(self._settings)
        yield GaugeMetricFamily(
            HEARTBEAT_STATUS,
            "worker_heartbeat exit code for the last completed tick: 0 alive, 1 stale, 3 unreadable. "
            "The same function the livenessProbe runs.",
            value=float(code),
        )

        heartbeat = worker_heartbeat.read(self._settings)
        if heartbeat is None:
            return

        yield GaugeMetricFamily(
            HEARTBEAT_AGE,
            "Seconds since the worker loop last COMPLETED a tick. Written after the work, never "
            "before, so it does not advance through a query blocked on a lock.",
            # Clamped at zero, and read off `worker_heartbeat._now` rather than
            # `time.time` — see the class docstring. A negative age means the
            # clock moved backwards under us, which is not evidence of a stall.
            value=max(0.0, worker_heartbeat._now() - heartbeat.at),
        )
        yield GaugeMetricFamily(
            HEARTBEAT_BOUND,
            "The staleness bound derived from the tick interval the loop itself recorded. Exported "
            "beside the age so an alert does not have to hard-code a cadence the loop may not be running at.",
            value=worker_heartbeat.staleness_bound(heartbeat.interval, self._settings.worker_heartbeat_stale_multiple),
        )


def build_registry(
    settings: Settings | None = None,
    *,
    component: str,
    counters: _Counted | None = None,
    connect: Connect | None = None,
) -> CollectorRegistry:
    """The registry one process exports.

    A fresh `CollectorRegistry` rather than `prometheus_client.REGISTRY`, for two
    reasons that are both about correctness rather than tidiness: the global one
    is process-wide, so two `create_app()` calls in one test session would
    collide on `Duplicated timeseries`; and the global one is where a
    transitively-imported library's own collectors land, which would put series
    this service does not define behind this service's name.

    `counters` is what makes the worker's registry different from the API's, and
    passing it is the ONLY way the six counter series come into existence. See
    the module docstring for why they must not exist anywhere else.
    """
    settings = settings or get_settings()
    registry = CollectorRegistry()

    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)

    Gauge(
        BUILD_INFO,
        "Always 1. The labels are the payload: which process answered this scrape, and at what version.",
        ["service", "version", "component"],
        registry=registry,
    ).labels(settings.service_name, settings.version, component).set(1)

    registry.register(RelayDepthCollector(connect or (lambda: session_scope(settings))))
    registry.register(HeartbeatCollector(settings))
    if counters is not None:
        registry.register(WorkerCounterCollector(counters))
    return registry
