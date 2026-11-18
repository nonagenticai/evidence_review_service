"""The three depth gauges are the database, and they say so only when they are.

`worker.py` already counted everything worth counting and nothing exported it.
The queue DEPTH, though, is not in any process's memory — it is a fact about
three tables, and this module measures that the gauges track it.

## ⛔ The measurement that decided the whole design

`domain_outbox`, `research_domain_events` and `event_deliveries` all carry
`FORCE ROW LEVEL SECURITY` with a policy comparing
`current_setting('app.current_tenant', true)::uuid`. A Prometheus scrape has no
request, therefore no actor, therefore no tenant GUC — so the comparand is
`NULL`, every comparison is `NULL`, and `count(*)` is **0**. Measured on this
database, as `evidence_review_app`, with 1,703 un-relayed outbox rows actually
present:

    evidence_review_relay_depth_observable{reason="row_level_security"} 0.0
    (no evidence_review_relay_outbox_unrelayed_rows sample at all)

…and on a connection that CAN see across tenants, at the same instant:

    evidence_review_relay_depth_observable{reason="observable"} 1.0
    evidence_review_relay_outbox_unrelayed_rows 1703.0

`test_a_role_that_cannot_see_across_tenants_exports_no_depth` is the first of
those two, and it is the reason the API's `/metrics` does not simply query the
tables on scrape. A `0` there would have been the most convincing wrong number
this service could produce: it is the value a healthy drained queue has, it is
stable, and it would have stayed 0 through a relay that had been dead for a week.

## Why the gauges are read through a connection this module supplies

The suite's own runtime role is `evidence_review_app` (see `tests/conftest.py`,
which demotes it precisely so RLS means something). That role cannot see across
tenants — which is the point of the first test, and makes the other three
untestable through it. So `RelayDepthCollector` takes its connection as a
parameter, and these tests hand it the OWNER connection `conftest` stashed in
`MIGRATION_DATABASE_URL`, on which RLS is bypassed. Both sides of the branch are
then exercised against a real database rather than a mock of one.

## Deltas, never absolutes

Nothing truncates between tests or between runs, and `relay_pending` reads the
outbox with no tenant predicate. Every assertion here is therefore about how much
a gauge MOVED across one operation. An `== 1` on any of them would pass alone and
fail in a suite, which is a test that reports on the order tests ran in.
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import metrics  # noqa: E402
from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: A fresh tenant per RUN, for the reason `test_an_event_reaches_the_log_and_no_further`
#: gives at length: nothing truncates between runs, so a stable label leaves the
#: previous run's rows inside this run's reach.
RUN = uuid.uuid4().hex[:12]
TENANT = tenant(f"tenant-relay-depth-{RUN}")
TENANT_UUID = uuid.UUID(TENANT)
LEAD = "lead-relay-depth"


def _actor(role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=LEAD, role=ActorRole(role))


@contextmanager
def _cross_tenant_session():  # noqa: ANN201
    """A session on the OWNER connection, where RLS does not narrow the answer.

    `MIGRATION_DATABASE_URL` is where `tests/conftest.py` keeps the inherited
    (owner/superuser) URL after it demotes `DATABASE_URL` to the app role. Built
    per call rather than cached on a module engine so a test that leaves a
    transaction open cannot poison the next one.
    """
    owner_url = os.environ.get("MIGRATION_DATABASE_URL") or DB_URL
    engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
    session = Session(engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()
        engine.dispose()


def _samples(connect) -> dict[str, float]:  # noqa: ANN001
    """Every sample `RelayDepthCollector` emits through `connect`, flattened.

    Keyed by name, with `evidence_review_relay_depth_observable`'s `reason`
    folded into its key — so an ABSENT depth series is a missing dict key rather
    than a zero, which is exactly the distinction under test.
    """
    flat: dict[str, float] = {}
    for family in metrics.RelayDepthCollector(connect).collect():
        for sample in family.samples:
            key = sample.name
            if sample.labels:
                key = f"{sample.name}[{','.join(f'{k}={v}' for k, v in sorted(sample.labels.items()))}]"
            flat[key] = sample.value
    return flat


def _depths(connect) -> tuple[float, float, float]:  # noqa: ANN001
    flat = _samples(connect)
    return (
        flat[metrics.OUTBOX_UNRELAYED],
        flat[metrics.DELIVERIES_PENDING],
        flat[metrics.DELIVERIES_POISONED],
    )


def _create_review(title: str) -> uuid.UUID:
    """One review, and therefore exactly one `review.created.v1` in the outbox.

    Driven through the application service rather than the API, for the reason
    the relay suite gives: the outbox row and the state it describes are one
    transaction and `actor_scope` is where that boundary is.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="relay-depth",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        return review.review_id


def _relay_once() -> tuple[int, int]:
    from application.services import relay
    from database import actor_scope

    with actor_scope(_actor()) as session:
        relayed, deliveries = relay.relay_pending(session)
    return relayed, deliveries


def _one_logged_event_id() -> uuid.UUID:
    """An `event_id` that really made the hop, so a hand-written delivery has a
    row to point its foreign key at."""
    from database import actor_scope

    review_id = _create_review("an-event-for-a-delivery")
    _relay_once()
    with actor_scope(_actor()) as session:
        return uuid.UUID(
            str(
                session.execute(
                    text(
                        "SELECT e.event_id FROM research_domain_events e  WHERE e.aggregate_id = :aggregate_id LIMIT 1"
                    ),
                    {"aggregate_id": review_id},
                ).scalar_one()
            )
        )


# ──────────────────────────────────────────────────────────────────────────
# The branch that decided the design.
# ──────────────────────────────────────────────────────────────────────────


def test_a_role_that_cannot_see_across_tenants_exports_no_depth() -> None:
    """⛔ ABSENT, not zero. The one thing this endpoint must never do.

    The suite's runtime role is `evidence_review_app`, which holds only the
    tenant policy. On a scrape there is no bound tenant, so it sees nothing —
    and a `0` exported from that state is indistinguishable from a drained
    queue, would be stable, and would stay 0 through a relay that died a week
    ago. There is no alert that can be written against a metric with that
    property.
    """
    from database import session_scope

    flat = _samples(lambda: session_scope())

    observable = f"{metrics.DEPTH_OBSERVABLE}[reason={metrics.REASON_ROW_LEVEL_SECURITY}]"
    assert flat.get(observable) == 0.0, (
        "the app role reported that it can see the relay tables across tenants. It cannot: "
        "domain_outbox, research_domain_events and event_deliveries all carry FORCE RLS with a "
        f"tenant-GUC policy and this role holds no USING(true) policy. Samples: {sorted(flat)}"
    )
    for absent in (metrics.OUTBOX_UNRELAYED, metrics.DELIVERIES_PENDING, metrics.DELIVERIES_POISONED):
        assert absent not in flat, (
            f"{absent} was exported by a role that cannot see the rows, so its value is 0 no matter "
            "what is in the queue. A false zero is worse than no metric: it is the value a healthy "
            "drained queue has."
        )


def test_a_role_that_can_see_across_tenants_exports_all_three() -> None:
    """The other side of the same branch — and the control for every test below.

    Without this, a green `test_a_role_that_cannot_see...` could equally mean
    the collector emits nothing under any circumstances.
    """
    flat = _samples(_cross_tenant_session)

    assert flat.get(f"{metrics.DEPTH_OBSERVABLE}[reason={metrics.REASON_OBSERVABLE}]") == 1.0, (
        f"the owner connection was reported as unable to see across tenants. Samples: {sorted(flat)}"
    )
    for present in (metrics.OUTBOX_UNRELAYED, metrics.DELIVERIES_PENDING, metrics.DELIVERIES_POISONED):
        assert present in flat, f"{present} is missing on a connection that can see every row"


# ──────────────────────────────────────────────────────────────────────────
# Each gauge moves with the fact it names.
# ──────────────────────────────────────────────────────────────────────────


def test_an_unrelayed_event_raises_the_outbox_gauge_and_relaying_it_lowers_it() -> None:
    """The relay's own backlog, in both directions.

    Both directions on purpose: a gauge wired to `count(*) FROM domain_outbox`
    with the `NOT EXISTS` dropped would pass the first half of this test forever
    and fail the second, because relaying an event does not delete its outbox
    row — progress is DERIVED (`13-…:§8.1`), never stored. That mutation is the
    single most plausible wrong implementation of this gauge.
    """
    before, _, _ = _depths(_cross_tenant_session)

    _create_review("an-unrelayed-event")
    after_create, _, _ = _depths(_cross_tenant_session)
    assert after_create == before + 1, (
        f"creating one review put {after_create - before} rows in the un-relayed outbox count; "
        "exactly one `review.created.v1` was emitted."
    )

    relayed, _ = _relay_once()
    assert relayed >= 1, "the relay moved nothing, so the second half of this test measures nothing"
    after_relay, _, _ = _depths(_cross_tenant_session)
    assert after_relay < after_create, (
        f"the outbox gauge did not fall when the relay drained it ({after_create} -> {after_relay}). "
        "An outbox row is not deleted when it is relayed — progress is derived from a NOT EXISTS "
        "against research_domain_events — so a gauge that counts domain_outbox alone never falls."
    )


def test_a_pending_delivery_raises_the_pending_gauge() -> None:
    """`event_deliveries` in state `pending`: fanned out, nobody has acknowledged."""
    event_id = _one_logged_event_id()
    _, before, _ = _depths(_cross_tenant_session)

    with _cross_tenant_session() as session:
        _insert_delivery(session, event_id=event_id, consumer=f"c_pending_{RUN}")

    _, after, _ = _depths(_cross_tenant_session)
    assert after == before + 1, (
        f"one pending delivery moved the pending gauge by {after - before}. The gauge is not "
        "counting `state = 'pending'`."
    )


def test_a_poisoned_delivery_raises_the_poisoned_gauge_and_not_the_pending_one() -> None:
    """The counterpart of kag's `kag_callback_outbox_dead_total`.

    `13-…:§8.2` says a poisoned delivery pages the owning team. Until this
    gauge existed the only record of one was a WARNING line in a log stream with
    no Service in front of it.

    ⚠️ It asserts the PENDING gauge does not move too. The two predicates are one
    edit apart (`state = 'pending'` vs `poison_reason IS NOT NULL` over the same
    table), and a poisoned row counted as pending would make a permanently dead
    delivery look like ordinary in-flight work.
    """
    event_id = _one_logged_event_id()
    _, pending_before, poisoned_before = _depths(_cross_tenant_session)

    with _cross_tenant_session() as session:
        _insert_delivery(
            session,
            event_id=event_id,
            consumer=f"c_poison_{RUN}",
            state="poisoned",
            poison_reason="handler_failed_repeatedly",
            poison_detail="the handler failed on every attempt",
        )

    _, pending_after, poisoned_after = _depths(_cross_tenant_session)
    assert poisoned_after == poisoned_before + 1, (
        f"one poisoned delivery moved the poisoned gauge by {poisoned_after - poisoned_before}. "
        "The gauge is not counting `poison_reason IS NOT NULL`."
    )
    assert pending_after == pending_before, (
        f"the pending gauge moved by {pending_after - pending_before} when a POISONED row was "
        "written. A poisoned delivery is never going to be delivered; counting it as pending makes "
        "a permanent loss look like in-flight work."
    )


def _insert_delivery(session: Any, *, event_id: uuid.UUID, consumer: str, **overrides: Any) -> uuid.UUID:
    """A delivery row written by hand.

    Same helper shape as the relay suite's, and for the same reason: the states
    below are reached in production through `relay._fan_out`'s failure path, and
    reproducing that path here would be testing the relay rather than the gauge.
    """
    delivery_id = uuid.uuid4()
    params: dict[str, Any] = {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "tenant_id": TENANT_UUID,
        "consumer": consumer,
        "state": "pending",
        "poison_reason": None,
        "poison_detail": None,
    }
    params.update(overrides)
    session.execute(
        text(
            "INSERT INTO event_deliveries "
            "  (delivery_id, event_id, tenant_id, consumer, state, poison_reason, poison_detail) "
            "VALUES (:delivery_id, :event_id, :tenant_id, :consumer, :state, :poison_reason, :poison_detail)"
        ),
        params,
    )
    return delivery_id
