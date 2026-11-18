"""`T4` and `T2`: what the worker reports, and what its refusal tells you to do.

Neither needs a database. `T4` is about a return value being discarded, and `T2`
is about a sentence being wrong; both are properties of `worker.py` itself, and
putting them behind a live-postgres skip would be the
`a-test-skipped-in-ci-is-an-unexercised-claim` shape for no gain.

## `T4`, as measured

`relay.relay_pending` returns a `RelayResult` carrying `.poisoned` — the count of
`(event, consumer)` pairs whose delivery insert failed and was recorded as a
`poisoned` row instead. `13-…:§8.2` says such a row should page the owning team.
`tick` wrote `relayed, delivered = relay.relay_pending(session)` and returned a
plain 3-tuple, so `.poisoned` was dropped on the floor: the worker's only output
line read `relayed=N deliveries=M`, which is exactly what a healthy pass looks
like. A delivery that will never be made was written to the database and
reported by nothing.

## Why `TickResult` is a `tuple` subclass and the test asserts that shape

Returning a 4-tuple would break every `relayed, delivered, reaped = tick(...)`
call site with `ValueError: too many values to unpack` — including the three
lifecycle tests that monkeypatch `tick` to a plain `(0, 0, 0)`. So the counts a
caller already unpacks stay a 3-tuple and the new fact rides as an attribute.
That is a contract, not an implementation detail, and it is pinned below.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

import worker
from application.services.relay import RelayResult
from config import Settings

# ===================================================== T4: the poisoned count ===


@contextmanager
def _no_database(monkeypatch: pytest.MonkeyPatch):
    """`session_scope` yielding an object no test below ever calls a method on.

    `tick`'s two units of work are stubbed out at `relay.relay_pending` and
    `jobs.reap_expired_leases`, so the session is only ever handed through.
    """

    # ⚠️ `statement_timeout_ms` mirrors the real `database.session_scope`, which
    # `tick` now passes `worker.RELAY_TICK_STATEMENT_TIMEOUT_MS` to. A double
    # that does not accept what the real function accepts is a double that has
    # drifted, and it fails with a `TypeError` about the stub rather than
    # anything about the code under test.
    @contextmanager
    def _scope(_settings=None, *, statement_timeout_ms=None):
        yield object()

    monkeypatch.setattr(worker, "session_scope", _scope)
    yield


def _stub_tick_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: RelayResult,
    reaped: list[uuid.UUID] | None = None,
) -> None:
    monkeypatch.setattr(worker.relay, "relay_pending", lambda _session: result)
    monkeypatch.setattr(worker.jobs, "reap_expired_leases", lambda _session: reaped or [])


def test_tick_carries_the_relays_poisoned_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect itself: `tick` used to discard `.poisoned` entirely."""
    event_id = uuid.uuid4()
    result = RelayResult(3, 0, 2, ((event_id, "consumer-a"), (event_id, "consumer-b")))

    with _no_database(monkeypatch):
        _stub_tick_sources(monkeypatch, result=result)
        outcome = worker.tick(Settings())

    assert outcome.poisoned == 2, "tick dropped the relay's poisoned count; nothing can report it"
    assert outcome.poisoned_events == ((event_id, "consumer-a"), (event_id, "consumer-b"))


def test_tick_still_unpacks_as_the_three_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility half. A 4-tuple would break every existing call site.

    The control for the test above: `poisoned` must be ADDED to the return value,
    not spliced into the positional contract.
    """
    with _no_database(monkeypatch):
        _stub_tick_sources(monkeypatch, result=RelayResult(3, 5, 1), reaped=[uuid.uuid4()])
        relayed, delivered, reaped = worker.tick(Settings())

    assert (relayed, delivered, reaped) == (3, 5, 1)


def test_a_clean_tick_reports_zero_poisoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: `poisoned` is not a constant that happens to be right."""
    with _no_database(monkeypatch):
        _stub_tick_sources(monkeypatch, result=RelayResult(1, 4))
        outcome = worker.tick(Settings())

    assert outcome.poisoned == 0
    assert outcome.poisoned_events == ()


def test_the_worker_loop_logs_a_poisoned_delivery(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """End to end through `run`: the count has to reach an operator, not just a field.

    A value carried on a return type nothing reads is the same defect one layer
    up, so the assertion is on the emitted log record and not on `TickResult`.
    """
    event_id = uuid.uuid4()
    monkeypatch.setattr(worker, "preflight", lambda settings: [])
    monkeypatch.setattr(
        worker,
        "tick",
        lambda settings: worker.TickResult(1, 0, 0, 1, ((event_id, "consumer-a"),)),
    )

    with caplog.at_level(logging.INFO, logger="evidence_review.worker"):
        assert worker.run(Settings(), once=True) == 0

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a poisoned delivery produced no warning; the worker reported a healthy pass"
    rendered = " ".join(r.getMessage() for r in warnings)
    assert "poisoned" in rendered
    assert str(event_id) in rendered, "the warning does not name the event nobody will ever receive"
    assert "consumer-a" in rendered

    info = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "poisoned=1" in info, "the counts line still reads like a clean pass"


def test_a_clean_tick_produces_no_poison_warning(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The control for the test above: the warning is conditional, not unconditional.

    Without this, a `LOGGER.warning` emitted on every tick would satisfy the
    assertion above and make the signal worthless in exactly the way an alert
    that always fires is worthless.
    """
    monkeypatch.setattr(worker, "preflight", lambda settings: [])
    monkeypatch.setattr(worker, "tick", lambda settings: worker.TickResult(2, 2, 0))

    with caplog.at_level(logging.INFO, logger="evidence_review.worker"):
        assert worker.run(Settings(), once=True) == 0

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_loop_tolerates_a_tick_that_is_a_plain_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run` must not assume the richer return type.

    `tick` is monkeypatched to `(0, 0, 0)` by the lifecycle suite and by any
    harness embedding this loop. Instrumentation that crashed the worker because
    its return type was plainer than expected would be a worse defect than the
    one being fixed here.
    """
    monkeypatch.setattr(worker, "preflight", lambda settings: [])
    monkeypatch.setattr(worker, "tick", lambda settings: (1, 1, 1))

    assert worker.run(Settings(), once=True) == 0


# ============================ T2: a remediation that actually does something ===


def test_the_prescribed_remediation_is_not_a_no_op() -> None:
    """The message used to say "Run `alembic upgrade head` again once the role exists".

    It cannot work. An alembic revision runs exactly once; `alembic_version` is
    already at or past `0023`, so `upgrade head` does nothing, prints nothing, and
    the operator restarts into the identical refusal having learnt nothing. A
    remediation that cannot work is worse than none: it spends the reader's trust
    before they start looking for the real cause.

    Asserted on the message text rather than on a code comment, because the
    message is what somebody actually reads at 3am.
    """
    remediation = worker._REMEDIATION
    assert "a revision runs once" in remediation
    assert "no-op" in remediation
    assert "create the role BEFORE migrating" in remediation
    assert "NEW revision" in remediation

    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "Run `alembic upgrade head` again" not in source, (
        "the refuted instruction is still in the file; re-running a recorded revision does nothing"
    )
    assert "creates them idempotently" not in source


def test_both_problem_messages_carry_the_remediation() -> None:
    """The policy problem and the privilege problem are separately reachable.

    A database can fail either gate alone, so an operator who hits only one of
    them must still be told what repairs it — attaching the remediation to just
    one message would leave the other reading as a dead end.
    """
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert source.count("+ _REMEDIATION") == 2


# ========================= the two constants must not drift apart ===


def test_the_declared_privileges_cover_every_cross_tenant_table() -> None:
    """`CROSS_TENANT_TABLES` drives the policy check; `REQUIRED_PRIVILEGES` drives
    the grant check. A table present in one and absent from the other is checked
    by exactly one of the two gates, silently — which is the shape of the defect
    the privilege check was added to close.
    """
    assert tuple(worker.REQUIRED_PRIVILEGES) == tuple(worker.CROSS_TENANT_TABLES)
    for table, privileges in worker.REQUIRED_PRIVILEGES.items():
        assert privileges, f"{table} declares no required privilege"
        assert "SELECT" in privileges, f"{table} is read by the worker; SELECT is not optional"


@pytest.mark.parametrize(
    ("table", "privilege"),
    [
        # The relay's own writes. Revoking either is the measured T1 condition.
        ("research_domain_events", "INSERT"),
        ("event_deliveries", "INSERT"),
        # The reaper's, which the four-table version of this list would have missed.
        ("durable_jobs", "UPDATE"),
        ("job_attempts", "INSERT"),
        ("job_attempts", "UPDATE"),
    ],
)
def test_the_write_verbs_the_worker_executes_are_declared(table: str, privilege: str) -> None:
    """Named individually rather than asserted as a whole dict.

    A single equality against the whole mapping would go red for a harmless
    addition and green for nothing else; these five are the verbs whose absence
    produced silence rather than an error, so each is pinned on its own.
    """
    assert privilege in worker.REQUIRED_PRIVILEGES[table]
