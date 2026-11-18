"""The property, not the file: a heartbeat that STOPS ADVANCING must fail the check.

## What this suite refuses to be

A heartbeat test that asserts "the file exists" is worthless, and it is worthless
in a specific, predictable way: it passes against an implementation that touches
the file once at start-up and never again — which is the exact defect a heartbeat
exists to make impossible. The file is not the property. The property is *the
recorded instant moves forward when the loop does work, and only then*.

So every test below drives the real `worker.run` loop on an injected clock and
asserts on the verdict `worker_heartbeat.check` returns, which is the thing
kubelet will actually read.

## The four properties, and the mutant each one kills

1. **Written AFTER the work.** `test_the_heartbeat_is_not_written_before_the_work`
   and `test_the_recorded_instant_is_when_the_tick_finished` both go red against
   an implementation that advances the heartbeat at the top of the tick. That
   mutant is not hypothetical — it is the natural way to write this, and it
   produces a probe that stays green through a query blocked on a lock forever.
2. **A tick that raised did not complete.** `test_a_loop_whose_ticks_all_fail_goes_stale`
   goes red against the same mutant, and against one that advances the heartbeat
   in a `finally`.
3. **A swallowed failure is transient, not terminal.**
   `test_a_swallowed_tick_failure_does_not_permanently_stop_the_heartbeat`
   goes red against an implementation that stops writing after the first
   exception — the opposite over-correction.
4. **The bound is DERIVED from the interval.**
   `test_a_sixty_second_worker_is_not_killed_by_a_thirty_second_bound` goes red
   against any constant bound, including one that happens to be right at the
   default 5s interval.

## Why the clock is injected rather than slept through

The bound is 30s at the default cadence. A suite that proved staleness by
sleeping would cost half a minute per assertion and would still be a race on a
loaded runner. `worker._now`, `worker._sleep` and `worker_heartbeat._now` are
module-level seams for exactly this, and patching those three rather than the
shared `time` module object keeps the fake clock inside this test.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - the probe entrypoint is only real as a subprocess
import sys
from pathlib import Path

import pytest

import worker
import worker_heartbeat
from config import Settings

_SRC = Path(__file__).resolve().parents[2] / "src"


class _Clock:
    """A wall clock the test moves by hand."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(tmp_path / "worker" / "heartbeat.json"))
    return Settings()


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tick_impl,
    ticks: int,
    *,
    interval: float = 5.0,
    clock: _Clock | None = None,
) -> tuple[int, _Clock, dict[str, int]]:
    """Run the REAL `worker.run` loop for exactly `ticks` iterations.

    `tick_impl(settings, n)` stands in for the database work and may raise.
    Returns `(exit code, clock, {"n": ticks actually run})`.
    """
    clock = clock or _Clock()
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", str(interval))
    monkeypatch.setattr(worker, "preflight", lambda _settings: [])
    monkeypatch.setattr(worker, "_now", clock)
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    monkeypatch.setattr(worker, "_sleep", lambda seconds: clock.advance(seconds))
    # Real handlers would outlive this test — `monkeypatch` restores the
    # attribute, not the process's disposition for SIGTERM.
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)

    calls = {"n": 0}

    def _tick(inner_settings: Settings):
        calls["n"] += 1
        return tick_impl(inner_settings, calls["n"])

    monkeypatch.setattr(worker, "tick", _tick)

    class _StopAfter(worker._Stop):  # type: ignore[misc, valid-type]
        @property  # type: ignore[override]
        def requested(self) -> bool:
            return calls["n"] >= ticks

        @requested.setter
        def requested(self, _value: bool) -> None:
            """The dataclass `__init__` assigns this; the count is the authority."""

    monkeypatch.setattr(worker, "_Stop", _StopAfter)

    return worker.run(settings, once=False), clock, calls


def _idle(_settings: Settings, _n: int):
    return (0, 0, 0)


# ===================================================== the bound is DERIVED ===


def test_the_bound_is_a_multiple_of_the_interval() -> None:
    assert worker_heartbeat.staleness_bound(5.0, multiple=6.0) == 30.0
    assert worker_heartbeat.staleness_bound(60.0, multiple=6.0) == 360.0
    assert worker_heartbeat.staleness_bound(10.0, multiple=3.0) == 30.0


def test_the_floor_only_wins_below_the_derived_term() -> None:
    """`MIN_STALENESS_SECONDS` is a floor, not the bound.

    If the floor won at the DEFAULT interval, the derivation would be dead code
    at the only configuration anyone runs — green here and untested in practice.
    """
    assert worker_heartbeat.staleness_bound(0.5) == worker_heartbeat.MIN_STALENESS_SECONDS
    assert worker_heartbeat.staleness_bound(5.0) > worker_heartbeat.MIN_STALENESS_SECONDS


def test_a_sixty_second_worker_is_not_killed_by_a_thirty_second_bound(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ THE mutation guard for constraint 3.

    A worker deployed with `WORKER_INTERVAL_SECONDS=60` completes a tick, and 45
    seconds later the probe runs. Under any bound that ignores the interval —
    including a hard-coded 30, which is exactly right at the default cadence —
    this pod is killed while it is working perfectly. Under a derived bound
    (6 x 60 = 360s) it passes.
    """
    clock = _Clock()
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=clock(), interval=60.0, ticks=1))

    clock.advance(45.0)
    code, message = worker_heartbeat.check(settings)

    assert code == worker_heartbeat.EXIT_ALIVE, message
    assert "360.0s" in message


def test_the_bound_uses_the_interval_the_loop_recorded_not_the_probes_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two can disagree, and only one of them describes the cadence measured.

    A `WORKER_INTERVAL_SECONDS` edited on the Deployment reaches the probe's
    environment the moment the pod is recreated, but a still-running loop keeps
    the interval it started with. Deriving the bound from the environment would
    measure the manifest; deriving it from the heartbeat measures the loop.
    """
    clock = _Clock()
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", "5")
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=clock(), interval=120.0, ticks=1))

    clock.advance(100.0)

    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_ALIVE


def test_the_stale_multiple_is_configurable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=clock(), interval=60.0, ticks=1))
    clock.advance(200.0)

    assert worker_heartbeat.check(settings, multiple=6.0)[0] == worker_heartbeat.EXIT_ALIVE
    assert worker_heartbeat.check(settings, multiple=2.0)[0] == worker_heartbeat.EXIT_STALE


# ============================================== freshness, at the boundary ===


def test_no_heartbeat_at_all_is_a_refusal(settings: Settings) -> None:
    """Not a pass. A process that has never completed a tick is not alive in the
    sense this probe measures, and the message must say which of the two states
    it is rather than making the reader guess."""
    code, message = worker_heartbeat.check(settings)
    assert code == worker_heartbeat.EXIT_NO_HEARTBEAT
    assert code != worker_heartbeat.EXIT_ALIVE
    assert "not completed a single tick" in message


def test_an_unreadable_heartbeat_is_a_refusal(settings: Settings) -> None:
    path = worker_heartbeat.heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    code, message = worker_heartbeat.check(settings)
    assert code == worker_heartbeat.EXIT_UNREADABLE
    assert "cannot be read" in message


def test_a_heartbeat_missing_its_instant_is_a_refusal(settings: Settings) -> None:
    path = worker_heartbeat.heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticks": 4}), encoding="utf-8")

    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_UNREADABLE


def test_the_boundary_is_the_bound(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=clock(), interval=5.0, ticks=1))
    bound = worker_heartbeat.staleness_bound(5.0)

    clock.advance(bound - 0.1)
    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_ALIVE

    clock.advance(0.2)
    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_STALE


def test_a_clock_that_moved_backwards_is_not_read_as_a_stall(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat stamped in the future means the clock moved, not that the loop
    stopped. Reading a negative age as enormous freshness would be as wrong as
    reading it as staleness; it is clamped and passes."""
    clock = _Clock()
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=clock() + 500.0, interval=5.0, ticks=1))

    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_ALIVE


# ================================== the loop: advancing, and stopping to ===


def test_a_running_idle_loop_stays_green(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that makes the other half meaningful: an idle worker — every
    counter zero, every relay table empty, which is production today — must pass
    the check. Without this, a probe that always failed would satisfy the
    staleness tests."""
    code, clock, calls = _drive(monkeypatch, settings, _idle, ticks=8)

    assert code == 0
    assert calls["n"] == 8
    verdict, message = worker_heartbeat.check(settings)
    assert verdict == worker_heartbeat.EXIT_ALIVE, message
    assert worker_heartbeat.read(settings).ticks == 8  # type: ignore[union-attr]


def test_a_heartbeat_that_stops_advancing_fails_the_check(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔ THE property. The loop works, then stops; the clock keeps going."""
    _code, clock, _calls = _drive(monkeypatch, settings, _idle, ticks=3)
    assert worker_heartbeat.check(settings)[0] == worker_heartbeat.EXIT_ALIVE

    clock.advance(worker_heartbeat.staleness_bound(5.0) + 1.0)

    code, message = worker_heartbeat.check(settings)
    assert code == worker_heartbeat.EXIT_STALE
    assert "not finishing ticks" in message


def test_a_loop_whose_ticks_all_fail_goes_stale(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick that raised did NOT complete, so it must not advance the heartbeat.

    The shape is the real one: the worker was healthy, then the database went
    away. One tick succeeds, eleven raise, `run` swallows every one of them and
    keeps looping — and the probe, not the process, is what notices.

    ⚠️ Also asserts the loop did not exit. Converting a stalled tick into a crash
    is a different behaviour with a different blast radius, and it is explicitly
    NOT what this change does.
    """

    def _fails_after_the_first(_settings: Settings, n: int):
        if n == 1:
            return (0, 0, 0)
        raise RuntimeError("connection to server was lost")

    code, _clock, calls = _drive(monkeypatch, settings, _fails_after_the_first, ticks=12)

    assert code == 0, "the loop must keep running; a stalled tick is not a crash"
    assert calls["n"] == 12, "every tick after the failure must still have been attempted"

    verdict, message = worker_heartbeat.check(settings)
    assert verdict == worker_heartbeat.EXIT_STALE, message
    heartbeat = worker_heartbeat.read(settings)
    assert heartbeat is not None
    assert heartbeat.ticks == 1, "only the completed tick advanced the heartbeat"


def test_a_loop_that_never_completes_a_tick_never_writes_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_fails(_settings: Settings, _n: int):
        raise RuntimeError("the first tick never got to the end")

    _drive(monkeypatch, settings, _always_fails, ticks=6)

    assert worker_heartbeat.read(settings) is None
    assert worker_heartbeat.check(settings)[0] != worker_heartbeat.EXIT_ALIVE


def test_a_swallowed_tick_failure_does_not_permanently_stop_the_heartbeat(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction. A transient failure is transient: the loop swallows
    it, the next completed tick advances the heartbeat again, and the check is
    green across the whole sequence. An implementation that stopped writing after
    the first exception would fail here."""
    instants: list[float] = []

    def _one_bad_tick(_settings: Settings, n: int):
        if n == 2:
            raise RuntimeError("a transient blip")
        return (0, 0, 0)

    _code, clock, _calls = _drive(monkeypatch, settings, _one_bad_tick, ticks=4)

    heartbeat = worker_heartbeat.read(settings)
    assert heartbeat is not None
    assert heartbeat.ticks == 3, "three ticks completed; the fourth attempt raised"
    assert heartbeat.errors == 1
    instants.append(heartbeat.at)

    verdict, message = worker_heartbeat.check(settings)
    assert verdict == worker_heartbeat.EXIT_ALIVE, message


# ============================ ⛔ AFTER the work, which is the whole argument ===


def test_the_heartbeat_is_not_written_before_the_work(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔ THE mutation guard for constraint 1.

    Read the heartbeat from INSIDE the tick. On the first tick there must be
    nothing at all; on the Nth there must still be the (N-1)th. An
    implementation that advances the heartbeat at the top of the loop body
    reports `ticks=N` here and this test goes red — which is the point, because
    that implementation stays green forever through a tick that never returns.
    """
    seen: list[worker_heartbeat.Heartbeat | None] = []

    def _observe(_settings: Settings, _n: int):
        seen.append(worker_heartbeat.read(settings))
        return (0, 0, 0)

    _drive(monkeypatch, settings, _observe, ticks=4)

    assert seen[0] is None, "a heartbeat existed before the first tick did any work"
    assert [h.ticks for h in seen[1:]] == [1, 2, 3]  # type: ignore[union-attr]


def test_the_recorded_instant_is_when_the_tick_finished(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same property measured on the clock rather than on the counter.

    The tick takes 40 seconds of wall time. The heartbeat must record the moment
    it finished, not the moment it started — a heartbeat stamped at the start is
    already 40 seconds stale the instant it is written, and on a tick that hangs
    it is a timestamp for work that never happened.
    """
    clock = _Clock()
    started_at = clock()

    def _slow(_settings: Settings, _n: int):
        clock.advance(40.0)
        return (0, 0, 0)

    _drive(monkeypatch, settings, _slow, ticks=1, clock=clock)

    heartbeat = worker_heartbeat.read(settings)
    assert heartbeat is not None
    assert heartbeat.at == started_at + 40.0
    assert heartbeat.at != started_at


def test_a_heartbeat_write_failure_does_not_stop_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unwritable heartbeat path is a reporting failure, not a work failure.

    The loop must keep relaying. It must also SAY so, because the probe will now
    read this pod as stalled and the next person needs to know the restart was
    about the filesystem.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(blocker / "heartbeat.json"))
    settings = Settings()

    with caplog.at_level("ERROR", logger="evidence_review.worker"):
        code, _clock, calls = _drive(monkeypatch, settings, _idle, ticks=3)

    assert code == 0
    assert calls["n"] == 3
    assert any("could not write the heartbeat" in record.message for record in caplog.records)


# ======================== the probe is a real command, run as a subprocess ===


def _probe(path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_SRC)
    environment["WORKER_HEARTBEAT_PATH"] = str(path)
    environment.pop("SHADOW_MODE", None)
    return subprocess.run(  # nosec B603 - fixed argv, no shell, interpreter is sys.executable
        [sys.executable, "-m", "worker_heartbeat"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def test_the_probe_runs_as_a_module_and_exits_zero_on_a_fresh_heartbeat(tmp_path: Path) -> None:
    """`exec` probe, no HTTP server, no new port. If `python -m worker_heartbeat`
    is not runnable as a bare command then the whole design is unwireable, and
    nothing that imports the module in-process can tell you that."""
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"at": __import__("time").time(), "interval": 5.0, "ticks": 3}), encoding="utf-8")

    result = _probe(path)
    assert result.returncode == 0, result.stderr
    assert "alive" in result.stdout


def test_the_probe_exits_non_zero_on_a_stale_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    stale = __import__("time").time() - 600.0
    path.write_text(json.dumps({"at": stale, "interval": 5.0, "ticks": 3}), encoding="utf-8")

    result = _probe(path)
    assert result.returncode != 0
    assert "past the" in result.stdout


def test_the_probe_exits_non_zero_when_there_is_no_heartbeat(tmp_path: Path) -> None:
    result = _probe(tmp_path / "never-written.json")
    assert result.returncode != 0
