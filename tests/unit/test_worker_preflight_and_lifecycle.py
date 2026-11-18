"""Four defects the audit measured directly in `worker.py`, each with a test.

None of these need a real database: `run` and `preflight` are exercised with
`preflight` and `tick` monkeypatched, because the defects here are about
control flow (what runs before what, which thread can install a signal
handler) rather than about what a query returns. `tests/integration/
test_two_workers_and_a_reaper.py` already covers the real claim/reap path
against Postgres.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

import worker
from config import Settings

# ============================================ 1. six cross-tenant tables, not four ===


def test_cross_tenant_tables_includes_the_job_tables() -> None:
    """`reap_expired_leases` reads `durable_jobs` with no tenant predicate and
    `_seal_open_attempt` does the same on `job_attempts` — both rely entirely on
    the `durable_jobs_worker` / `job_attempts_worker` policies migration `0023`
    creates. Leaving either out of `CROSS_TENANT_TABLES` reproduces the exact
    "looks healthy, does nothing" failure this file exists to prevent: preflight
    reports zero problems, the worker starts, and the reaper silently reaps
    zero leases forever.
    """
    assert set(worker.CROSS_TENANT_TABLES) == {
        "domain_outbox",
        "research_domain_events",
        "event_deliveries",
        "domain_inbox",
        "durable_jobs",
        "job_attempts",
    }


def test_the_module_docstring_no_longer_claims_four_tables() -> None:
    """The comment above `CROSS_TENANT_TABLES` used to say "the four tables the
    relay and the reaper touch" — wrong even before this fix, since the reaper
    always touched `durable_jobs`/`job_attempts` too. Pinned so a future edit
    that adds a seventh table without updating the prose is caught here rather
    than only read by the next confused engineer.
    """
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "the four tables" not in source
    assert re.search(r"six tables", source)


# ================================== 2. a malformed WORKER_INTERVAL_SECONDS ===


@pytest.mark.parametrize("raw", ["abc", "", "1.2.3", "  "])
def test_bad_interval_is_a_named_problem_not_a_crash(raw: str) -> None:
    interval, problem = worker._parse_interval(raw)
    assert interval is None
    assert problem is not None
    assert raw in problem or repr(raw) in problem


@pytest.mark.parametrize("raw", ["0", "-1", "-0.5"])
def test_non_positive_interval_is_refused(raw: str) -> None:
    interval, problem = worker._parse_interval(raw)
    assert interval is None
    assert problem is not None
    assert "positive" in problem


def test_good_interval_parses_clean() -> None:
    interval, problem = worker._parse_interval("2.5")
    assert interval == 2.5
    assert problem is None


def test_preflight_reports_the_bad_interval_as_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    """`preflight` must never let a malformed `WORKER_INTERVAL_SECONDS` reach an
    unguarded `float()` call outside it. Reproduced against the real function:
    before the fix, `run()` parsed the interval BEFORE calling `preflight`, so
    `WORKER_INTERVAL_SECONDS=abc` raised an unhandled `ValueError` and
    `preflight` never ran at all.
    """
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", "not-a-number")

    class _FakeSession:
        def execute(self, *_args, **_kwargs):
            class _Result:
                def scalar_one(self):
                    return "some_other_role"

            return _Result()

    from contextlib import contextmanager

    # `statement_timeout_ms` mirrors the real `database.session_scope`;
    # `preflight` passes `PREFLIGHT_STATEMENT_TIMEOUT_MS`.
    @contextmanager
    def _fake_session_scope(_settings, *, statement_timeout_ms=None):
        yield _FakeSession()

    monkeypatch.setattr(worker, "session_scope", _fake_session_scope)

    problems = worker.preflight(Settings())
    assert any("WORKER_INTERVAL_SECONDS" in problem for problem in problems)


def test_run_reports_bad_interval_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-to-end reproduction: `run(once=True)` with a malformed interval
    must return the config-error exit code, not raise `ValueError` off a raw
    `float()` call that used to sit above `preflight()`.
    """
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", "abc")
    monkeypatch.setattr(worker, "preflight", lambda settings: ["WORKER_INTERVAL_SECONDS='abc' is not a number."])

    exit_code = worker.run(Settings(), once=True)
    assert exit_code == 78


# ========================== 3./4. run(once=True) off the main thread ===


def test_run_once_off_main_thread_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduced: calling `run(once=True)` from a `threading.Thread` used to
    raise `ValueError: signal only works in main thread of the main
    interpreter` BEFORE any tick, because signal handlers were installed
    unconditionally. A test harness or embedding process using the one-shot
    path must get a completed tick, not a crash.
    """
    monkeypatch.setattr(worker, "preflight", lambda settings: [])
    monkeypatch.setattr(worker, "tick", lambda settings: (0, 0, 0))

    outcome: dict[str, object] = {}

    def _target() -> None:
        try:
            outcome["result"] = worker.run(Settings(), once=True)
        except BaseException as exc:  # pragma: no cover - failure path, asserted below
            outcome["error"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "run(once=True) did not return on a worker thread"
    assert "error" not in outcome, f"run(once=True) raised off the main thread: {outcome.get('error')!r}"
    assert outcome["result"] == 0


def test_run_once_does_not_install_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The looping path still wants cooperative shutdown; the one-shot path
    has nothing to cooperate with, since it returns after a single tick
    regardless. `signal.signal` must not even be called for `once=True`.
    """
    monkeypatch.setattr(worker, "preflight", lambda settings: [])
    monkeypatch.setattr(worker, "tick", lambda settings: (0, 0, 0))

    calls: list[tuple] = []
    monkeypatch.setattr(worker.signal, "signal", lambda *args: calls.append(args))

    exit_code = worker.run(Settings(), once=True)

    assert exit_code == 0
    assert calls == []


def test_run_loop_still_installs_signal_handlers_on_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not regress the looping path: on the main thread, with
    `once=False`, handlers are still installed so a real deployment keeps
    cooperative shutdown.
    """
    monkeypatch.setattr(worker, "preflight", lambda settings: [])

    calls = {"n": 0}

    def _fake_tick(_settings):
        calls["n"] += 1
        return (0, 0, 0)

    monkeypatch.setattr(worker, "tick", _fake_tick)

    installed: list[int] = []
    real_signal = worker.signal.signal

    def _capture(signum, handler):
        installed.append(signum)
        return real_signal(signum, handler)

    monkeypatch.setattr(worker.signal, "signal", _capture)

    stop_instance_holder: dict[str, worker._Stop] = {}
    original_stop_cls = worker._Stop

    class _StopThatStopsImmediately(original_stop_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.requested = True
            stop_instance_holder["stop"] = self

    monkeypatch.setattr(worker, "_Stop", _StopThatStopsImmediately)

    exit_code = worker.run(Settings(), once=False)

    assert exit_code == 0
    assert worker.signal.SIGTERM in installed
    assert worker.signal.SIGINT in installed
