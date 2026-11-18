"""The process that runs the loops nothing was running.

`FND-006` shipped a durable-job claimant, a lease reaper and a cooperative cancel.
`FND-007` shipped an outbox, a durable log, per-consumer deliveries and an inbox.
Both merged. Neither had a process calling them: `job_catalog`'s docstring said it
plainly — "the process that calls them on a loop is deployment, and it does not
exist yet" — and measured on 2026-09-04, `research_domain_events`,
`event_deliveries` and `durable_jobs` all held **zero rows**. Six work packages of
transport, inert, because nobody had written ninety lines of loop.

This is that loop.

## What it runs, and the one thing it deliberately does not

* `relay.relay_pending` — `13-…:§8.1` steps 2 and 3. Moves committed outbox rows
  into the durable log and fans out a delivery row per registered consumer.
* `jobs.reap_expired_leases` — `§7.4`. Fails or requeues jobs whose worker stopped
  heartbeating, so a drained node does not strand work forever.

It does **not** claim and execute durable jobs. `job_catalog.OWED_BY` assigns each
job type to the work package that owes its handler, and not one of those handlers
exists yet. A worker that claimed a job it cannot execute would take the lease,
fail to run anything, and let the reaper it is also running fail the job on
lease expiry — a loop that manufactures failures out of an empty queue. When a
package ships its handler, claiming belongs here beside these two.

## ⛔ It refuses to start rather than relay nothing

This is the important design decision in the file, and it is a direct response to
how the subsystem failed the first time.

`domain_outbox`, `research_domain_events`, `event_deliveries` and `domain_inbox`
all carry `FORCE ROW LEVEL SECURITY` with a tenant policy comparing
`current_setting('app.current_tenant', true)::uuid`. A relay is inherently
cross-tenant: it drains every tenant's outbox in one pass and has no HTTP request
to derive a tenant from. With the GUC unset that comparand is `NULL`, every
comparison is `NULL`, and the worker reads **zero rows** — indistinguishable, at
the call site, from an empty queue.

`0020` and `0022` anticipated this with an ADDITIVE policy granted to one
restricted role, `evidence_review_worker`. But both migrations create that policy
only `IF EXISTS (SELECT 1 FROM pg_roles ...)`, and the role does not exist in
production — measured on worker4 — so on both runs Postgres emitted
`RAISE NOTICE 'role evidence_review_worker is absent; cross-tenant relay policy
skipped'` and moved on. A NOTICE in a migration log nobody reads.

So a worker started against the ordinary application role would have looked
perfectly healthy, logged `relayed 0` every tick forever, and reproduced the exact
condition this process exists to end. It therefore **preflights and exits
non-zero** instead, naming what to provision. A crash-looping pod with a
diagnosable message is worth more than a green one that does nothing, and
`which-way-a-guard-should-fail` is unambiguous here: the failure is ours, not the
world's, so fail closed.

## ⛔ And it checks the privilege, not a proxy for the privilege

The first version of that preflight asked `pg_policies` whether a `<table>_worker`
policy row existed, and treated its presence as "this worker can do its job". It
is not the same question. A policy governs which ROWS a statement sees; a GRANT
governs whether the statement RUNS. `0023` issues both — but only on the branch
that CREATES the policy, so a database whose policy arrived any other way can
hold every policy and no grant. The audit measured the consequence: with the
policy present and `INSERT` revoked on `event_deliveries`, preflight returned
`[]`, the relay logged `relayed=1 deliveries=0`, and the event became permanently
undeliverable with no exception, no poisoned row and no counter out of place.

So `preflight` now asks `has_table_privilege` for each verb this process actually
executes (`REQUIRED_PRIVILEGES`). Checking a proxy for the property instead of
the property is the recurring failure in this subsystem, and it is the same
mistake as the NOTICE nobody read.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from types import FrameType

import prometheus_client
from sqlalchemy import text
from sqlalchemy.orm import Session

import metrics
import worker_heartbeat
from application.services import jobs, relay
from bounded_db import DatabaseBudgetExceededError, run_bounded
from config import Settings, get_settings
from database import session_scope

LOGGER = logging.getLogger("evidence_review.worker")

#: ⛔ The wall-clock budget for one whole relay tick.
#:
#: ## The number this is measured against
#:
#: `worker_heartbeat.staleness_bound(interval, multiple)` is
#: `max(20, 6 x interval)` — **30.0 s** at the live `WORKER_INTERVAL_SECONDS=5`
#: declared in the worker Deployment. Past that the `livenessProbe`'s
#: `python -m worker_heartbeat` reports stale and kubelet restarts the pod.
#: A tick that can block for 30.016 s (the measured `getaddrinfo` stall, which
#: `connect_timeout` cannot see) therefore restarts a WORKING worker on its own.
#: That is not hypothetical here — see :func:`tick` for the two restarts it
#: caused on 2026-09-07.
#:
#: ## Why 15.0 s
#:
#: The bound is not "under 30", it is "leaves room for a RETRY under 30".
#: The loop sleeps `interval` between ticks, so from the last completed tick at
#: `t0`::
#:
#:     t0 + 5      a tick starts and stalls
#:     t0 + 5 + B  it is abandoned; the loop logs and sleeps
#:     t0 + 10 + B the next tick starts, and against a recovered resolver
#:                 completes in milliseconds -> the heartbeat advances
#:
#: The heartbeat must advance before `t0 + 30`, so `B < 20`. **15.0 s** takes
#: that with 5 s of margin for the tick's own work and clock jitter, and lands
#: on two numbers that already exist: 3 x the tick interval, and exactly half
#: the staleness bound. Unbounded, the same sequence advances at `t0 + 35` and
#: the pod is killed.
#:
#: `tests/unit/test_every_database_wait_is_bounded.py` re-derives this against
#: `worker_heartbeat.staleness_bound`, so shortening the interval or raising
#: the multiple fails with the reason attached rather than rotting quietly.
RELAY_TICK_BUDGET_SECONDS = 15.0

#: `statement_timeout`, in milliseconds, for each of the tick's two transactions.
#:
#: ⚠️ This is NOT a summand of the budget above, and the difference from
#: `metrics.py` is worth stating: a scrape issues a FIXED, SMALL number of
#: statements, so there `connect_timeout + n x statement_timeout` can be made to
#: sum below the deadline. A relay tick issues an unbounded-in-advance number of
#: them — one per row in the batch — so no per-statement value can bound the
#: total. Here the deadline is the only bound on the WHOLE tick, and
#: `statement_timeout` has a different job: it ends the abandoned transaction.
#:
#: ⚠️ `metrics.py` states that number as four and this change makes it **five**:
#: the collector connects through `database.session_scope`, which now issues a
#: `SET LOCAL statement_timeout` of its own BEFORE the collector's. The file is
#: 0 diff bytes and its 5.0 s deadline is unaffected — see
#: `database._bound_statements` for why nothing is weakened, and why the audited
#: arithmetic in `metrics.py` is left as it stands rather than re-derived here.
#:
#: That job sets the value. After the deadline fires, the abandoned thread is
#: inside one statement; Postgres kills it within at most this long, the
#: `session_scope` rolls back and the connection returns to the pool.
#:
#: ⚠️ **That pool is 2 + 2, in production and by default — the same four slots
#: either way.** `worker-deployment.yaml` sets `DB_POOL_SIZE: "2"` and
#: `DB_MAX_OVERFLOW: "2"`, raised from 1/1 when the metrics endpoint landed
#: precisely because the relay loop and a Prometheus scrape became TWO consumers
#: of it. So the arithmetic that matters is four slots against two concurrent
#: consumers, not a production floor tighter than the default: an abandoned tick
#: parks ONE of the four for at most this long while the scrape's own bounded
#: reading holds at most one more. 5 000 ms — one tick interval — means the slot
#: is back before the next tick could reach its own deadline, while still being
#: ~1000x the single-digit milliseconds an indexed relay statement against a
#: 200-row batch actually takes.
RELAY_TICK_STATEMENT_TIMEOUT_MS = 5000

#: The wall-clock budget for `preflight`, which runs once before the loop.
#:
#: Startup had the same hole as the tick and a worse presentation: an unbounded
#: `session_scope` here does not crash, it HANGS, and a hung start-up is a pod
#: that never writes a heartbeat, is eventually killed by the `livenessProbe`
#: (`failureThreshold: 4` x `periodSeconds: 15` = ~60 s), and restarts with no
#: line in its log saying why. Bounded, it exits 78 (`EX_CONFIG`) naming the
#: database it could not reach — the same diagnosable refusal preflight already
#: gives for a missing role or a missing policy.
#:
#: 20.0 s is comfortably inside that ~60 s window with room for the log lines
#: and the exit, and generous against a preflight that issues nine statements
#: (`current_user`, six policy probes, the privilege reads) on a cold pool.
PREFLIGHT_BUDGET_SECONDS = 20.0

#: `statement_timeout`, in milliseconds, for preflight's introspection.
#:
#: Nine catalogue reads against `pg_policies` / `information_schema`, each of
#: which is milliseconds. 5 000 ms keeps any one of them from consuming the
#: whole :data:`PREFLIGHT_BUDGET_SECONDS` and hiding the other eight answers,
#: which is the property preflight is built around: report EVERY problem at
#: once, so an operator is not made to do the work twice.
PREFLIGHT_STATEMENT_TIMEOUT_MS = 5000

#: The role `0020` and `0022` grant the additive cross-tenant policy to.
WORKER_ROLE = "evidence_review_worker"

#: The six tables the relay and the reaper touch across tenants. Each must carry
#: the worker policy, and the preflight names the ones that do not rather than
#: reporting a single unhelpful boolean.
#:
#: `durable_jobs` and `job_attempts` belong here too, not just the four outbox/
#: inbox tables: `jobs.reap_expired_leases` selects from `durable_jobs` with no
#: tenant predicate and `_seal_open_attempt` does the same on `job_attempts`,
#: relying entirely on the `durable_jobs_worker` / `job_attempts_worker`
#: policies migration `0023` creates. Leaving them out of this list was the
#: exact "looks healthy, does nothing" failure this file exists to prevent:
#: with either policy absent, preflight reports zero problems, the worker
#: starts, and the reaper silently reaps zero leases forever.
CROSS_TENANT_TABLES = (
    "domain_outbox",
    "research_domain_events",
    "event_deliveries",
    "domain_inbox",
    "durable_jobs",
    "job_attempts",
)

#: The table privileges this process actually executes, per table.
#:
#: ## ⛔ A policy is not a privilege, and checking the policy proved nothing
#:
#: RLS and `GRANT` are two independent gates and they fail in opposite
#: directions. A policy decides which ROWS a statement may see; a grant decides
#: whether the statement may RUN at all. `preflight` used to check only
#: `pg_policies`, and the audit measured what that misses:
#:
#:   policy `event_deliveries_worker` present + `INSERT` revoked on
#:   `event_deliveries` → `preflight()` returned `[]`, the worker started, the
#:   relay wrote the durable-log row, fanned out to **0** consumers,
#:   `unrelayed_count()` read `0`, and the tick logged `relayed=1 deliveries=0`.
#:   No exception, no poisoned row — the fan-out's fallback write needs the same
#:   revoked `INSERT` — and the outbox row is `NOT EXISTS`-satisfied forever. The
#:   event is permanently undeliverable and every counter says the relay is fine.
#:
#: That combination is not hypothetical: `0023`'s `ELSIF` branch returns early
#: when the policy already exists, so it issues its `GRANT` **only** on the run
#: that creates the policy. Any database where the policy arrived by another
#: route — `0020`/`0022` with the role present, a restore, a hand-fix — has the
#: policy and may have no grant, and that is exactly the state the old check
#: reported as healthy.
#:
#: So this maps each table to the verbs this process's own statements need, and
#: preflight asks `has_table_privilege` for each one. Minimal on purpose: a check
#: that demanded more than the code executes would refuse to start a correctly
#: least-privileged database, and one that demanded less is the defect above.
#:
#: * `domain_outbox` — read only. Relay progress is derived (`NOT EXISTS` against
#:   the log), never written back; see `relay.relay_pending`.
#: * `research_domain_events` — `INSERT … ON CONFLICT DO NOTHING`, plus the
#:   `SELECT` in the same statement's `NOT EXISTS`.
#: * `event_deliveries` — one `INSERT` per consumer, and a second `INSERT` for a
#:   `poisoned` row when the first fails. Both need the same verb, which is why
#:   revoking it produced silence rather than a poisoned row.
#: * `domain_inbox` — read only. Nothing in this process writes a receipt; the
#:   consumer that will is not in this service (`relay` §replay). Listed so a
#:   worker that cannot even see the table is refused rather than reporting an
#:   empty inbox.
#: * `durable_jobs` — the reaper's `SELECT … FOR UPDATE` and its `UPDATE`.
#: * `job_attempts` — `_seal_open_attempt` inserts an attempt row and updates it
#:   when it closes.
REQUIRED_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "domain_outbox": ("SELECT",),
    "research_domain_events": ("SELECT", "INSERT"),
    "event_deliveries": ("SELECT", "INSERT"),
    "domain_inbox": ("SELECT",),
    "durable_jobs": ("SELECT", "UPDATE"),
    "job_attempts": ("SELECT", "INSERT", "UPDATE"),
}


def _now() -> float:
    """Wall clock, in epoch seconds.

    A seam, for the reason `worker_heartbeat._now` gives: the tests need to drive
    this loop's clock, and monkeypatching the shared `time` module object to do
    it would reach every other importer in the process.
    """
    return time.time()


def _sleep(seconds: float) -> None:
    """The gap between ticks. A seam for the same reason as `_now`."""
    time.sleep(seconds)


@dataclass
class _Counters:
    """Everything this process has done since it started, and when it last said so.

    ## ⛔ Why there is a periodic log line at all

    Measured over nine hours in production, this worker's ENTIRE output was two
    lines: `created SQLAlchemy engine` and `worker started: ...`. The loop below
    logs only a tick that moved something, all four relay tables are empty, and
    there is no HTTP server, no Service, no ServiceMonitor and no PodMonitor on
    the workload. So "alive and idle" and "hung" produced byte-identical output —
    silence — and nothing anywhere could tell them apart.

    ## Why 300 seconds and not every tick

    Logging every tick at the default 5s cadence is 17,280 lines a day, all of
    them `relayed=0 deliveries=0 reaped=0 poisoned=0`. That is not observability;
    it is the reason the previous version logged nothing at all, because the only
    alternative anyone considered was noise.

    300s is 288 lines a day — one per five-minute bucket, which is the resolution
    a log-based alert or a human scrolling actually works at. It is short enough
    that "no heartbeat line for fifteen minutes" is a three-sample judgement
    rather than a coin flip on one missing line, and long enough that the line
    stays readable when someone is following the stream live.

    ⚠️ The FIRST completed tick always logs, whatever the interval. A pod that
    had to wait five minutes to say anything about itself would spend its whole
    startup window in exactly the silence this class exists to end.
    """

    ticks: int = 0
    relayed: int = 0
    deliveries: int = 0
    poisoned: int = 0
    reaped: int = 0
    errors: int = 0
    last_logged_at: float | None = None

    def record(self, relayed: int, deliveries: int, reaped: int, poisoned: int) -> None:
        self.ticks += 1
        self.relayed += relayed
        self.deliveries += deliveries
        self.reaped += reaped
        self.poisoned += poisoned

    def due(self, now: float, every: float) -> bool:
        return self.last_logged_at is None or (now - self.last_logged_at) >= every


@dataclass
class _Stop:
    """Cooperative shutdown flag.

    A worker killed mid-transaction is safe — every loop below owns its own
    transaction and the database rolls back an incomplete one — but a worker that
    finishes its tick and exits cleanly leaves no lease to reap, which is one
    less thing for the next start to recover.
    """

    requested: bool = False

    def request(self, signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("shutdown requested by signal %s; finishing the current tick", signum)
        self.requested = True


def _parse_interval(raw: str) -> tuple[float | None, str | None]:
    """`(interval, problem)`. Exactly one is not `None`.

    Isolated from `preflight` so it can be unit-tested without a database, and
    called BY `preflight` so a malformed `WORKER_INTERVAL_SECONDS` becomes a
    named, diagnosable problem alongside the others rather than an unhandled
    `ValueError` raised before preflight ever runs.
    """
    try:
        interval = float(raw)
    except ValueError:
        return None, f"WORKER_INTERVAL_SECONDS={raw!r} is not a number."
    if interval <= 0:
        return None, f"WORKER_INTERVAL_SECONDS={raw!r} must be positive, got {interval}."
    return interval, None


def preflight(settings: Settings) -> list[str]:
    """Every reason this process cannot do its job. Empty means it can.

    Returns problems rather than raising, so a start-up failure can report ALL of
    them at once. An operator who provisions the role, restarts, and then learns
    the policies are also missing has been made to do the work twice by a message
    that stopped at the first defect.
    """
    problems: list[str] = []

    _, interval_problem = _parse_interval(os.environ.get("WORKER_INTERVAL_SECONDS", "5"))
    if interval_problem:
        problems.append(interval_problem)

    # ⛔ BOUNDED, and the budget failure is reported as a problem rather than
    # raised. Preflight's contract is that it returns every reason this process
    # cannot do its job; "the database did not answer" is one of those reasons,
    # and letting it escape as a traceback would lose the interval problem
    # already collected above — the exact "made to do the work twice" this
    # function exists to avoid.
    try:
        problems.extend(
            run_bounded(lambda: _database_problems(settings), budget_seconds=PREFLIGHT_BUDGET_SECONDS, name="preflight")
        )
    except DatabaseBudgetExceededError as exc:
        problems.append(
            f"the database did not answer within {PREFLIGHT_BUDGET_SECONDS:.0f}s, so none of the role "
            f"or policy checks could run ({exc}). This is NOT a schema problem: nothing was read. "
            "psycopg3 resolves the host in Python before libpq is involved, so a stalled resolver "
            "presents here and `connect_timeout` cannot see it — check DNS for the host in "
            "DATABASE_URL before checking the database."
        )
    return problems


def _database_problems(settings: Settings) -> list[str]:
    """Preflight's database half, on the thread the budget may abandon."""
    problems: list[str] = []
    with session_scope(settings, statement_timeout_ms=PREFLIGHT_STATEMENT_TIMEOUT_MS) as session:
        current_user = session.execute(text("SELECT current_user")).scalar_one()
        if current_user != WORKER_ROLE:
            problems.append(
                f"connected as {current_user!r}, not {WORKER_ROLE!r}. The relay is cross-tenant "
                f"and the additive policy in migrations 0020/0022 is granted TO {WORKER_ROLE} and "
                "to nobody else. As any other role the tenant GUC is unset, every RLS comparand "
                "is NULL, and this process would read zero rows forever while looking healthy. "
                "Point DATABASE_URL at that role."
            )

        missing = [
            table
            for table in CROSS_TENANT_TABLES
            if not session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = :table AND policyname = :policy)"),
                {"table": table, "policy": f"{table}_worker"},
            ).scalar_one()
        ]
        if missing:
            problems.append(
                f"the cross-tenant worker policy is absent on: {', '.join(missing)}. "
                f"Migrations 0020, 0022 and 0023 create it only IF the {WORKER_ROLE} role already "
                "exists, and they RAISE NOTICE and continue when it does not — so creating the "
                "role AFTER those migrations ran does not retroactively create the policies. " + _REMEDIATION
            )

        problems.extend(_privilege_problems(session))
    return problems


#: What actually repairs a database in the state `preflight` refuses to start on.
#:
#: ⛔ It is NOT "run `alembic upgrade head` again", which is what this message
#: used to say. An alembic revision runs exactly once: `alembic_version` already
#: names `0023` (or later), so `upgrade head` is a no-op that prints nothing, and
#: an operator who follows that instruction restarts into the identical refusal
#: with no new information. A remediation that cannot work is worse than none —
#: it spends the reader's trust before they start looking for the real cause.
_REMEDIATION = (
    "⛔ Re-running `alembic upgrade head` does NOT fix this: a revision runs once, "
    "`alembic_version` is already at or past 0023, and re-running it is a no-op. "
    "Either (a) create the role BEFORE migrating, which is the only ordering that "
    "works on a fresh environment — `CREATE ROLE evidence_review_worker LOGIN …` "
    "then `alembic upgrade head`; or (b) on a database that is already migrated, add "
    "a NEW revision that re-asserts the policy AND the GRANT (0023's `_create_policy` "
    "and `_connect_and_usage` are the shape to copy), or have the schema owner issue "
    "the CREATE POLICY / GRANT statements by hand. Nothing already recorded in "
    "`alembic_version` will run again."
)


def _privilege_problems(session: Session) -> list[str]:
    """Tables whose GRANTs do not cover the statements this process executes.

    Separate from the policy check on purpose: the two are independent gates and
    a database can fail either one alone. The measured failure was a database
    that passed the policy check and failed this one, and reported healthy —
    see `REQUIRED_PRIVILEGES` for what that cost.

    `has_table_privilege` is asked per verb rather than per table, so the message
    names the exact statement that would be refused instead of "something about
    event_deliveries". Existence is checked first with `to_regclass`, because
    `has_table_privilege` on an absent table raises `UndefinedTable` and aborts
    the transaction — the preflight would then die with a driver error instead of
    reporting the perfectly diagnosable "migrations have not run here".
    """
    problems: list[str] = []
    absent: list[str] = []
    lacking: list[str] = []

    for table, privileges in REQUIRED_PRIVILEGES.items():
        present = session.execute(
            text("SELECT to_regclass(:qualified) IS NOT NULL"),
            {"qualified": f"public.{table}"},
        ).scalar_one()
        if not present:
            absent.append(table)
            continue

        denied = [
            privilege
            for privilege in privileges
            if not session.execute(
                text("SELECT has_table_privilege(:table, :privilege)"),
                {"table": table, "privilege": privilege},
            ).scalar_one()
        ]
        if denied:
            lacking.append(f"{table} ({', '.join(denied)})")

    if absent:
        problems.append(
            f"these tables do not exist in this database: {', '.join(absent)}. "
            "The schema is behind the code — run `alembic upgrade head` as the schema OWNER "
            "before starting this process."
        )
    if lacking:
        problems.append(
            f"the {WORKER_ROLE} role lacks table privileges it executes: {', '.join(lacking)}. "
            "A row-level-security POLICY is not a GRANT: the policy decides which rows a "
            "statement sees, the grant decides whether the statement runs at all, and a database "
            "can carry every worker policy while refusing every worker INSERT. That combination "
            "is reachable — migration 0023 issues its GRANT only on the run that CREATES the "
            "policy, and returns early when the policy is already there. Measured consequence: "
            "the relay writes the durable-log row, fans out to zero consumers, cannot even write "
            "the poisoned row that would record the failure, and logs `relayed=1 deliveries=0` "
            "forever while the event becomes permanently undeliverable. " + _REMEDIATION
        )
    return problems


class TickResult(tuple):
    """`(relayed, deliveries, reaped)`, carrying `poisoned` alongside.

    A `tuple` subclass for the reason `relay.RelayResult` is one, and the reason
    is load-bearing here rather than stylistic: `tick` is monkeypatched by the
    lifecycle tests and called as a plain 3-tuple, and every existing caller
    writes `relayed, delivered, reaped = tick(settings)`. Returning a 4-tuple
    would break all of them with `ValueError: too many values to unpack` — so
    the counts a caller already unpacks stay a 3-tuple and the new fact rides as
    an attribute, which is additive by construction.

    `poisoned` exists because the previous version of `tick` discarded it.
    `relay.relay_pending` returns a `RelayResult` whose `.poisoned` counts
    (event, consumer) pairs whose delivery insert failed and was recorded as a
    poisoned row — `13-…:§8.2` says those "page the owning team". `tick` unpacked
    the result into two names and dropped the rest, so a poisoned delivery was
    written to the database and then reported by nothing: the worker's only
    output line said `relayed=N deliveries=M`, which reads as a healthy pass.
    """

    # See `RelayResult`: annotations only. A non-empty `__slots__` is impossible
    # on a variable-length builtin subclass, and the annotation is what the type
    # checker needs to resolve `self.poisoned`.
    poisoned: int
    poisoned_events: tuple[tuple[uuid.UUID, str], ...]

    def __new__(
        cls,
        relayed: int,
        deliveries: int,
        reaped: int,
        poisoned: int = 0,
        poisoned_events: tuple[tuple[uuid.UUID, str], ...] = (),
    ) -> TickResult:
        self = super().__new__(cls, (relayed, deliveries, reaped))
        self.poisoned = poisoned
        self.poisoned_events = poisoned_events
        return self


def _tick_body(settings: Settings) -> TickResult:
    """The tick's database work, on the thread :func:`tick` may abandon.

    Each unit of work takes its own transaction. A reaper failure must not roll
    back a relay that already succeeded: they share nothing but a process, and
    coupling their transactions would make one subsystem's bad day the other's.

    ⚠️ Two transactions, ONE deadline — see :func:`tick`. Bounding each of them
    separately would state a bound of `RELAY_TICK_BUDGET_SECONDS` and deliver
    one of twice that, which is the difference between surviving a blip and
    being restarted by it.
    """
    with session_scope(settings, statement_timeout_ms=RELAY_TICK_STATEMENT_TIMEOUT_MS) as session:
        result = relay.relay_pending(session)
        relayed, delivered = result

    with session_scope(settings, statement_timeout_ms=RELAY_TICK_STATEMENT_TIMEOUT_MS) as session:
        reaped = len(jobs.reap_expired_leases(session))

    return TickResult(relayed, delivered, reaped, result.poisoned, result.poisoned_events)


def tick(settings: Settings) -> TickResult:
    """One pass, BOUNDED. Returns `(events relayed, deliveries created, leases reaped)`.

    Unpacks as that 3-tuple exactly as before; `.poisoned` and
    `.poisoned_events` ride alongside — see :class:`TickResult`.

    ## ⛔ The production evidence for this bound

    `evidence-review-worker` was restarted twice on 2026-09-07 between 18:50 and
    19:00 UTC — exit 137 preceded by `shutdown requested by signal 15`, an
    in-place liveness kill rather than a rollout. It was **not** this service's
    fault: eleven namespaces restarted in the same twenty-minute window
    (`flux-system` 7 times, `cloudflared` 7, `commonpractice-coaching` 5,
    `cattle-fleet-system` 5, `ingestion` 4), a fleet-wide infrastructure blip.

    **But this worker should have survived it, and the reason it did not is
    here.** The tick's `session_scope` was unbounded, so a stalled name
    resolution — measured at 30.016 s, and outside `connect_timeout` entirely
    because psycopg3 resolves in Python before libpq sees anything — held the
    loop past the heartbeat staleness bound. `worker_heartbeat` reported stale,
    the `livenessProbe` restarted a worker that was working, and the brief
    dependency blip became a pod restart. Bounding the tick is what breaks that
    chain: the loop's `except Exception` logs a failed tick, sleeps, and the
    NEXT tick advances the heartbeat while the bound still has room.

    ⚠️ A raise is what makes it work, and it costs nothing that was not already
    lost. `run()` catches it, leaves `completed` False and does not advance the
    heartbeat — identical to any other tick failure, because that is what this
    is. What changes is that it happens at :data:`RELAY_TICK_BUDGET_SECONDS`
    with room left inside the bound, instead of at 30 s with none.
    """
    return run_bounded(
        lambda: _tick_body(settings),
        budget_seconds=RELAY_TICK_BUDGET_SECONDS,
        name="relay_tick",
    )


def start_metrics_server(settings: Settings, counters: _Counters) -> bool:
    """Serve `/metrics` for THIS process. `True` if a listener is now up.

    ## ⛔ Why the worker needs its own listener rather than the API's

    The API's `/metrics` cannot carry any of the six numbers below. They live in
    `counters`, in this process's memory, and a `prometheus_client.Counter`
    registered in the API would read `0` for the life of the pod — a flat line
    that reads as *"the relay is healthy and idle"* when the truth is *"this
    process has never relayed anything and never will"*. The same is true of the
    queue depths for a different reason (RLS; see `metrics`) and of the
    heartbeat for a third (the file is in this container). So the honest export
    point for all of them is here.

    ## Never fatal

    A worker that refuses to relay because it could not bind a metrics port has
    turned an observability gap into an outage, which inverts the whole point of
    this change. `EADDRINUSE` from a sidecar, a restart racing its predecessor's
    socket, a `WORKER_METRICS_PORT` typo — each is logged and the loop runs.

    `prometheus_client.start_http_server` spawns a DAEMON thread, and that is
    load-bearing rather than incidental: it means the scrape still answers while
    the loop below is wedged inside a blocked query, which is the only condition
    under which the heartbeat-age gauge is worth anything at all.
    """
    if settings.worker_metrics_port == 0:
        LOGGER.info("worker metrics server disabled (WORKER_METRICS_PORT=0)")
        return False
    try:
        prometheus_client.start_http_server(
            settings.worker_metrics_port,
            addr=settings.worker_metrics_host,
            registry=metrics.build_registry(settings, component="worker", counters=counters),
        )
    except OSError:
        LOGGER.exception(
            "could not bind the metrics server on %s:%d; this process will relay and reap normally "
            "but nothing can scrape it. Set WORKER_METRICS_PORT to a free port, or to 0 to stop "
            "trying.",
            settings.worker_metrics_host,
            settings.worker_metrics_port,
        )
        return False
    LOGGER.info(
        "worker metrics on %s:%d/metrics",
        settings.worker_metrics_host,
        settings.worker_metrics_port,
    )
    return True


def run(settings: Settings | None = None, *, once: bool = False) -> int:
    """Loop until asked to stop. Returns a process exit code."""
    settings = settings or get_settings()

    problems = preflight(settings)
    if problems:
        for problem in problems:
            LOGGER.error("preflight: %s", problem)
        LOGGER.error(
            "refusing to start. A worker that cannot see the log across tenants relays nothing "
            "and reports success, which is the failure this process was written to end."
        )
        return 78  # EX_CONFIG — the environment is wrong, not the code.

    # Guaranteed valid: preflight already validated WORKER_INTERVAL_SECONDS and
    # would have refused to start otherwise.
    interval, _ = _parse_interval(os.environ.get("WORKER_INTERVAL_SECONDS", "5"))
    assert interval is not None

    stop = _Stop()
    if not once:
        # Signals can only be installed from the main thread of the main
        # interpreter. A one-shot tick is routinely driven from a test harness
        # or an embedding process running this off the main thread, and there
        # is nothing to catch cooperatively in a call that returns after one
        # tick anyway — so only pay for handlers on the looping path, and even
        # there tolerate a thread that cannot install them rather than crash
        # before a single tick runs.
        try:
            signal.signal(signal.SIGTERM, stop.request)
            signal.signal(signal.SIGINT, stop.request)
        except ValueError:
            LOGGER.warning(
                "cannot install SIGTERM/SIGINT handlers outside the main thread of the main "
                "interpreter; cooperative shutdown is unavailable and this process will only "
                "stop between ticks if killed."
            )

    LOGGER.info(
        "worker started: relay + lease reaper, every %.1fs. Durable-job execution is NOT run "
        "here — job_catalog.OWED_BY assigns each handler to the package that owes it, and none "
        "exists yet. Heartbeat at %s, checked by `python -m worker_heartbeat` against a "
        "%.1fs bound (%.1fx the tick interval).",
        interval,
        settings.worker_heartbeat_path,
        worker_heartbeat.staleness_bound(interval, settings.worker_heartbeat_stale_multiple),
        settings.worker_heartbeat_stale_multiple,
    )

    counters = _Counters()

    # ⛔ Only on the LOOPING path, for the reason the signal handlers above are:
    # a one-shot tick is routinely driven from a test harness or an embedding
    # process, often many times in one interpreter, and each of those would try
    # to bind the same port — so the second onwards would log a bind failure
    # that says nothing about the code under test. A process that runs a single
    # tick and exits is also not a scrape target: nothing could reach it before
    # it was gone. `start_metrics_server` is exercised directly instead, by
    # `tests/unit/test_the_worker_serves_its_own_metrics.py`.
    if not once:
        start_metrics_server(settings, counters)

    while True:
        completed = False
        try:
            result = tick(settings)
            relayed, delivered, reaped = result
            # `getattr`, not `result.poisoned`: `tick` is monkeypatched to a plain
            # tuple by the lifecycle tests, and a worker loop that crashes because
            # its instrumentation assumed a richer return type would be a worse
            # defect than the one this line fixes.
            poisoned = getattr(result, "poisoned", 0)
            if relayed or delivered or reaped or poisoned:
                LOGGER.info("relayed=%d deliveries=%d reaped=%d poisoned=%d", relayed, delivered, reaped, poisoned)
            if poisoned:
                # WARNING and separate: a poisoned delivery is a delivery that
                # will never be made, and `13-…:§8.2` wants the owning team
                # paged. Until that dashboard exists this line is the whole
                # signal, so it names the pairs rather than only counting them.
                LOGGER.warning(
                    "%d poisoned deliver%s this tick: %s. Each is an event a consumer will never "
                    "receive; 13-…:§8.2 requires the owning team to be paged.",
                    poisoned,
                    "y" if poisoned == 1 else "ies",
                    ", ".join(
                        f"{event_id}->{consumer}" for event_id, consumer in getattr(result, "poisoned_events", ())
                    ),
                )
            counters.record(relayed, delivered, reaped, poisoned)
            completed = True
        except Exception:
            # Logged and swallowed on purpose. A transient database blip must not
            # end the process: the pod would restart, preflight again, and the
            # only thing achieved is a restart count that looks like instability.
            # A genuinely broken environment is caught by preflight, once, loudly.
            #
            # ⛔ `completed` stays False, so this tick does NOT advance the
            # heartbeat. That is the whole point of the heartbeat: a loop that is
            # running and not finishing ticks must look different from a loop
            # that is running and idle, and the only thing that distinguishes
            # them is whether the work got to the end.
            counters.errors += 1
            LOGGER.exception("tick failed; continuing")

        if completed:
            # ⛔ AFTER the work, and only after work that finished. A heartbeat
            # written above `tick()` would advance at full cadence through a
            # query blocked on a lock forever — a liveness signal for the
            # scheduler rather than for the work. See `worker_heartbeat`.
            try:
                worker_heartbeat.advance(
                    settings,
                    worker_heartbeat.Heartbeat(
                        at=_now(),
                        interval=interval,
                        ticks=counters.ticks,
                        relayed=counters.relayed,
                        deliveries=counters.deliveries,
                        poisoned=counters.poisoned,
                        reaped=counters.reaped,
                        errors=counters.errors,
                    ),
                )
            except OSError:
                # Not fatal, and not silent. The loop is working; the thing that
                # reports it is not, so the probe will call this pod stale and
                # restart it — which is the right outcome for an unwritable
                # heartbeat path, and this line is what tells the next reader
                # that the restart was about the filesystem and not the relay.
                LOGGER.exception(
                    "could not write the heartbeat to %s; the liveness probe will read this "
                    "process as stalled even though the tick completed.",
                    settings.worker_heartbeat_path,
                )

        # OUTSIDE the `completed` branch on purpose: a loop whose every tick is
        # failing is still alive, and `errors=N ticks=0` is a materially
        # different diagnosis from silence. The tracebacks above say what broke;
        # this line says the loop is still there to break.
        now = _now()
        if counters.due(now, settings.worker_heartbeat_log_seconds):
            counters.last_logged_at = now
            LOGGER.info(
                "heartbeat: ticks=%d relayed=%d deliveries=%d poisoned=%d reaped=%d errors=%d "
                "(cumulative since start; every %.0fs)",
                counters.ticks,
                counters.relayed,
                counters.deliveries,
                counters.poisoned,
                counters.reaped,
                counters.errors,
                settings.worker_heartbeat_log_seconds,
            )

        if once or stop.requested:
            return 0
        _sleep(interval)


#: The flag that turns this module into its own liveness probe.
#:
#: ⚠️ `python -m worker_heartbeat` is the stanza to WIRE — see `README.md`. This
#: flag exists because `worker.py` is the file an operator has in their hand, and
#: a probe they cannot guess is a probe nobody adds; but it imports SQLAlchemy,
#: the ORM models and every application service on the way in, which is seconds
#: of interpreter start-up charged against `timeoutSeconds` on every probe.
#: `worker_heartbeat` imports the standard library and `config`, and nothing else.
HEALTHCHECK_FLAG = "--healthcheck"


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if HEALTHCHECK_FLAG in arguments:
        return worker_heartbeat.main()
    return run()


if __name__ == "__main__":
    sys.exit(main())
