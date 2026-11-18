"""One bounded database operation, for every path that must not wait forever.

## The defect this module exists for

⛔ **`connect_timeout` is not a bound on a database call.** psycopg3 resolves the
host name in Python — `_conninfo_attempts._resolve_hostnames` ->
`socket.getaddrinfo` — **before libpq is handed anything**, and `getaddrinfo`
takes no timeout. Measured on a host whose internal DNS resolvers are
unreachable::

    socket.getaddrinfo("db.example", 5432)
    ERR gaierror [Errno 8] nodename nor servname provided, or not known
    elapsed 30.015876054763794

So a connection attempt can block for 30 s *outside* everything libpq and
Postgres can see. `connect_timeout` never gets a chance to apply, and
`statement_timeout` is a server-side setting on a server this process has not
reached.

⛔ **And a hang raises nothing.** Every site below was already wrapped in
`except Exception`, which is correct handling for a call that FAILS and no
handling at all for one that never returns. The code looked defended and was
not — that is the whole finding, and it is why the fix is a clock rather than
another `except`.

## Three bounds, not one

A "bounded database operation" in this service means all three, because each one
covers a failure the other two cannot see:

============================  =========================================
`connect_timeout`             libpq's TCP handshake. Set on the engine
                              (`database.get_engine`). Blind to name
                              resolution.
`statement_timeout`           Postgres ends the statement. Set with
                              ``SET LOCAL`` as the first statement of
                              every scope in `database`. Blind to
                              anything before the server is reached.
**a wall-clock deadline**     :func:`run_bounded`. The ONLY one of the
                              three that covers `getaddrinfo`, and the
                              only one that can stop a caller waiting.
============================  =========================================

The deadline does not make the other two redundant. It stops the *caller*
waiting; it cannot stop the abandoned work, and abandoned work holds a
connection — which is a pool slot the next caller needs. `connect_timeout` and
`statement_timeout` are what actually end it.

## Why a thread, and why it is abandoned

`getaddrinfo`, `connect()` and `execute()` are all uninterruptible from the
calling thread. There is no signal, no flag and no cancellation that reaches
them. The only way to put a ceiling on a call you cannot interrupt is **to stop
waiting for it**, which requires the call to be on a different thread. So the
work runs on a daemon thread and the caller waits on an `Event`.

`daemon=True` rather than `concurrent.futures`: a `ThreadPoolExecutor`
registers an `atexit` hook that JOINS its workers, so one wedged operation would
hold the interpreter open for as long as it stayed wedged — a bounded call
turned into an unbounded shutdown, the same defect wearing a hat. A daemon
thread is abandoned at exit.

This is the mechanism `metrics.RelayDepthCollector` already ships and that
`d1707670` audited into production. ⛔ `metrics.py` is deliberately NOT
refactored onto this module: its 5.0 s budget is deployed and audited, and
re-pointing an audited bound at new code to save a few lines would put the one
already-proven site at risk to tidy up. This module is that mechanism made
reusable for the sites that had nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from log_config import get_logger

logger = get_logger(__name__)


class BoundedDatabaseError(Exception):
    """A bounded operation did not produce an answer. It did not hang either."""


class DatabaseBudgetExceededError(BoundedDatabaseError, TimeoutError):
    """The wall-clock budget ran out with the work still outstanding.

    Also a `TimeoutError` so that callers which already treat a timeout as a
    retryable dependency failure — the worker loop's `except Exception`, the
    readiness probe's report — need no new branch to keep behaving correctly.
    """

    def __init__(self, name: str, budget_seconds: float) -> None:
        self.name = name
        self.budget_seconds = budget_seconds
        super().__init__(
            f"{name} did not answer within its {budget_seconds:.1f}s budget. The work was ABANDONED, "
            "not cancelled: it is still holding a connection until connect_timeout or "
            "statement_timeout ends it."
        )


class DatabaseBusyError(BoundedDatabaseError):
    """A previous operation of the same name is still outstanding.

    Raised by the single-flight guard, and it is a materially different fact
    from :class:`DatabaseBudgetExceededError`: this caller waited for nothing at all.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"{name} is already outstanding from an earlier call that was abandoned. Refusing to "
            "open a second connection: accumulating one per call is how a stalled dependency "
            "exhausts the pool that the real work needs."
        )


class SingleFlight:
    """At most one outstanding operation, released by the thread that OWNS it.

    ⛔ Bounding one call is not enough on its own, and the reason is the pool.
    A caller that gives up does not stop the work; the abandoned thread keeps
    its connection until `connect_timeout` or `statement_timeout` ends it. A
    probe on a 10 s kubelet period against a resolver that takes 30 s to fail
    therefore accumulates three connections before the first one is returned —
    and `app.py` caps the request threadpool to `pool_size + max_overflow`, so
    those are the same slots every request handler needs.

    The release happens in the worker thread's `finally` whenever that thread
    took the operation, never in the caller's, because the caller leaves first
    by construction. Releasing on the caller's side while the work is still
    outstanding would re-admit the next call while the abandoned one still holds
    its connection, which is exactly the accumulation this prevents.

    ⛔ **That makes the release dependent on a worker thread that runs, so a
    path which acquires without producing one leaks the guard permanently.**
    :func:`run_bounded` therefore does not reason about whether the thread is
    live — it cannot, see below — but settles ownership of the call on a
    one-shot lock that the caller and the runner both race for. See the comment
    there.

    ⚠️ **A held guard and a leaked guard are different facts, and only the
    second one is this class's problem.** A socket that blackholes AFTER the
    connect succeeds leaves the worker parked in `recv()`: the `finally` still
    runs, so the guard is HELD rather than lost. Refusing probes is correct
    *while* the socket is dead — but the guard goes on being held after it
    recovers, because what ends the hold is the abandoned `recv()` returning,
    not the database answering again. So every probe from the moment of recovery
    to the moment that socket finally dies is a **false NotReady** against a
    healthy database, not a correct refusal.

    ⚠️ And that window is long, because nothing bounds it: `connect_args` in
    `database.get_engine` sets no
    `keepalives_idle` / `keepalives_interval` / `tcp_user_timeout`, so what ends
    the parked `recv()` is the platform TCP keepalive default — on Linux 7200 s
    idle plus 9 x 75 s of probes, about **two hours**. `statement_timeout`
    cannot end it
    either: it is enforced by a server whose reply is what went missing.

    ⛔ That is a SEPARATE defect and is deliberately not fixed here. Its lever is
    `connect_args`, which is process-wide and reaches every caller including the
    relay and all 52 `actor_scope` sites; it needs its own derivation and its own
    measurement against a real blackhole, not a value smuggled in beside a
    control-flow fix. Conflating the two would ship an untested engine-wide TCP
    change under the heading of a leak repair.
    """

    __slots__ = ("_busy", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    def acquire(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._lock:
            self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy


def run_bounded[T](
    work: Callable[[], T],
    *,
    budget_seconds: float,
    name: str,
    single_flight: SingleFlight | None = None,
) -> T:
    """Run `work` on a thread this call is willing to LEAVE. Never waits longer.

    Returns whatever `work` returned, and re-raises whatever it raised, so a
    caller that already handles database errors keeps handling them. The one new
    outcome is :class:`DatabaseBudgetExceededError`, which is the answer to the
    question `except Exception` could never ask: *did it come back at all?*

    ⚠️ `work` takes no session and opens its own. That is deliberate: the
    connection has to be established ON the abandonable thread, because
    establishing it is the part that hangs. A signature that handed `work` a
    ready session would have done the dangerous half in the caller.

    It also lets one deadline cover SEVERAL transactions — which
    `worker.tick()` needs, since a relay failure must not roll back the reaper
    and vice versa, but the two together are what must fit inside the heartbeat
    staleness bound. Bounding each separately would state a bound of `budget`
    and deliver one of `2 x budget`.
    """
    # ⛔ **Who owns this call — the runner or the caller — decided ONCE, here.**
    #
    # `Lock.acquire(blocking=False)` succeeds for exactly one claimant ever,
    # because nothing ever releases `owned`. Whoever wins it runs the work and
    # performs the release; whoever loses does neither. That is the whole
    # mechanism, and it is what makes both branches below race-free.
    #
    # ⚠️ It is built this way because **`start()` returning is NOT the fact the
    # old comment here claimed it was.** `threading.Thread.start` (CPython
    # 3.14.3, `threading.py:1003-1011`) ends::
    #
    #     try:
    #         _start_joinable_thread(self._bootstrap, handle=..., daemon=...)
    #     except Exception:
    #         with _active_limbo_lock:
    #             del _limbo[self]
    #         raise
    #     self._started.wait()          # <- line 1011, OUTSIDE the try
    #
    # so `start()` can raise from that last line with `_runner` **already
    # live**. A branch that assumes "start() raised => no thread exists" then
    # releases a guard whose work is still outstanding, and a second caller
    # acquires while the first still holds its connection — measured on the
    # branch tip before this, with the raise modelled at line 1011::
    #
    #     work still outstanding                                  = True
    #     guard busy while work outstanding                       = False
    #     a SECOND caller acquired while the first is outstanding = True
    #
    # No flag passed into the thread can fix that: the caller cannot know which
    # side of `_started.wait()` it failed on, and any answer it computes is
    # stale the instant it reads it. A lock both sides contend for needs no
    # answer.
    #
    # It is created BEFORE the guard is taken so that a failure to create it has
    # acquired nothing, and can therefore leak nothing.
    owned = threading.Lock()

    if single_flight is not None and not single_flight.acquire():
        raise DatabaseBusyError(name)

    # ⛔ The `try` opens IMMEDIATELY after `acquire()`, with nothing between
    # them. Everything from here to `start()` allocates, and an allocation
    # failure anywhere in it used to leave the guard busy with no thread ever
    # born to release it — the same permanent leak, one line higher up. Modelled
    # with `threading.Event()` raising `MemoryError`, on the branch tip before
    # this: `guard busy after = True`, `a later caller can acquire = False`.
    try:
        done = threading.Event()
        # Two typed one-slot lists rather than a `dict[str, object]`. Emptiness
        # IS the discriminator, which matters because `work()` returning `None`
        # and `work()` raising are different outcomes that a `value = None`
        # sentinel cannot tell apart — and one of these paths returns `None` on
        # success (`health._database_answers`). It also keeps `T` and
        # `BaseException` through to the `raise` and the `return` without a cast.
        returned: list[T] = []
        raised: list[BaseException] = []

        def _runner() -> None:
            if not owned.acquire(blocking=False):
                # The caller took this call back because `start()` raised. It
                # has already released the guard and is raising, so there is no
                # one to return an answer to. ⛔ Return WITHOUT touching the
                # database: opening a connection nobody waits for is precisely
                # the accumulation `SingleFlight` exists to prevent.
                return
            try:
                returned.append(work())
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below.
                raised.append(exc)
            finally:
                # ⛔ In the WORKER's finally, not the caller's. The caller leaves
                # first by construction — that is what the deadline is — so a
                # release on its side would re-admit the next call while this
                # thread still held its connection.
                if single_flight is not None:
                    single_flight.release()
                done.set()

        threading.Thread(target=_runner, name=f"bounded-db-{name}", daemon=True).start()
    except BaseException:
        # `Thread.start()` raises `RuntimeError: can't start new thread` when the
        # interpreter cannot get one from the OS. What this branch guarantees is
        # NOT "no thread exists" — it cannot know that, see `owned` above — but
        # this, which is the property that actually matters:
        #
        #   **the guard is released exactly once, and never while work is
        #   outstanding.**
        #
        # Winning `owned` means `_runner` has not begun the work and will now
        # stand down at that same lock, so releasing here abandons nothing.
        # Losing it means `_runner` owns the operation and its `finally` will
        # release — later, when the work it is really doing is really over.
        #
        # Without a release on this path the guard stays busy for the LIFE OF
        # THE PROCESS, and the consequence is worse than the stall it guards
        # against: every `/health/ready` then returns 503 `DatabaseBusyError`
        # against a database answering in milliseconds, while `/health/live` —
        # `async`, touching nothing — keeps returning 200. kubelet never
        # restarts it. The pod sits NotReady forever, and the one symptom an
        # operator can see says the DATABASE is busy.
        #
        # ⚠️ That shape is NEW here, and it is why this is a fix rather than a
        # note. Before this module the same exhaustion drained all fifteen anyio
        # tokens in ~150 s and killed the API, which at least got the pod
        # replaced. Surviving wedged is a regression on being killed.
        #
        # ⚠️ The claim is attempted UNCONDITIONALLY, on its own line and before
        # any `single_flight` test, because winning it is ALSO what stands
        # `_runner` down — and that has to happen whether or not there is a
        # guard to release. Folding it into the `if` below the `is not None`
        # test would leave the runner opening a connection for a caller that
        # has already gone.
        this_call_is_ours = owned.acquire(blocking=False)
        if this_call_is_ours and single_flight is not None:
            single_flight.release()
        raise

    if not done.wait(budget_seconds):
        logger.warning(
            "abandoned a database operation that exceeded its budget",
            extra={"operation": name, "budget_seconds": budget_seconds},
        )
        raise DatabaseBudgetExceededError(name, budget_seconds)

    if raised:
        raise raised[0]
    return returned[0]
