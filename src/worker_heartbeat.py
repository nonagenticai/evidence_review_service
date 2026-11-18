"""The only thing outside `worker.py` that can tell a working loop from a wedged one.

## The gap this closes, as measured

`evidence-review-worker` runs a 5s relay + lease-reaper loop in production and
nothing could observe it:

* it has no HTTP server, so there is **no liveness probe**;
* there is no `Service`, no `ServiceMonitor` and no `PodMonitor` in the
  `evidence-review` namespace — `kubectl -n evidence-review get
  servicemonitor,podmonitor` answered *No resources found*, so there is no
  metric either;
* its entire output over nine hours was two lines, `created SQLAlchemy engine`
  and `worker started: relay + lease reaper, every 5.0s`, because `run()` only
  logs a tick that moved something and all four relay tables are empty;
* `run()` catches, logs and swallows a tick failure and keeps looping — correct,
  and it means a loop that has stopped making progress emits nothing and
  restarts nothing.

Put together: **"working, queue empty" and "hung" are observationally
identical.** Nothing inside or outside the process could distinguish them.

This module is the outside observer. The loop writes a heartbeat file at the end
of every completed tick; a probe runs `python -m worker_heartbeat` in the same
container and exits non-zero when that file has stopped advancing.

## ⛔ Why the heartbeat is written AFTER the work, never before

A heartbeat written at the top of a tick answers "is the scheduler still calling
me?". That is not the question. The failure mode here is a loop whose *body*
stops making progress — a query blocked on a lock with no timeout, a socket read
with no deadline — while the process, the interpreter and the scheduler are all
perfectly alive. A heartbeat written before the work advances at full cadence
through exactly that failure, which would make this file a decorative liveness
signal rather than an observation of the work.

So `advance()` is called only on the success path of the tick, after
`relay_pending` and `reap_expired_leases` have both returned. The consequence is
deliberate and worth stating plainly: a tick that raises does **not** advance the
heartbeat, so a sustained database outage will eventually fail the probe and
restart the pod. That is the intended reading — the heartbeat asserts *work
completed*, not *thread scheduled* — and the operator holds the other half of the
decision in `failureThreshold`. The counter-argument (restarting does not fix a
database outage) is real, which is why the recommended stanza in `README.md`
tolerates ~90s of no completed work before it kills anything.

## ⛔ Why the staleness bound is derived and not a constant

The bound has to answer "how long is too long between two completed ticks", and
that is a question about the loop's own cadence. A hard-coded 30s bound kills a
worker deployed with `WORKER_INTERVAL_SECONDS=60` on its very first tick — the
probe would be reporting the configuration, not the health.

So the bound is `max(MIN_STALENESS_SECONDS, multiple * interval)`, and the
`interval` used is the one **recorded in the heartbeat by the loop that wrote
it**, not the one in the probe's environment. Those can differ — an env change
that the running process has not picked up is exactly the state where a bound
read from the environment would be wrong — and the loop's own number is the only
one that describes the cadence being measured.

`MIN_STALENESS_SECONDS` is a floor, not the bound: it exists so that a very
short interval (`0.5`) does not derive a 3s bound that an ordinary slow tick
against a busy Postgres would trip. At the default 5s interval the derived term
(30s) is larger than the floor (20s), so the derivation is load-bearing at the
default configuration and not only at unusual ones.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from config import Settings, get_settings

LOGGER = logging.getLogger("evidence_review.worker.heartbeat")

#: How many tick intervals of silence are tolerated before the loop is called
#: stale. Six, so an ordinary slow tick, a GC pause and a probe landing at an
#: unlucky moment cannot stack up into a false kill, while a genuinely wedged
#: loop is caught inside half a minute at the default cadence.
DEFAULT_STALE_MULTIPLE = 6.0

#: The floor under the derived bound. See the module docstring: this is what
#: stops a sub-second `WORKER_INTERVAL_SECONDS` from deriving a bound shorter
#: than a single honest tick takes. It is NOT the bound — at the default 5s
#: interval the derived term is 30s and wins.
MIN_STALENESS_SECONDS = 20.0

#: ⚠️ There is deliberately NO `DEFAULT_HEARTBEAT_PATH` here. The default lives
#: on `config.Settings.worker_heartbeat_path` and nowhere else, because the loop
#: and the probe are two processes that must agree on one path — and two
#: constants for one fact is precisely how they would come to disagree, silently,
#: in the environment where only one of them was overridden.
#:
#: Exit codes. `0` is the only one that means "the loop completed a tick
#: recently"; every other path — no file, unreadable file, stale file — is a
#: refusal, because each one of them is a state in which nothing can say the
#: loop is working.
EXIT_ALIVE = 0
EXIT_STALE = 1
EXIT_NO_HEARTBEAT = 2
EXIT_UNREADABLE = 3


def _now() -> float:
    """Wall clock, in epoch seconds.

    Wall clock and not `monotonic`, and that is forced rather than chosen: the
    writer and the reader are two different processes, and a monotonic clock is
    only comparable within one process. Both run in the same container and
    therefore share `CLOCK_REALTIME`, so the one hazard a wall clock carries
    across machines — skew — does not apply here.

    A module-level seam so tests can drive the clock without monkeypatching the
    `time` module object, which is shared with every other importer.
    """
    return time.time()


@dataclass(frozen=True)
class Heartbeat:
    """What one completed tick asserts about itself.

    `at` is the instant the tick FINISHED. `interval` is the cadence the loop is
    actually running at, recorded here so the check can derive its bound from
    the writer rather than from its own environment — see the module docstring.

    The counters are cumulative since process start, not per-tick. Per-tick
    numbers would make an idle worker's heartbeat indistinguishable from a
    freshly restarted one; cumulative numbers let a reader see, from two
    consecutive files or two consecutive log lines, whether anything moved.
    """

    at: float
    interval: float
    ticks: int = 0
    relayed: int = 0
    deliveries: int = 0
    poisoned: int = 0
    reaped: int = 0
    errors: int = 0


def heartbeat_path(settings: Settings) -> Path:
    return Path(settings.worker_heartbeat_path)


def staleness_bound(
    interval: float,
    multiple: float = DEFAULT_STALE_MULTIPLE,
    floor: float = MIN_STALENESS_SECONDS,
) -> float:
    """How long a heartbeat may go unadvanced before the loop is called stale.

    ⛔ DERIVED FROM THE INTERVAL, deliberately, and the relationship is the whole
    point of the function existing separately from its one caller: a constant
    bound would kill a worker configured with a longer interval than the
    constant, and it would do so on the first tick, reporting the configuration
    instead of the health.

    `multiple * interval` is "this many missed ticks". `floor` only takes over
    when the interval is small enough that a handful of them is shorter than one
    honest tick against a live database.
    """
    return max(floor, multiple * interval)


def advance(settings: Settings, heartbeat: Heartbeat) -> None:
    """Record a completed tick.

    Written to a temporary name and `os.replace`d into place, so a reader that
    lands mid-write sees the previous complete heartbeat rather than a truncated
    JSON document. `os.replace` is atomic within a filesystem, and both names are
    in the same directory by construction.

    Raises on failure rather than swallowing: the caller decides what a failed
    heartbeat write means for the loop, and it is the caller that holds the
    context for the log line.
    """
    path = heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(asdict(heartbeat)), encoding="utf-8")
    os.replace(temporary, path)


def read(settings: Settings) -> Heartbeat | None:
    """The last completed tick, or `None` if there is not one this can trust.

    `None` covers three states that a probe must treat identically — the file is
    absent, the file is unreadable, the file is not the document this module
    writes — because in every one of them nothing can say the loop is working,
    which is the only question being asked.
    """
    path = heartbeat_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return Heartbeat(
            at=float(raw["at"]),
            interval=float(raw["interval"]),
            ticks=int(raw.get("ticks", 0)),
            relayed=int(raw.get("relayed", 0)),
            deliveries=int(raw.get("deliveries", 0)),
            poisoned=int(raw.get("poisoned", 0)),
            reaped=int(raw.get("reaped", 0)),
            errors=int(raw.get("errors", 0)),
        )
    except KeyError, TypeError, ValueError:
        return None


def check(settings: Settings, *, multiple: float | None = None) -> tuple[int, str]:
    """`(exit code, one line for the probe's log)`.

    Never raises for an expected state: a probe whose command dies with a
    traceback is indistinguishable, to kubelet, from one that returned non-zero,
    and the traceback is then the only diagnosis anyone gets.
    """
    stale_multiple = settings.worker_heartbeat_stale_multiple if multiple is None else multiple
    path = heartbeat_path(settings)

    if not path.exists():
        return (
            EXIT_NO_HEARTBEAT,
            f"no heartbeat at {path}: this process has not completed a single tick since it "
            "started. Either it is still in preflight, or the first tick has not returned.",
        )

    heartbeat = read(settings)
    if heartbeat is None:
        return (
            EXIT_UNREADABLE,
            f"the heartbeat at {path} exists and cannot be read as one. Treated as a refusal, not "
            "as a pass: an unreadable heartbeat says nothing about whether the loop is working.",
        )

    # Clamped at zero. A negative age means the recorded instant is in the
    # future, which inside one container means the clock moved backwards
    # underneath us; that is not evidence the loop is stalled, and reading it as
    # a huge freshness would be just as wrong.
    age = max(0.0, _now() - heartbeat.at)
    bound = staleness_bound(heartbeat.interval, stale_multiple)

    if age > bound:
        return (
            EXIT_STALE,
            f"the last completed tick was {age:.1f}s ago, past the {bound:.1f}s bound "
            f"({stale_multiple:g} x the {heartbeat.interval:g}s tick interval this loop recorded). "
            f"The loop is running and not finishing ticks: {heartbeat.ticks} completed, "
            f"{heartbeat.errors} failed. relayed={heartbeat.relayed} "
            f"deliveries={heartbeat.deliveries} poisoned={heartbeat.poisoned} "
            f"reaped={heartbeat.reaped}.",
        )

    return (
        EXIT_ALIVE,
        f"alive: last completed tick {age:.1f}s ago, bound {bound:.1f}s. "
        f"ticks={heartbeat.ticks} relayed={heartbeat.relayed} deliveries={heartbeat.deliveries} "
        f"poisoned={heartbeat.poisoned} reaped={heartbeat.reaped} errors={heartbeat.errors}",
    )


def main(settings: Settings | None = None) -> int:
    """The probe entrypoint: `python -m worker_heartbeat`.

    Prints to stdout rather than logging, because kubelet surfaces the command's
    output in the Pod event for a failed probe and nothing surfaces a logger that
    was never configured.
    """
    code, message = check(settings or get_settings())
    print(message)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
