"""A failed fan-out leaves a POISONED ROW, and a connection fault is not N of them.

`13-…:§8.2` requires a rights-safe poison queue whose rows "page the owning
team". `relay._record_poisoned` existed to write those rows and could not: it
re-wrote the IDENTICAL `(event_id, tenant_id, consumer, replay_of=NULL)` tuple
the failing insert had just written, changing only `state`, `poison_reason` and
`poison_detail`. No constraint on `event_deliveries` mentions those three
columns, so there was no refusal that could reject the pending row and accept
the poisoned one. Three separately demonstrated failures each produced **zero**
poisoned rows, and a previous session reported the defect fixed while it was
not — which is why this file asserts the ROWS, in the database, and never a
return value or a log line alone.

## The three failures, and what each one now does

| failure                                     | refused by                             | poisoned row? |
|---------------------------------------------|----------------------------------------|---------------|
| a duplicate registration                     | `uq_event_deliveries_consumer_event`   | **yes** — it names the original in `replay_of_delivery_id`, which the partial index does not cover |
| a consumer named `flow-projector`            | `ck_event_deliveries_consumer_shape`   | **yes** — attributed to `invalid_consumer`, the real name in `poison_detail` |
| `permission denied` on `event_deliveries`    | the grant                              | **no**, and honestly so — a role that cannot INSERT cannot INSERT a row about that |

The third is not a gap to be closed later. It is the one case where nothing can
be persisted, and what this file holds there is the OBSERVABILITY: the log line
must carry the driver's own reason. The line it replaces asserted a cause it had
never tested — "which happens exactly when a delivery for this (event, consumer)
pair already exists" — and that sentence was false for both of the other two
failures above.

## ⚠️ Isolation

Same two hazards as `test_an_event_reaches_the_log_and_no_further.py`, same two
answers: a per-RUN tenant (nothing truncates between runs) and assertions that
name our own `event_id`s (the relay reads `domain_outbox` with no tenant
predicate; only RLS narrows it, and RLS is bypassed for a superuser).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, text  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

RUN = uuid.uuid4().hex[:12]
TENANT = tenant(f"tenant-poison-lands-{RUN}")
TENANT_UUID = uuid.UUID(TENANT)
LEAD = "lead-poison"

_MAX_PASSES = 50


class _RecordingLogger:
    """A stand-in for `relay.logger` that keeps `(event, kwargs)`.

    Asserting on the LOG is the point of two tests here — the defect they hold
    is that the relay said something untrue about a failure it could not
    persist — and structlog's rendering is configuration this file must not
    depend on. A recorder is the smallest thing that observes the call.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    def of(self, event: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == event]


def _actor(role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=LEAD, role=ActorRole(role))


def _consumer(suffix: str) -> str:
    """A name that satisfies `ck_event_deliveries_consumer_shape`, per test."""
    return f"c_{suffix}_{RUN}"


def _registration(name: str, event_type: str) -> Any:
    from domain.policies.consumer_registry import ConsumerRegistration

    return ConsumerRegistration(
        name=name,
        owner="fan-out-test",
        events=frozenset({event_type}),
        effect="none; this registration exists to drive the relay's failure path",
    )


def _create_review(title: str) -> uuid.UUID:
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="poison",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        return review.review_id


def _drain() -> None:
    """Relay until a pass moves nothing, so the outbox holds no backlog.

    Called BEFORE each test arms its registry patch: the relay reads the outbox
    globally, and an un-relayed row from an earlier module would otherwise be
    fanned out under this test's deliberately broken registration.
    """
    from application.services import relay
    from database import actor_scope

    for _ in range(_MAX_PASSES):
        with actor_scope(_actor()) as session:
            relayed, _ = relay.relay_pending(session)
        if relayed == 0:
            return
    raise AssertionError("the relay made 50 passes without draining; it is not making progress")


def _event_id_of(review_id: uuid.UUID) -> uuid.UUID:
    from database import actor_scope

    with actor_scope(_actor()) as session:
        row = session.execute(
            text("SELECT event_id FROM domain_outbox WHERE aggregate_id = :a AND event_type = 'review.created.v1'"),
            {"a": review_id},
        ).one_or_none()
    assert row is not None, "the review committed no review.created.v1, so there is nothing to relay"
    return row[0]


def _deliveries(event_id: uuid.UUID) -> list[dict[str, Any]]:
    from database import actor_scope

    with actor_scope(_actor()) as session:
        rows = session.execute(
            text(
                "SELECT delivery_id, consumer, state, poison_reason, poison_detail, replay_of_delivery_id "
                "  FROM event_deliveries WHERE event_id = :e ORDER BY state, consumer"
            ),
            {"e": event_id},
        ).mappings()
        return [dict(r) for r in rows]


def _relay_once(monkeypatch: pytest.MonkeyPatch, *, consumers: tuple[str, ...] | None = None) -> Any:
    """One relay pass with the registry replaced, returning the `RelayResult`."""
    from application.services import relay
    from database import actor_scope

    if consumers is not None:
        monkeypatch.setattr("domain.policies.consumer_registry.consumers_for", lambda _t: consumers)
    with actor_scope(_actor()) as session:
        return relay.relay_pending(session)


# ──────────────────────────────────────────────────────────────────────────
# 1. A duplicate registration — the unique index
# ──────────────────────────────────────────────────────────────────────────


def test_a_duplicate_registration_leaves_a_poisoned_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same consumer registered twice: one delivery, one POISONED row.

    `consumers_for` returns the name twice, so the second insert collides on
    `uq_event_deliveries_consumer_event`. That is the failure the audit
    demonstrated, and it produced zero poisoned rows because the poison INSERT
    reproduced the colliding tuple exactly. It now names the original in
    `replay_of_delivery_id`, which takes the row out of the partial index's
    `WHERE replay_of_delivery_id IS NULL` scope entirely.
    """
    from domain.policies import consumer_registry

    _drain()
    name = _consumer("dup")
    registration = _registration(name, "review.created.v1")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (registration, registration))

    review_id = _create_review("duplicate-registration")
    event_id = _event_id_of(review_id)
    result = _relay_once(monkeypatch)

    rows = _deliveries(event_id)
    pending = [r for r in rows if r["state"] == "pending"]
    poisoned = [r for r in rows if r["state"] == "poisoned"]

    assert len(pending) == 1, f"expected exactly one delivery to land, got {pending}"
    assert len(poisoned) == 1, (
        "the duplicate registration failed its insert and left NO poisoned row. This is the "
        f"exact defect the audit demonstrated three times over; deliveries for the event: {rows}"
    )
    assert result.poisoned == 1, f"the pass did not report the poisoning it performed: {result!r}"
    assert (event_id, name) in result.poisoned_events

    poison = poisoned[0]
    assert poison["consumer"] == name, "the poisoned row must be attributed to the consumer that failed"
    assert poison["poison_reason"] == "handler_failed_repeatedly", (
        "the reason must be one of 0022's four; ck_event_deliveries_poison_reason_registered is closed"
    )
    assert poison["replay_of_delivery_id"] == pending[0]["delivery_id"], (
        "the poisoned row must name the delivery it derives from — that is both what makes it a "
        "non-original the partial unique index ignores, and what a reader needs to see WHICH "
        "delivery the second attempt collided with"
    )
    assert poison["poison_detail"], "a poisoned row with no detail cannot be triaged"


# ──────────────────────────────────────────────────────────────────────────
# 2. A consumer name the CHECK refuses
# ──────────────────────────────────────────────────────────────────────────


def test_a_refused_consumer_name_leaves_a_poisoned_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """`flow-projector` reaches the relay: no delivery, but a POISONED row.

    Registration itself now refuses this name (`tests/unit/
    test_a_consumer_name_is_validated_where_it_is_registered.py`), so this test
    injects it at the relay's own seam — `consumers_for`, the only thing
    `_fan_out` reads. Two defences, because a stream that silently delivers
    nothing is not a defect anybody notices.

    The poisoned row cannot carry the offending name: the identical
    `ck_event_deliveries_consumer_shape` applies to it. So it is attributed to
    `invalid_consumer` and the real name goes into `poison_detail`, which
    nothing constrains.
    """
    from application.services import relay

    _drain()
    review_id = _create_review("refused-consumer-name")
    event_id = _event_id_of(review_id)
    result = _relay_once(monkeypatch, consumers=("flow-projector",))

    rows = _deliveries(event_id)
    assert [r for r in rows if r["state"] == "pending"] == [], (
        "the database accepted a delivery for a consumer whose name violates "
        "ck_event_deliveries_consumer_shape; the constraint is not doing its job"
    )
    poisoned = [r for r in rows if r["state"] == "poisoned"]
    assert len(poisoned) == 1, (
        "a consumer name the CHECK refuses produced NO poisoned row — the poison insert carried "
        f"the same refused name and was refused by the same constraint. Rows: {rows}"
    )
    assert result.poisoned == 1

    poison = poisoned[0]
    assert poison["consumer"] == relay._UNSHAPED_CONSUMER, (
        "the poisoned row must be attributed to the shaped stand-in, never to a name derived from "
        "the bad one — a derivation can land on a real consumer and page a team that was not involved"
    )
    assert "flow-projector" in (poison["poison_detail"] or ""), (
        "the real consumer name is the single most useful fact about this failure and it is not in the row anywhere"
    )
    assert poison["replay_of_delivery_id"] is None, (
        "nothing existed for this (event, consumer) pair, so the poisoned row is the first row and "
        "has nothing to derive from"
    )


# ──────────────────────────────────────────────────────────────────────────
# 3. permission denied — nothing can be written, and the log must say why
# ──────────────────────────────────────────────────────────────────────────


def _owner_engine() -> Any:
    url = os.getenv("MIGRATION_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("no MIGRATION_DATABASE_URL; cannot revoke a privilege from the runtime role")
    return create_engine(url, future=True, connect_args={"prepare_threshold": 0})


def _runtime_role() -> str:
    from sqlalchemy import create_engine as _ce

    engine = _ce(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            name, is_super = conn.execute(
                text("SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")
            ).one()
        if is_super:
            pytest.skip(
                "the runtime role is a SUPERUSER, so a REVOKE cannot deny it anything and this "
                "test would pass for the wrong reason. See tests/conftest.py."
            )
        return str(name)
    finally:
        engine.dispose()


def test_a_permission_denial_records_nothing_and_names_the_real_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """No INSERT privilege: no poisoned row, and the log states the DRIVER's reason.

    A role that cannot INSERT into `event_deliveries` cannot INSERT a poisoned
    row saying so, and no design fixes that. What was fixable is the sentence
    the relay printed instead: it asserted the poison row fails "exactly when a
    delivery for this (event, consumer) pair already exists", a cause it never
    tested and which is FALSE here — nothing exists for this pair at all.
    """
    from application.services import relay
    from domain.policies import consumer_registry

    role = _runtime_role()
    _drain()
    name = _consumer("denied")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    review_id = _create_review("permission-denied")
    event_id = _event_id_of(review_id)

    recorder = _RecordingLogger()
    monkeypatch.setattr(relay, "logger", recorder)

    engine = _owner_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(f"REVOKE INSERT ON event_deliveries FROM {role}"))
        result = _relay_once(monkeypatch)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"GRANT INSERT ON event_deliveries TO {role}"))
        engine.dispose()

    assert _deliveries(event_id) == [], "a row was written by a role that has no INSERT privilege"
    assert result.poisoned == 0, "the relay reported poisoning a row it could not write"

    unrecorded = recorder.of("relay.fan_out.delivery_unrecorded")
    assert len(unrecorded) == 1, f"the unrecordable failure was not reported once: {recorder.calls}"
    entry = unrecorded[0]
    assert "permission denied" in (entry.get("poison_error") or ""), (
        f"the log does not carry the driver's own reason the poisoned row could not be written; got {entry!r}"
    )
    assert "already exists" not in repr(entry), (
        "the relay is still stating the invented cause. The poisoned row was refused for a "
        "PERMISSION denial and nothing about this (event, consumer) pair exists at all."
    )


# ──────────────────────────────────────────────────────────────────────────
# 4. A connection-level fault is ONE abort, not one poison event per consumer
# ──────────────────────────────────────────────────────────────────────────


def test_a_connection_fault_is_one_abort_not_one_poison_event_per_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend is terminated mid-fan-out. Three consumers, ONE log line.

    A dead connection fails identically for every consumer and none of them is
    at fault. The relay used to emit a `delivery_failed` line AND a false
    "already exists" reason for each remaining consumer, and then
    `relay_pending`'s own flush raised anyway — so the transaction outcome was
    right and only the record of it was a lie. Correct rollback with an
    incorrect account of why is exactly the shape that sends the next person
    to the wrong subsystem.

    The kill is real (`pg_terminate_backend` on this session's own backend from
    a second connection), not a raised double: the classification under test is
    of what the DRIVER actually produces.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from application.services import relay
    from database import actor_scope
    from domain.policies import consumer_registry

    _drain()
    names = tuple(_consumer(f"conn{i}") for i in range(3))
    monkeypatch.setattr(
        consumer_registry,
        "REGISTERED",
        tuple(_registration(n, "review.created.v1") for n in names),
    )

    _create_review("connection-fault")

    recorder = _RecordingLogger()
    monkeypatch.setattr(relay, "logger", recorder)

    killed: list[int] = []
    real_consumers_for = consumer_registry.consumers_for

    def kill_then_answer(event_type: str) -> tuple[str, ...]:
        answer = real_consumers_for(event_type)
        if answer and not killed:
            killed.append(1)
            _terminate(_backend_pid_of_the_open_session())
        return answer

    session_pid: list[int] = []

    def _backend_pid_of_the_open_session() -> int:
        return session_pid[0]

    monkeypatch.setattr(consumer_registry, "consumers_for", kill_then_answer)

    raised: Exception | None = None
    try:
        with actor_scope(_actor()) as session:
            session_pid.append(int(session.execute(text("SELECT pg_backend_pid()")).scalar_one()))
            relay.relay_pending(session)
    except SQLAlchemyError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001 - the rollback on the way out can raise its own
        raised = exc

    assert killed, "the backend was never terminated, so this test measured an ordinary relay pass"
    assert raised is not None, (
        "a terminated backend did not propagate out of the relay pass. The caller owns the "
        "transaction and must be told it is gone."
    )

    aborted = recorder.of("relay.fan_out.aborted")
    failed = recorder.of("relay.fan_out.delivery_failed")
    unrecorded = recorder.of("relay.fan_out.delivery_unrecorded")

    assert len(aborted) == 1, f"expected exactly one abort line for one connection fault: {recorder.calls}"
    assert failed == [], (
        f"{len(failed)} consumers were blamed for one connection fault. None of them is at fault "
        "and none of their deliveries was ever attempted against a live connection."
    )
    assert unrecorded == [], "a connection fault produced 'could not record a poison row' noise per consumer"


def _terminate(pid: int) -> None:
    """Kill `pid`'s backend from a SEPARATE connection, and wait for it to die."""
    from sqlalchemy import create_engine as _ce

    engine = _ce(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            conn.commit()
    finally:
        engine.dispose()
