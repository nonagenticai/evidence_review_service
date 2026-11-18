"""`alive and idle` must not look like `dead`, and it did.

## The measurement

`evidence-review-worker`'s entire output over nine hours in production was two
lines — `created SQLAlchemy engine` and `worker started: relay + lease reaper,
every 5.0s`. The loop logs a tick only when it moved something; all four relay
tables are empty; so a healthy worker and a wedged one produced byte-identical
output, which is none.

The heartbeat file (see the sibling suite) is what a machine reads. This suite
covers what a HUMAN reads: a periodic line carrying the counters, so that
scrolling the log answers "is it alive?" without anyone having to exec into the
pod.

## The two failures this has to sit between

* **Silence** — today. Indistinguishable from a stall.
* **Noise** — one line per tick, which at the 5s default is 17,280 lines a day
  of `relayed=0 deliveries=0 reaped=0 poisoned=0`. That is why the previous
  version logged nothing: the only alternative anyone had considered was a
  stream nobody would read, and a stream nobody reads is silence with a
  storage bill.

So `test_the_line_is_not_logged_every_tick` and `test_the_line_returns_after_the
_interval` are a pair, and neither is meaningful alone: the first would be
satisfied by never logging, the second by logging always.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import worker
import worker_heartbeat
from config import Settings


class _Clock:
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
) -> tuple[int, _Clock]:
    clock = _Clock()
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", str(interval))
    monkeypatch.setattr(worker, "preflight", lambda _settings: [])
    monkeypatch.setattr(worker, "_now", clock)
    monkeypatch.setattr(worker_heartbeat, "_now", clock)
    monkeypatch.setattr(worker, "_sleep", lambda seconds: clock.advance(seconds))
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

    return worker.run(settings, once=False), clock


def _idle(_settings: Settings, _n: int):
    return (0, 0, 0)


def _heartbeat_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith("heartbeat:")]


# ================================================= alive, and visibly idle ===


def test_an_idle_worker_says_so(settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """⛔ THE line. Every counter zero — production's actual state — and the log
    must still carry a statement that the loop completed a tick."""
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, settings, _idle, ticks=1)

    lines = _heartbeat_lines(caplog)
    assert len(lines) == 1, lines
    assert "ticks=1" in lines[0]
    for counter in ("relayed=0", "deliveries=0", "poisoned=0", "reaped=0"):
        assert counter in lines[0], f"{counter} missing from {lines[0]!r}"


def test_the_first_completed_tick_logs_immediately(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pod that waited five minutes to say anything about itself would spend
    its entire startup window in exactly the silence this exists to end. The
    first completed tick logs whatever the interval is."""
    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "3600")
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, Settings(), _idle, ticks=1)

    assert len(_heartbeat_lines(caplog)) == 1


def test_the_line_is_not_logged_every_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Twenty ticks at 5s is 100 seconds of wall time, well inside one 300s
    window. One line, not twenty. Paired with the test below — on its own this
    would be satisfied by an implementation that never logs at all."""
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, settings, _idle, ticks=20)

    assert len(_heartbeat_lines(caplog)) == 1


def test_the_line_returns_after_the_interval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """121 ticks at 5s is 600 seconds: the first tick, then two more windows."""
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, settings, _idle, ticks=121)

    assert len(_heartbeat_lines(caplog)) == 3


def test_the_log_interval_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "10")

    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, Settings(), _idle, ticks=21)

    # 21 ticks at 5s = 100s of wall time; the first tick plus one line per 10s.
    assert len(_heartbeat_lines(caplog)) == 11


def test_the_default_log_interval_is_five_minutes() -> None:
    """300s is 288 lines a day — one per five-minute bucket, the resolution a
    log-based alert or a human scrolling actually works at, and short enough
    that "nothing for fifteen minutes" is a three-sample judgement rather than a
    coin flip on one missing line. Pinned because the number is the entire
    difference between this being useful and being noise."""
    assert Settings().worker_heartbeat_log_seconds == 300.0


def test_an_unparseable_log_interval_falls_back_rather_than_logging_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "not-a-number")
    assert Settings().worker_heartbeat_log_seconds == 300.0
    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "0")
    assert Settings().worker_heartbeat_log_seconds == 300.0


# ========================================================= what it carries ===


def test_the_counters_are_cumulative(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Per-tick numbers would make an idle worker's line identical to a freshly
    restarted one's. Cumulative numbers let two consecutive lines answer
    "did anything move?"."""

    def _busy(_settings: Settings, n: int):
        return worker.TickResult(n, n * 2, n * 3, n * 4, ())

    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "1")
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, Settings(), _busy, ticks=3)

    last = _heartbeat_lines(caplog)[-1]
    assert "ticks=3" in last
    assert "relayed=6" in last  # 1 + 2 + 3
    assert "deliveries=12" in last  # 2 + 4 + 6
    assert "reaped=18" in last  # 3 + 6 + 9
    assert "poisoned=24" in last  # 4 + 8 + 12


def test_a_loop_that_is_alive_but_failing_still_says_so(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """⛔ Deliberately OUTSIDE the completed-tick branch.

    `errors=12 ticks=0` is a materially different diagnosis from silence: the
    loop is alive and the work is broken. The tracebacks say what broke; this
    line is what says the loop is still there to break. An implementation that
    only logged on a successful tick would go silent for exactly the incident it
    is most needed in.
    """

    def _always_fails(_settings: Settings, _n: int):
        raise RuntimeError("connection to server was lost")

    # 1s, so all twelve failures land in the captured lines rather than only the
    # first — twelve ticks at 5s is 55s of wall time, one 300s window.
    monkeypatch.setenv("WORKER_HEARTBEAT_LOG_SECONDS", "1")
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, Settings(), _always_fails, ticks=12)

    lines = _heartbeat_lines(caplog)
    assert lines, "a loop whose every tick fails must still report that it is running"
    assert "ticks=0" in lines[-1]
    assert "errors=12" in lines[-1]


def test_the_startup_line_names_the_probe_and_its_bound(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The one line an operator definitely sees has to say where the heartbeat is
    and what bound it will be judged against; otherwise the probe is a fact that
    only exists in a manifest they may not have."""
    with caplog.at_level("INFO", logger="evidence_review.worker"):
        _drive(monkeypatch, settings, _idle, ticks=1)

    started = [record.getMessage() for record in caplog.records if record.getMessage().startswith("worker started")]
    assert len(started) == 1
    assert settings.worker_heartbeat_path in started[0]
    assert "worker_heartbeat" in started[0]
    assert "30.0s bound" in started[0]


# ============================================== the flag on worker.py itself ===


def test_the_healthcheck_flag_checks_instead_of_looping(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m worker --healthcheck` must run the check and must NOT start the
    loop. A flag that fell through to `run()` would start a second relay inside
    the probe."""
    monkeypatch.setattr(worker_heartbeat, "get_settings", lambda: settings)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("--healthcheck started the worker loop")

    monkeypatch.setattr(worker, "run", _must_not_run)

    assert worker.main([worker.HEALTHCHECK_FLAG]) == worker_heartbeat.EXIT_NO_HEARTBEAT

    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=worker_heartbeat._now(), interval=5.0, ticks=1))
    assert worker.main([worker.HEALTHCHECK_FLAG]) == worker_heartbeat.EXIT_ALIVE


def test_no_argument_still_runs_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Without it, a `main` that always health-checked would pass
    the test above."""
    calls: list[int] = []
    monkeypatch.setattr(worker, "run", lambda: calls.append(1) or 0)

    assert worker.main([]) == 0
    assert calls == [1]
