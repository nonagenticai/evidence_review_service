"""`FND-007` — the event reaches the durable log exactly once, and fans out to
exactly the consumers the registry names for it, and no others.

`13-…:§17` owes this work package a "fan-out, ordering, replay and poison-event
suite". This is it.

## The gate that used to stand here, and why it doesn't anymore (retired 2026-09-04)

Until 2026-09-04 this suite's headline claim was a NEGATIVE one that read like
a bug until you knew why: the log filled and **nothing was delivered**.
`13-…:§8.3` stated the gate — "No service emits a listed document `09` event
until its schema, example, consumer matrix and compatibility test are merged" —
and those four artifacts existed for **zero** of the twenty-one events, so
`consumer_registry.REGISTERED` was empty BY DESIGN and a suite asserting "a
delivery appears" would have been asserting the gate was crossed.

That premise is retired, deliberately, in the same unit of work that rewrote
this file — not abandoned, crossed:

* `research_contracts` 0.9.0 released twelve payload documents covering the
  eighteen wired events, each with an example and a compatibility test.
* `DataCompany` #26 amended `§8.3` so the SLO and retention columns — which
  `§14.2` forbids inventing before Phase 0 — gate the stable-v1 contract
  freeze rather than individual emission. Without that amendment the gate was
  unsatisfiable by any amount of work.
* `consumer_registry.REGISTERED` now names two consumers, `fr` and
  `research_output`, transcribed from `14-…:2204-2211`. `NOT_REGISTERED` names
  four more that document mentions which deliberately have no entry yet.

So the headline inverts: the event reaches the log exactly once AND fans out
to exactly the consumers `consumer_registry.consumers_for(event_type)` names —
no more, no fewer, and never to a name the registry does not carry. A reader
of git history should not read this diff as the gate being given up on; the
two prerequisites above are what crossed it.

## What is actually load-bearing here

* **Exactly once into the log** — the relay is at-least-once by construction (it
  can crash after inserting and before recording that it did), so the log's
  primary key plus `ON CONFLICT (event_id) DO NOTHING` is what makes a repeat
  harmless.
* **Progress is derived, never stored** — `§8.1`: "A single global `consumed_at`
  flag is not sufficient because several projections and services may consume
  the same event." So the absence of a column is a guarantee, and
  `test_progress_is_derived_and_survives_a_crash` asserts the absence directly
  against `information_schema`.
* **Ordering per `§8.2`'s tuple** — `(resource_scope, tenant_id,
  aggregate_type, aggregate_id, aggregate_version)`.
* **Replay refuses an unregistered consumer** — creating deliveries nothing will
  ever claim is how a queue fills with work that will never be done.
* **The log is append-only and receipts are undeletable** — the three server-side
  guards `ER-M022` installs.

## ⚠️ Isolation: nothing truncates, and the relay has no tenant filter

Two independent hazards, and they need two independent answers.

1. Nothing truncates between runs or between modules, so a fixed tenant label
   would make one run's counts depend on whether an earlier one had run. Hence
   this module's own tenant label AND a per-run nonce folded into it: a fresh
   tenant uuid every run, so RLS alone hides every earlier run's rows.
2. `relay_pending` reads `domain_outbox` **GLOBALLY** — look at the SQL, there is
   no tenant predicate. Tenant RLS is the only thing narrowing it, and RLS is
   bypassed entirely for a superuser (`tests/conftest.py` says so at length and
   prints when it could not provision the non-owner role). So on a database
   where the runtime role was not demoted, a relay pass here moves *every*
   tenant's outbox rows.

Which is why **every assertion in this file is about our own `event_id`s, never
about a global `count(*)`**. `assert count(*) == 1` on `research_domain_events`
or `event_deliveries` would be a test that passes alone and fails in a suite —
and the hand-inserted delivery rows further down this file (poison, undeletable)
would corrupt any global count of `event_deliveries`, registered consumer or
not, without any product change at all.

Skipped rather than faked without postgres: the append-only trigger, the
DELETE-revoking trigger, `uq_domain_inbox_consumer_event` and every poison CHECK
are server side. An in-memory double would run this file green while enforcing
none of them.
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

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: A fresh tenant per RUN, not merely per module. Every other module in this
#: directory takes its own label because nothing truncates between MODULES; this
#: one also takes a nonce because nothing truncates between RUNS either, and the
#: relay reads the outbox with no tenant filter. A stable label would leave the
#: previous run's un-relayed rows visible to this one under exactly the
#: condition — a superuser connection, RLS bypassed — that the harness warns
#: about but cannot prevent.
RUN = uuid.uuid4().hex[:12]
TENANT = tenant(f"tenant-event-relay-{RUN}")
TENANT_UUID = uuid.UUID(TENANT)
LEAD = "lead-relay"

#: How many relay passes may run before we call it a loop that does not
#: terminate. `relay_pending` moves at most `DEFAULT_BATCH` (200) rows a pass and
#: this module creates fewer than twenty events, so anything approaching this is
#: a relay that is not making progress rather than a large backlog.
_MAX_PASSES = 50


def _actor(role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=LEAD, role=ActorRole(role))


def _consumer(suffix: str) -> str:
    """A consumer name that satisfies `ck_event_deliveries_consumer_shape`.

    `^[a-z][a-z0-9_]*$`, so the leading character has to be a letter and the
    nonce (lowercase hex) can only follow it. Per-test names because
    `uq_event_deliveries_consumer_event` and `uq_domain_inbox_consumer_event`
    are real, and two tests sharing a name would collide on a constraint neither
    of them is about.
    """
    return f"c_{suffix}_{RUN}"


# ──────────────────────────────────────────────────────────────────────────
# Driving the service, not the API — same argument as
# `test_an_event_commits_with_its_cause.py`: the outbox row and the state it
# describes are one transaction, and `actor_scope` is where that boundary is.
# ──────────────────────────────────────────────────────────────────────────


def _create_review(title: str) -> uuid.UUID:
    """One review, and therefore exactly one `review.created.v1` in the outbox."""
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="relay",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        return review.review_id


def _create_review_with_a_lifecycle(title: str) -> uuid.UUID:
    """Five events on ONE aggregate: created, activated, paused, resumed, closed.

    Several events per aggregate is the whole point — `§8.2` guarantees ordering
    per aggregate, and an aggregate carrying a single event cannot witness an
    ordering guarantee being kept or broken.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="relay",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        protocol = review_svc.create_protocol_version(
            session,
            _actor(),
            review,
            inclusion_criteria={"population": "adults"},
            exclusion_criteria={"design": "not an RCT"},
            stage_definitions=[{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 1}],
            exclusion_reason_vocabulary=["wrong_design"],
            reviewers_required=1,
        )
        review_svc.lock_protocol_version(session, _actor(), protocol)
        review_svc.activate(
            session,
            _actor(),
            review,
            protocol_version_id=protocol.protocol_version_id,
            rationale="ready",
        )
        review_svc.transition(session, _actor(), review, to_state="paused", rationale="waiting")
        review_svc.transition(session, _actor(), review, to_state="active", rationale="resuming")
        review_svc.transition(session, _actor(), review, to_state="closed", rationale="done")
        return review.review_id


def _relay_until_drained() -> tuple[int, int]:
    """Relay in passes until a pass moves nothing. Returns the totals.

    A LOOP rather than a single call, and not for elegance: `relay_pending` is
    bounded at `DEFAULT_BATCH` rows, and on a connection where RLS was bypassed
    it is reading every tenant's outbox. One call would leave a backlog and the
    "a second pass relays nothing" assertion would then be measuring the batch
    limit rather than idempotency.
    """
    from application.services import relay
    from database import actor_scope

    relayed_total = 0
    deliveries_total = 0
    for _ in range(_MAX_PASSES):
        with actor_scope(_actor()) as session:
            relayed, deliveries = relay.relay_pending(session)
        relayed_total += relayed
        deliveries_total += deliveries
        if relayed == 0:
            return relayed_total, deliveries_total
    raise AssertionError(
        f"the relay made {_MAX_PASSES} passes without draining. It is not making progress: either "
        "the log insert is not landing, or `unrelayed_count`'s NOT EXISTS is not seeing rows the "
        "relay just wrote."
    )


def _outbox_row(session: Any, event_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT event_id, event_type, tenant_id, aggregate_type, aggregate_id, aggregate_version, "
                "       payload, content_hash FROM domain_outbox WHERE event_id = :eid"
            ),
            {"eid": event_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _outbox_event_ids(session: Any, aggregate_id: uuid.UUID) -> list[uuid.UUID]:
    return [
        row[0]
        for row in session.execute(
            text("SELECT event_id FROM domain_outbox WHERE aggregate_id = :aid ORDER BY aggregate_version"),
            {"aid": aggregate_id},
        )
    ]


def _outbox_event_ids_of_type(session: Any, aggregate_id: uuid.UUID, event_type: str) -> list[uuid.UUID]:
    """The outbox rows of ONE aggregate that carry ONE event type.

    `consumer_registry.consumers_for` keys on the event TYPE and cannot see an
    event id, so an event type is the only seam a test has for saying "this
    event fails and that one does not". Selecting by it here is what lets
    `test_a_poisoned_row_does_not_roll_back_healthy_rows_in_the_same_batch`
    name the single event it aimed the failure at.
    """
    return [
        row[0]
        for row in session.execute(
            text(
                "SELECT event_id FROM domain_outbox WHERE aggregate_id = :aid AND event_type = :etype "
                "ORDER BY aggregate_version"
            ),
            {"aid": aggregate_id, "etype": event_type},
        )
    ]


def _log_rows(session: Any, event_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT event_id, log_position, event_type, resource_scope, tenant_id, aggregate_type, "
            "       aggregate_id, aggregate_version, payload, content_hash "
            "  FROM research_domain_events WHERE event_id = ANY(:ids) ORDER BY log_position"
        ),
        {"ids": event_ids},
    ).mappings()
    return [dict(row) for row in rows]


def _a_relayed_event() -> uuid.UUID:
    """One event, in the log, whose `event_id` a delivery may legally reference.

    `event_deliveries.event_id` is a foreign key to `research_domain_events`, so
    the hand-written delivery rows below cannot invent an id — they have to point
    at an event that really made the hop.
    """
    from database import actor_scope

    review_id = _create_review("a-relayed-event")
    _relay_until_drained()
    with actor_scope(_actor()) as session:
        ids = _outbox_event_ids(session, review_id)
        assert len(ids) == 1, f"expected one outbox row for a freshly created review, got {len(ids)}"
        rows = _log_rows(session, ids)
    assert len(rows) == 1, "the event did not reach the log, so nothing downstream of it can be tested"
    return rows[0]["event_id"]


def _insert_delivery(session: Any, *, event_id: uuid.UUID, consumer: str, **overrides: Any) -> uuid.UUID:
    """A delivery row written by hand, because no code path creates one.

    The registry is empty, so `relay_pending` fans out to nobody and there is no
    production writer for this table yet. The DATABASE guarantees are still real
    today, and this is the only way to exercise them.
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


def _insert_receipt(session: Any, *, event_id: uuid.UUID, consumer: str) -> uuid.UUID:
    receipt_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO domain_inbox "
            "  (receipt_id, tenant_id, consumer, event_id, aggregate_type, aggregate_id, "
            "   aggregate_version, outcome) "
            "VALUES (:receipt_id, :tenant_id, :consumer, :event_id, 'review', :aggregate_id, 1, 'applied')"
        ),
        {
            "receipt_id": receipt_id,
            "tenant_id": TENANT_UUID,
            "consumer": consumer,
            "event_id": event_id,
            "aggregate_id": uuid.uuid4(),
        },
    )
    return receipt_id


# ──────────────────────────────────────────────────────────────────────────
# 1. The hop, and the fact that it happens once
# ──────────────────────────────────────────────────────────────────────────


def test_an_outbox_row_reaches_the_log_exactly_once() -> None:
    """Step 2 of `§8.1`: the relay inserts the envelope into a durable log.

    A failure of the first half means an event committed with its cause and then
    never left the outbox — the state changed and the log does not know.

    A failure of the second half — a second copy after a second pass — means the
    relay is not idempotent, and since it is at-least-once BY CONSTRUCTION (it
    can crash between the insert and anything that records the insert), every
    crash would then double the log. Consumers would apply the same event twice
    and the inbox would be the only thing between that and a doubled effect.

    ⚠️ Honest about what this measures: the second pass is filtered out by
    `NOT EXISTS` before `ON CONFLICT` is ever reached, so this asserts the
    OBSERVABLE property (no second copy) and not the specific mechanism. The
    conflict clause is the backstop for a concurrent pass, which a single-process
    test cannot stage.

    The payload / `content_hash` / `aggregate_version` equality is what makes
    "reached the log" mean something. A log row that arrived with a different
    payload than the one committed alongside the state is worse than a missing
    one: replay would then faithfully reproduce something that never happened.
    """
    from database import actor_scope

    review_id = _create_review("reaches-the-log-once")

    with actor_scope(_actor()) as session:
        event_ids = _outbox_event_ids(session, review_id)
        outbox = _outbox_row(session, event_ids[0])
    assert len(event_ids) == 1, f"a created review should raise exactly one event, got {len(event_ids)}"
    assert outbox is not None

    from domain.policies import consumer_registry

    expected_consumers = consumer_registry.consumers_for("review.created.v1")
    assert expected_consumers, (
        "`consumer_registry.consumers_for('review.created.v1')` returned nothing. Either the "
        "registry lost its `fr` registration or this test's premise (that review.created.v1 has a "
        "registered consumer) is stale — check `14-…:2204-2211` row 1 before touching the assertion."
    )

    relayed, deliveries = _relay_until_drained()
    assert relayed >= 1, "the relay drained without moving our event"
    assert deliveries == len(expected_consumers), (
        f"the relay created {deliveries} deliveries for one review.created.v1 event; "
        f"`consumer_registry.consumers_for` names {expected_consumers}. A mismatch means `_fan_out` "
        "stopped consulting the registry, or consulted a different event_type than the one it relayed."
    )

    with actor_scope(_actor()) as session:
        delivered_consumers = {
            row[0]
            for row in session.execute(
                text("SELECT consumer FROM event_deliveries WHERE event_id = ANY(:ids)"),
                {"ids": event_ids},
            )
        }
    assert delivered_consumers == set(expected_consumers), (
        f"deliveries for our event name {sorted(delivered_consumers)}, but the registry names "
        f"{sorted(expected_consumers)} for review.created.v1. Either an unregistered consumer got a "
        "delivery, or a registered one did not."
    )

    with actor_scope(_actor()) as session:
        rows = _log_rows(session, event_ids)
    assert len(rows) == 1, f"expected one log row for our event, got {len(rows)}"

    logged = rows[0]
    assert logged["event_id"] == outbox["event_id"], (
        "the log minted a NEW id. `§8.1` step 6 — replay recreates deliveries WITHOUT changing "
        "`event_id` — is meaningless if the id does not survive the hop."
    )
    assert logged["event_type"] == "review.created.v1"
    assert logged["payload"] == outbox["payload"], "the payload changed on the way into the log"
    assert logged["content_hash"] == outbox["content_hash"], (
        "the content hash changed on the way into the log, so the log is not a faithful copy of "
        "what committed with the state"
    )
    assert logged["aggregate_version"] == outbox["aggregate_version"]
    assert str(logged["tenant_id"]) == TENANT

    # The second pass. Scoped to OUR event id — a global count would be a
    # different test on every run, see the module docstring.
    second_relayed, second_deliveries = _relay_until_drained()
    assert second_relayed == 0, (
        f"a second relay pass moved {second_relayed} rows after the first drained the outbox. "
        "Progress is derived from `NOT EXISTS ... research_domain_events`; if that predicate does "
        "not see the rows the relay just wrote, every pass re-relays the whole log."
    )
    assert second_deliveries == 0

    with actor_scope(_actor()) as session:
        copies = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = :eid"),
            {"eid": outbox["event_id"]},
        ).scalar_one()
    assert copies == 1, f"the log holds {copies} copies of one event id"


# ──────────────────────────────────────────────────────────────────────────
# 2. Fan-out: exactly the consumers the registry names, and no others
# ──────────────────────────────────────────────────────────────────────────


def test_the_relay_creates_exactly_the_deliveries_the_registry_names() -> None:
    """`13-…:§8.3`'s gate, crossed, asserted as a fact about this database.

    Until 2026-09-04 this test asserted the opposite — that the registry was
    empty and nothing was delivered — because the gate had not been crossed:
    zero of the twenty-one events had a schema, example, consumer matrix and
    compatibility test merged. `research_contracts` 0.9.0 and `DataCompany` #26
    (see the module docstring) crossed it, and `consumer_registry.REGISTERED`
    now names `fr` and `research_output`.

    So the claim inverts, but the discipline that motivated the old test does
    not: a delivery to a consumer the registry does NOT name for that event is
    exactly as much a bug now as a delivery to anybody was before. Registering
    a consumer ahead of its event's four artifacts, or `_fan_out` drifting from
    what `consumer_registry.consumers_for` says, both fail here.
    `tests/unit/test_no_consumer_outruns_its_schema.py` is the other half of
    that tripwire; this is the half that watches the relay rather than the
    registry.

    ⚠️ Scoped to our own event ids on purpose. `test_a_delivery_cannot_be_deleted`
    and `test_a_poisoned_delivery_must_name_a_registered_reason` below insert
    delivery rows BY HAND, naming per-test consumers that are never in the
    registry — so a global `SELECT count(*) FROM event_deliveries` would depend
    on test ordering and would fail without any product change.
    """
    from database import actor_scope
    from domain.policies import consumer_registry

    review_id = _create_review_with_a_lifecycle("registered-fan-out")
    _relay_until_drained()

    with actor_scope(_actor()) as session:
        event_ids = _outbox_event_ids(session, review_id)
        assert len(event_ids) == 5, f"expected the five lifecycle events, got {len(event_ids)}"
        rows = session.execute(
            text("SELECT event_id, event_type FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": event_ids},
        ).all()
        logged_by_id = {row[0]: row[1] for row in rows}
        delivery_rows = session.execute(
            text("SELECT event_id, consumer FROM event_deliveries WHERE event_id = ANY(:ids)"),
            {"ids": event_ids},
        ).all()

    assert len(logged_by_id) == 5, f"only {len(logged_by_id)} of 5 events reached the log"

    delivered_consumers_by_id: dict[uuid.UUID, set[str]] = {eid: set() for eid in event_ids}
    for event_id, consumer in delivery_rows:
        delivered_consumers_by_id.setdefault(event_id, set()).add(consumer)

    registered = consumer_registry.registered_names()
    not_registered = frozenset(consumer_registry.NOT_REGISTERED)

    for event_id in event_ids:
        event_type = logged_by_id[event_id]
        expected = set(consumer_registry.consumers_for(event_type))
        actual = delivered_consumers_by_id[event_id]
        assert actual == expected, (
            f"{event_type} ({event_id}) was delivered to {sorted(actual)}, but the registry names "
            f"{sorted(expected)}. Either the registry drifted from what `_fan_out` consulted, or a "
            "consumer was registered ahead of that event's schema/example/consumer-matrix/"
            "compatibility test."
        )
        assert actual <= registered, (
            f"{event_type} ({event_id}) was delivered to {sorted(actual - registered)}, which "
            "`consumer_registry.registered_names()` does not carry — a delivery nobody registered "
            "for is work nothing will ever claim."
        )
        assert not (actual & not_registered), (
            f"{event_type} ({event_id}) was delivered to {sorted(actual & not_registered)}, which "
            "`consumer_registry.NOT_REGISTERED` names as deliberately absent (no real inbox yet). "
            "Creating deliveries for it is the exact failure `NOT_REGISTERED` documents against."
        )


# ──────────────────────────────────────────────────────────────────────────
# 3. Progress is derived, so a crash cannot lose it
# ──────────────────────────────────────────────────────────────────────────


def test_progress_is_derived_and_survives_a_crash() -> None:
    """`§8.1`, verbatim: a single global `consumed_at` flag is NOT sufficient.

    The reason is stated in the same sentence — "several projections and services
    may consume the same event" — so one flag on the event can only ever record
    that SOMEBODY consumed it, which is not a question anybody needs answered.
    Progress belongs in `event_deliveries`, one row per (event, consumer).

    The absence of the column IS the guarantee, so it is asserted directly rather
    than inferred from behaviour. A `consumed_at` on `domain_outbox` would look
    like a harmless denormalisation right up until the second consumer arrives,
    and by then there would be rows depending on it.

    The crash half: because un-relayed is a `NOT EXISTS` against the log, it is
    correct after ANY interruption. There is no flag to be set-but-not-committed,
    so a relay killed between the insert and the "mark it relayed" it does not
    have cannot lose or repeat work. `unrelayed_count` reaching 0 is that same
    derivation read as a number — the numerator of the outbox-age metric
    `15-…` owes a dashboard for.
    """
    from application.services import relay
    from database import actor_scope

    _create_review_with_a_lifecycle("derived-progress")
    _relay_until_drained()

    with actor_scope(_actor()) as session:
        remaining = relay.unrelayed_count(session)
        outbox_columns = {
            row[0]
            for row in session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'domain_outbox'")
            )
        }
        log_columns = {
            row[0]
            for row in session.execute(
                text("SELECT column_name FROM information_schema.columns  WHERE table_name = 'research_domain_events'")
            )
        }

    assert remaining == 0, (
        f"{remaining} outbox rows are still un-relayed after the relay drained. Since 'un-relayed' "
        "is derived as NOT EXISTS against the log, a non-zero count here means the log insert did "
        "not land — not that a flag was missed."
    )

    forbidden = {"consumed_at", "consumed", "relayed", "relayed_at", "delivered", "delivered_at"}
    assert not (outbox_columns & forbidden), (
        f"`domain_outbox` grew {sorted(outbox_columns & forbidden)}. `§8.1` forbids exactly this: a "
        "single global flag cannot answer 'has THIS consumer seen it', and once rows depend on the "
        "column the derivation can no longer be the authority."
    )
    # The log carries `relayed_at` — that is WHEN THE RELAY RAN, not consumption —
    # so only the consumption-shaped names are forbidden there.
    consumption_flags = {"consumed_at", "consumed", "delivered", "delivered_at"}
    assert not (log_columns & consumption_flags), (
        f"`research_domain_events` grew a consumption flag: {sorted(log_columns & consumption_flags)}. "
        "Consumption is per-consumer and lives in `event_deliveries`."
    )
    assert "log_position" in log_columns, "no log position, so replay has no boundary to resume from"


# ──────────────────────────────────────────────────────────────────────────
# 4. Ordering
# ──────────────────────────────────────────────────────────────────────────


def test_the_relay_reads_in_aggregate_order() -> None:
    """`§8.2`: ordering is guaranteed per `(resource_scope, resource_tenant,
    aggregate_type, aggregate_id)`, using `aggregate_version`.

    The relay's `ORDER BY` is that exact tuple, so `log_position` — a sequence,
    assigned on insert — comes out ascending in it. What that buys is a
    position-based replay boundary that means something: a consumer resuming
    "after position N" resumes after a consistent prefix of every aggregate,
    rather than in the middle of one.

    Asserted as an IMPLICATION within each aggregate — ascending `log_position`
    implies ascending `aggregate_version` — rather than as an exact expected list
    of positions. Positions are a global sequence shared with every other tenant
    and every other test in the session, so the gaps between ours are not ours to
    predict; the ORDER within one aggregate is.

    A failure means a consumer reading the log in position order sees version 7
    before version 3. `§8.2` puts the deferral obligation on the CONSUMER and
    this test does not pretend the relay discharges it — but a relay that emits
    out of order turns deferral from an edge case into the normal path, and a
    consumer that defers everything makes no progress at all.

    Guarded against vacuity: several aggregates, several events each. A
    single-row group satisfies "ascending" trivially and would prove nothing.
    """
    from database import actor_scope

    review_ids = [_create_review_with_a_lifecycle(f"ordering-{n}") for n in range(3)]
    _relay_until_drained()

    with actor_scope(_actor()) as session:
        event_ids: list[uuid.UUID] = []
        for review_id in review_ids:
            event_ids.extend(_outbox_event_ids(session, review_id))
        rows = _log_rows(session, event_ids)

    assert len(rows) == len(event_ids) == 15, (
        f"expected 15 log rows across 3 aggregates, got {len(rows)} for {len(event_ids)} outbox rows"
    )

    by_aggregate: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:  # `rows` is already ordered by log_position
        by_aggregate.setdefault(row["aggregate_id"], []).append(row)

    assert len(by_aggregate) == 3, f"expected 3 aggregates, got {len(by_aggregate)}"

    for aggregate_id, group in by_aggregate.items():
        assert len(group) >= 2, (
            f"aggregate {aggregate_id} carries {len(group)} event(s); an ordering assertion over "
            "fewer than two is vacuous"
        )
        positions = [row["log_position"] for row in group]
        versions = [row["aggregate_version"] for row in group]
        assert positions == sorted(positions), f"the grouping lost log order for {aggregate_id}: {positions}"
        assert versions == sorted(versions), (
            f"aggregate {aggregate_id} is in the log out of version order: {versions} at positions "
            f"{positions}. A consumer reading by position sees a later revision before an earlier one."
        )
        assert len(set(versions)) == len(versions), (
            f"two log rows claim the same revision of {aggregate_id}: {versions}"
        )
        # The tuple `§8.2` orders by, held constant within the group so that the
        # per-aggregate claim above is the whole claim.
        assert {row["resource_scope"] for row in group} == {"tenant"}
        assert {str(row["tenant_id"]) for row in group} == {TENANT}
        assert {row["aggregate_type"] for row in group} == {"review"}


# ──────────────────────────────────────────────────────────────────────────
# 5. Replay
# ──────────────────────────────────────────────────────────────────────────


def test_replay_refuses_an_unregistered_consumer() -> None:
    """Step 6, and the refusal that keeps it honest.

    `replay` mints NEW delivery rows for events already in the log, pointing
    `replay_of_delivery_id` at the original and never touching `event_id`. For a
    consumer nobody registered, those rows would be work that will never be
    claimed — a queue that grows, alerts on its own depth, and has no worker that
    could ever drain it.

    So the refusal is loud NOW rather than a metric somebody notices in a month.
    A failure here means `replay` will happily create deliveries addressed to a
    name that does not exist, and since the registry is empty today that is EVERY
    name.
    """
    from api.problem_details import ValidationFailedError
    from application.services import relay
    from database import actor_scope

    with pytest.raises(ValidationFailedError) as excinfo:
        with actor_scope(_actor()) as session:
            relay.replay(session, consumer="nobody")

    message = str(excinfo.value)
    assert "nobody" in message, f"the refusal does not name the consumer it refused: {message}"

    # And nothing was written on the way to refusing.
    with actor_scope(_actor()) as session:
        orphans = session.execute(text("SELECT count(*) FROM event_deliveries WHERE consumer = 'nobody'")).scalar_one()
    assert orphans == 0, "the refusal happened after rows were created, which is not a refusal"


# ──────────────────────────────────────────────────────────────────────────
# 6-7. The server-side guards
# ──────────────────────────────────────────────────────────────────────────


def test_the_log_is_append_only() -> None:
    """A durable log whose rows can be edited after the fact is not a log.

    `ER-M022` states the consequence in the trigger's own message: replay "would
    reproduce something that never happened". An edited event is worse than a
    lost one, because every downstream projection rebuilt from the log would
    agree with the edit and nothing would disagree.

    Two halves, deliberately: the GRANT (`REVOKE UPDATE, DELETE`) and the
    TRIGGER. The runtime role hits the grant and gets `42501`; the owner sails
    past it and hits the trigger's `restrict_violation`. Either is a refusal and
    the test accepts both — but if the harness could not demote the connection
    (`tests/conftest.py` prints loudly when it could not), only the trigger is
    doing the work, and half the guard is missing while this test stays green.
    """
    from database import actor_scope

    event_id = _a_relayed_event()

    with pytest.raises(DBAPIError) as updated:
        with actor_scope(_actor()) as session:
            session.execute(
                text("UPDATE research_domain_events SET payload = '{}'::jsonb WHERE event_id = :eid"),
                {"eid": event_id},
            )
    assert "research_domain_events" in str(updated.value)

    with pytest.raises(DBAPIError) as deleted:
        with actor_scope(_actor()) as session:
            session.execute(
                text("DELETE FROM research_domain_events WHERE event_id = :eid"),
                {"eid": event_id},
            )
    assert "research_domain_events" in str(deleted.value)

    with actor_scope(_actor()) as session:
        survivors = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = :eid"),
            {"eid": event_id},
        ).scalar_one()
    assert survivors == 1, "the row is gone, so one of the refusals above refused and deleted anyway"


def test_a_delivery_cannot_be_deleted() -> None:
    """A deletable delivery makes "did this consumer ever see it?" unanswerable.

    `event_deliveries` MUTATES by design — pending, claimed, acknowledged — so it
    cannot be append-only. What it can be is undeletable, which is the weakest
    guard that still keeps the question decidable. Delete the row and the answer
    stops being "no" and becomes "there is no record either way", which reads
    identically to a delivery that was never created.
    """
    from database import actor_scope

    event_id = _a_relayed_event()
    consumer = _consumer("undeletable")

    with actor_scope(_actor()) as session:
        delivery_id = _insert_delivery(session, event_id=event_id, consumer=consumer)

    with pytest.raises(DBAPIError) as deleted:
        with actor_scope(_actor()) as session:
            session.execute(
                text("DELETE FROM event_deliveries WHERE delivery_id = :did"),
                {"did": delivery_id},
            )
    assert "event_deliveries" in str(deleted.value)

    with actor_scope(_actor()) as session:
        survivors = session.execute(
            text("SELECT count(*) FROM event_deliveries WHERE delivery_id = :did"),
            {"did": delivery_id},
        ).scalar_one()
    assert survivors == 1, "the delivery was deleted despite the refusal"


def test_an_inbox_receipt_cannot_be_deleted() -> None:
    """A deleted receipt REOPENS the exactly-once window it exists to close.

    The receipt is what a consumer checks before repeating an effect. Delete it
    and the next delivery of that event — a replay, a redelivery after a crash —
    finds nothing, applies the effect a second time, and writes a fresh receipt
    that looks exactly like the first. The duplicate leaves no trace at all.
    """
    from database import actor_scope

    event_id = _a_relayed_event()
    consumer = _consumer("receipt_undeletable")

    with actor_scope(_actor()) as session:
        receipt_id = _insert_receipt(session, event_id=event_id, consumer=consumer)

    with pytest.raises(DBAPIError) as deleted:
        with actor_scope(_actor()) as session:
            session.execute(
                text("DELETE FROM domain_inbox WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
    assert "domain_inbox" in str(deleted.value)

    with actor_scope(_actor()) as session:
        survivors = session.execute(
            text("SELECT count(*) FROM domain_inbox WHERE receipt_id = :rid"),
            {"rid": receipt_id},
        ).scalar_one()
    assert survivors == 1, "the receipt was deleted despite the refusal"


# ──────────────────────────────────────────────────────────────────────────
# 8. Exactly-once
# ──────────────────────────────────────────────────────────────────────────


def test_the_inbox_refuses_a_second_receipt_for_one_consumer_and_event() -> None:
    """`uq_domain_inbox_consumer_event` IS exactly-once.

    Every other guarantee in this subsystem is ADVISORY without it. The relay is
    at-least-once by construction, `replay` is explicitly not idempotent (a second
    replay creates a second set of deliveries, and the module docstring says so
    rather than pretending otherwise), and a consumer can crash between its
    effect and its acknowledgement. None of that duplicates an effect, and the
    only reason is that step 4 writes the receipt IN THE SAME TRANSACTION as the
    effect — so a repeat collides on this index and the whole transaction, effect
    included, becomes a no-op.

    Lose the index and nothing else in `FND-007` changes shape: the log still
    fills, deliveries still fan out, replay still works. Effects just start
    happening twice, silently, under exactly the conditions the rest of the
    design treats as routine.
    """
    from database import actor_scope

    event_id = _a_relayed_event()
    consumer = _consumer("exactly_once")

    with pytest.raises(IntegrityError) as excinfo:
        with actor_scope(_actor()) as session:
            _insert_receipt(session, event_id=event_id, consumer=consumer)
            session.flush()
            _insert_receipt(session, event_id=event_id, consumer=consumer)
            session.flush()

    assert "uq_domain_inbox_consumer_event" in str(excinfo.value), (
        f"the second receipt was refused by something other than the exactly-once index: {excinfo.value}"
    )

    # A DIFFERENT consumer must still be able to record its own receipt for the
    # same event — the index is per (consumer, event), and one that collided on
    # `event_id` alone would make a second consumer impossible, which is the
    # whole reason there is no global `consumed_at`.
    with actor_scope(_actor()) as session:
        _insert_receipt(session, event_id=event_id, consumer=_consumer("exactly_once_peer"))

    with actor_scope(_actor()) as session:
        receipts = session.execute(
            text("SELECT count(*) FROM domain_inbox WHERE event_id = :eid"),
            {"eid": event_id},
        ).scalar_one()
    assert receipts == 1, (
        f"expected exactly the surviving peer receipt for this event, got {receipts}. The colliding "
        "transaction above should have rolled back both of its inserts."
    )


# ──────────────────────────────────────────────────────────────────────────
# 9. Poison
# ──────────────────────────────────────────────────────────────────────────


def test_a_poisoned_delivery_must_name_a_registered_reason() -> None:
    """`§8.2`'s poison queue is rights-safe only if the reason is a closed set.

    Two CHECKs, and they fail for different reasons — which is why both arms are
    here rather than one:

    * `ck_event_deliveries_poison_names_itself` — `poisoned` and a reason are the
      same fact. A poisoned delivery with a NULL reason pages the owning team
      with nothing to act on, which trains the team to close the page.
    * `ck_event_deliveries_poison_reason_registered` — the vocabulary is CLOSED.
      There is no `other`, because "other" in a failure taxonomy is where every
      unexamined failure goes to be forgotten, and a free-text reason is also a
      place a payload gets copied into an alert.

    ⚠️ Nothing pages yet — there is no alert rule and no runbook wired to this
    state. The state exists so the metric CAN, and `15-…` still owes the
    "poison event" dashboard. This test asserts the schema half only.

    The third arm is not decoration: without a passing insert, both refusals
    above would also pass against a table that refuses every poisoned delivery.
    """
    from database import actor_scope

    event_id = _a_relayed_event()

    with pytest.raises(IntegrityError) as unnamed:
        with actor_scope(_actor()) as session:
            _insert_delivery(
                session,
                event_id=event_id,
                consumer=_consumer("poison_unnamed"),
                state="poisoned",
                poison_reason=None,
            )
            session.flush()
    assert "ck_event_deliveries_poison_names_itself" in str(unnamed.value), (
        f"a poisoned delivery with no reason was refused by something else: {unnamed.value}"
    )

    with pytest.raises(IntegrityError) as unregistered:
        with actor_scope(_actor()) as session:
            _insert_delivery(
                session,
                event_id=event_id,
                consumer=_consumer("poison_invented"),
                state="poisoned",
                poison_reason="because",
            )
            session.flush()
    assert "ck_event_deliveries_poison_reason_registered" in str(unregistered.value), (
        f"an invented poison reason was refused by something else: {unregistered.value}"
    )

    consumer = _consumer("poison_valid")
    with actor_scope(_actor()) as session:
        delivery_id = _insert_delivery(
            session,
            event_id=event_id,
            consumer=consumer,
            state="poisoned",
            poison_reason="handler_failed_repeatedly",
            poison_detail="the handler failed on every attempt",
        )

    with actor_scope(_actor()) as session:
        row = (
            session.execute(
                text(
                    "SELECT state, poison_reason, acknowledged_at, lease_expires_at "
                    "  FROM event_deliveries WHERE delivery_id = :did"
                ),
                {"did": delivery_id},
            )
            .mappings()
            .one()
        )

    assert row["state"] == "poisoned"
    assert row["poison_reason"] == "handler_failed_repeatedly", (
        "a registered reason was refused, so the two refusals above prove nothing about the "
        "vocabulary — they would pass against a table that refuses every poisoned delivery"
    )
    # Terminal, so it holds no lease: `ck_event_deliveries_terminal_holds_no_lease`.
    # A poisoned row still holding one would be reaped and retried forever.
    assert row["lease_expires_at"] is None
    assert row["acknowledged_at"] is None, "a poisoned delivery is the failure terminal, not the success one"


# ──────────────────────────────────────────────────────────────────────────
# 10. The repair: one bad row in `_fan_out` cannot block the batch, or the
# relay's future progress, forever. `consumer_registry.py` is out of scope for
# this fix and is never edited by these tests — `monkeypatch`-style manual
# patch/restore reproduces exactly the drift a duplicate `ConsumerRegistration`
# would cause (the audit's own reproduction), without touching the registry.
# ──────────────────────────────────────────────────────────────────────────


#: The event type the duplicate is aimed at, and the reason it is not
#: `review.created.v1`. `consumers_for` takes an event TYPE, so the type is the
#: ONLY thing a test can use to make one event in a batch fail and leave the
#: others alone. `review.created.v1` is emitted by every review this file
#: creates, including the ones that are supposed to be unaffected, so aiming at
#: it hits everything. `review.activated.v1` comes only from
#: `_create_review_with_a_lifecycle`, exactly once per aggregate
#: (`events._ACTIVE_BY_ORIGIN`, `configured -> active`; the later `paused ->
#: active` edge is `review.resumed.v1`), which makes it a real discriminator.
_DUPLICATED_EVENT_TYPE = "review.activated.v1"


def _duplicate_the_first_consumer(real_consumers_for: Any, event_type_to_duplicate: str) -> Any:
    """A `consumer_registry.consumers_for` stand-in that names one consumer
    TWICE for one event type — exactly what a duplicate `ConsumerRegistration`
    entry would produce, and exactly the reproduction the audit used to raise
    `UniqueViolation` on `uq_event_deliveries_consumer_event`.
    """

    def _patched(event_type: str) -> tuple[str, ...]:
        names = real_consumers_for(event_type)
        if event_type == event_type_to_duplicate and names:
            return (names[0], *names)
        return names

    return _patched


def test_a_poisoned_row_does_not_roll_back_healthy_rows_in_the_same_batch() -> None:
    """(a) A batch containing one failing row still lands every healthy row.

    Before this fix, `_fan_out` accumulated every delivery for the WHOLE batch
    behind ONE `session.flush()` at the end of `relay_pending`, with no
    `try`/`except` anywhere in between. A duplicate consumer registration —
    `consumer_registry.consumers_for` naming the same consumer twice for one
    event type — made the SECOND insert for that (event, consumer) pair
    collide on `uq_event_deliveries_consumer_event`. That exception raised out
    of the single flush; `database.actor_scope` rolls back its WHOLE
    transaction on any exception; and since `relay_pending` never commits
    internally, EVERY clean row in the SAME pass — including a completely
    unrelated event's own log row — was lost with it.

    ## ⚠️ REWRITTEN 2026-09-06, twice over. Read both halves.

    **What the assertion used to say.** This test asserted `result.poisoned
    == 0` and explained why: "the duplicate's SECOND insert cannot be rescued
    as a poisoned row either — `_record_poisoned` would write to the exact
    same (event_id, consumer) pair and collide on the identical unique index".
    That was a true description of the code and a FALSE specification. It
    pinned the defect an audit then demonstrated three times over:
    `_record_poisoned` rewrote the identical `(event_id, tenant_id, consumer,
    replay_of=NULL)` tuple, so no constraint could reject the pending row and
    accept the poisoned one, and the entire poison path was unreachable — with
    this assertion holding it in place. `13-…:§8.2` requires the failure to be
    recorded, not skipped. The poisoned row now names the delivery it collided
    with in `replay_of_delivery_id`, which takes it out of the scope of
    `uq_event_deliveries_consumer_event` (partial, `WHERE
    replay_of_delivery_id IS NULL`). See `relay._record_poisoned` and
    `tests/integration/test_a_poisoned_delivery_actually_lands.py`.

    **What the FIXTURE used to say, and could not deliver.** The paragraph
    below used to read "`poisoned_review`'s event hits the duplicated
    consumer, `healthy_review`'s does not", and both reviews were made by
    `_create_review`, so both emitted `review.created.v1`. `consumers_for`
    takes an event TYPE and cannot discriminate by event id, so the duplicate
    reached BOTH events and each left a poisoned row. The stated property —
    event B is unaffected by event A's failure — was never measured here: the
    "healthy" event was a second poisoned event, and its
    `healthy_delivered` set assertion passed only because a poisoned row
    carries the same consumer NAME as the pending row it replaces, so a set of
    names could not tell them apart. Changing `1` to `2` would have made the
    test green while it went on measuring nothing.

    The seam is honest instead: the two aggregates emit DIFFERENT event types,
    and the duplicate is registered against only one of them. A lifecycle
    review emits `review.activated.v1` exactly once (`events._ACTIVE_BY_ORIGIN`,
    `configured -> active`) and a plain `_create_review` never emits it at all,
    so `consumers_for("review.activated.v1")` is a discriminator between the
    two aggregates that `consumers_for("review.created.v1")` could never be.
    One event in the batch fails; the other five — four of them on the SAME
    aggregate as the failure, one on an entirely unrelated one — must land.

    The batch is drained first, deliberately: `relay_pending` reads every
    un-relayed outbox row in the database, so a stray `review.activated.v1`
    left behind by an earlier test would be a second poisoned row and
    `poisoned == 1` would be measuring test ordering rather than the relay.
    """
    from application.services import relay
    from database import actor_scope
    from domain.policies import consumer_registry

    # See the docstring: `poisoned == 1` is only a statement about THIS batch
    # if this batch is the only thing in it.
    _relay_until_drained()

    poisoned_review = _create_review_with_a_lifecycle("batch-survival-poisoned")
    healthy_review = _create_review("batch-survival-healthy")

    with actor_scope(_actor()) as session:
        poisoned_ids = _outbox_event_ids_of_type(session, poisoned_review, _DUPLICATED_EVENT_TYPE)
        sibling_ids = [
            event_id for event_id in _outbox_event_ids(session, poisoned_review) if event_id not in set(poisoned_ids)
        ]
        healthy_ids = _outbox_event_ids(session, healthy_review)
    assert len(poisoned_ids) == 1, (
        f"the lifecycle helper emitted {len(poisoned_ids)} {_DUPLICATED_EVENT_TYPE} events, not one — "
        "this test needs exactly one failing event to attribute a poisoned row to"
    )
    assert len(sibling_ids) == 4, (
        f"expected four other events on the failing aggregate, got {len(sibling_ids)} — the "
        "same-aggregate half of 'the batch survives' is what these carry"
    )
    assert len(healthy_ids) == 1

    expected_consumers = consumer_registry.consumers_for(_DUPLICATED_EVENT_TYPE)
    assert expected_consumers, (
        f"{_DUPLICATED_EVENT_TYPE} has no registered consumer — this test's reproduction needs one to duplicate"
    )
    healthy_consumers = consumer_registry.consumers_for("review.created.v1")
    assert healthy_consumers, "review.created.v1 has no registered consumer, so the healthy half proves nothing"
    duplicated_consumer = expected_consumers[0]

    real_consumers_for = consumer_registry.consumers_for
    consumer_registry.consumers_for = _duplicate_the_first_consumer(real_consumers_for, _DUPLICATED_EVENT_TYPE)
    try:
        with actor_scope(_actor()) as session:
            result = relay.relay_pending(session, limit=50)
    finally:
        consumer_registry.consumers_for = real_consumers_for

    relayed, deliveries = result
    assert relayed >= 6, f"expected both aggregates' six events to reach the log in one pass, got {relayed}"
    assert result.poisoned == 1, (
        f"exactly ONE event in this batch met the duplicated consumer, so exactly one poisoned row is "
        f"owed; got {result.poisoned}. More than one means the duplicate reached an event it was not "
        f"aimed at — which is the fixture defect this test was rewritten to remove — and zero means the "
        f"refused insert left no record at all"
    )

    with actor_scope(_actor()) as session:
        poisoned_logged = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": poisoned_ids},
        ).scalar_one()
        sibling_logged = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": sibling_ids},
        ).scalar_one()
        healthy_logged = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": healthy_ids},
        ).scalar_one()
        healthy_delivered = {
            row[0]
            for row in session.execute(
                text("SELECT consumer FROM event_deliveries WHERE event_id = ANY(:ids)"),
                {"ids": healthy_ids},
            )
        }
        # The property the old fixture claimed and could not hold: the UNRELATED
        # event carries no poisoned row of its own. A count of names could not
        # say this — a poisoned row and a pending row name the same consumer —
        # so it is asked of `state`.
        healthy_poisoned = session.execute(
            text("SELECT count(*) FROM event_deliveries WHERE event_id = ANY(:ids) AND state = 'poisoned'"),
            {"ids": healthy_ids},
        ).scalar_one()
        sibling_poisoned = session.execute(
            text("SELECT count(*) FROM event_deliveries WHERE event_id = ANY(:ids) AND state = 'poisoned'"),
            {"ids": sibling_ids},
        ).scalar_one()
        poisoned_delivered_count = session.execute(
            text(
                "SELECT count(*) FROM event_deliveries WHERE event_id = ANY(:ids) "
                "AND consumer = :consumer AND state <> 'poisoned'"
            ),
            {"ids": poisoned_ids, "consumer": duplicated_consumer},
        ).scalar_one()
        duplicate_poisoned = session.execute(
            text(
                "SELECT count(*) FROM event_deliveries WHERE event_id = ANY(:ids) "
                "AND consumer = :consumer AND state = 'poisoned' AND replay_of_delivery_id IS NOT NULL"
            ),
            {"ids": poisoned_ids, "consumer": duplicated_consumer},
        ).scalar_one()

    assert poisoned_logged == 1, (
        "the poisoned event's own log row did not survive — the pre-fix defect: one bad delivery "
        "insert rolled back the WHOLE transaction, including log rows the batch had already written"
    )
    assert sibling_logged == 4, (
        f"only {sibling_logged} of the failing aggregate's four other events reached the log — a "
        "failure isolated per (event, consumer) must not cost its own aggregate's other events"
    )
    assert healthy_logged == 1, "the healthy event never reached the log at all"
    assert healthy_delivered == set(healthy_consumers), (
        f"the healthy event's deliveries are {sorted(healthy_delivered)}, expected "
        f"{sorted(healthy_consumers)} — one bad row elsewhere in the batch cost this event its own "
        "deliveries"
    )
    assert healthy_poisoned == 0, (
        f"the unrelated event carries {healthy_poisoned} poisoned deliveries. It never met the "
        "duplicated consumer, so a failure has crossed from one event to another — the isolation "
        "this test exists for"
    )
    assert sibling_poisoned == 0, (
        f"{sibling_poisoned} of the failing aggregate's OTHER events were poisoned too; the isolation "
        "is per (event, consumer), not per aggregate"
    )
    assert poisoned_delivered_count == 1, (
        f"expected exactly ONE deliverable row to {duplicated_consumer} for the duplicated event "
        f"(the first insert), got {poisoned_delivered_count}. The duplicate's second insert colliding "
        "on `uq_event_deliveries_consumer_event` must not be doubled into a second deliverable row."
    )
    assert duplicate_poisoned == 1, (
        "the refused second insert left no poisoned row naming the delivery it collided with, so "
        "the failure is invisible to anything but a log line"
    )


def test_the_relay_makes_progress_after_a_poisoned_row_rather_than_re_blocking() -> None:
    """(b) The relay makes progress on the NEXT pass rather than re-blocking on
    the same row.

    This is the actual head-of-line block, not just the batch-rollback half of
    it: before the fix, a failure rolled back the log rows that pass had
    ALREADY written, so the poisoned outbox row was never marked relayed (it
    is a `NOT EXISTS` against the log — see the module docstring) and was
    re-selected, and failed the same way, on EVERY subsequent pass forever.
    Every tenant behind it stopped making progress, permanently, with nobody
    paged.

    This drives the failure once, then asserts the failing event's log row is
    written EXACTLY once (not re-attempted by a follow-up pass), and that work
    created AFTER the failure — on a call that never sees the duplicate patch
    — relays and delivers completely normally. A relay still wedged behind the
    earlier row would either re-log the old event or never reach the new one.
    """
    from application.services import relay
    from database import actor_scope
    from domain.policies import consumer_registry

    poisoned_review = _create_review("no-head-of-line-block")
    with actor_scope(_actor()) as session:
        poisoned_ids = _outbox_event_ids(session, poisoned_review)
    assert len(poisoned_ids) == 1

    expected_consumers = consumer_registry.consumers_for("review.created.v1")
    assert expected_consumers
    real_consumers_for = consumer_registry.consumers_for
    consumer_registry.consumers_for = _duplicate_the_first_consumer(real_consumers_for, "review.created.v1")
    try:
        with actor_scope(_actor()) as session:
            first_pass_relayed, _ = relay.relay_pending(session, limit=50)
    finally:
        consumer_registry.consumers_for = real_consumers_for

    assert first_pass_relayed >= 1, "the first pass did not relay our event at all"

    with actor_scope(_actor()) as session:
        logged_once = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": poisoned_ids},
        ).scalar_one()
    assert logged_once == 1, "the poisoned event's log row did not survive its own pass"

    # A pass over the SAME event, with the registry back to normal, must not
    # re-log it — proof it is not being retried forever.
    with actor_scope(_actor()) as session:
        relay.relay_pending(session, limit=50)
    with actor_scope(_actor()) as session:
        still_logged_once = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": poisoned_ids},
        ).scalar_one()
    assert still_logged_once == 1, f"the event was logged {still_logged_once} time(s) — it is being re-relayed"

    # New work created AFTER the failure must relay and deliver normally —
    # proof the relay is not wedged behind the earlier row.
    later_review = _create_review("no-head-of-line-block-later")
    _relay_until_drained()
    with actor_scope(_actor()) as session:
        later_ids = _outbox_event_ids(session, later_review)
        later_logged = session.execute(
            text("SELECT count(*) FROM research_domain_events WHERE event_id = ANY(:ids)"),
            {"ids": later_ids},
        ).scalar_one()
        later_delivered = {
            row[0]
            for row in session.execute(
                text("SELECT consumer FROM event_deliveries WHERE event_id = ANY(:ids)"),
                {"ids": later_ids},
            )
        }
    assert later_logged == 1, "work created AFTER the poisoned pass never reached the log — the relay is stuck"
    assert later_delivered == set(expected_consumers), (
        f"later work delivered to {sorted(later_delivered)}, expected {sorted(expected_consumers)} — the "
        "relay is still affected by a row it already isolated"
    )


def test_a_fan_out_failure_can_reach_the_poisoned_state() -> None:
    """(c), poisoned half: a delivery failure that is NOT a duplicate key can
    be rescued as a persisted `poisoned` row, not just skipped.

    ⚠️ CORRECTED 2026-09-06. This used to open "the test above proves the SKIP
    fallback, and only the skip fallback: a duplicate consumer collides with
    itself on `uq_event_deliveries_consumer_event` no matter what state the
    retry uses, so that reproduction can never reach a persisted poisoned row."
    That was a description of the defect, not of the design: `_record_poisoned`
    now writes the retry with `replay_of_delivery_id` set, which the partial
    index does not cover, so the duplicate reproduction reaches a poisoned row
    too. What is still only proved HERE is a failure that is not a duplicate
    key at all — a different refusal, and the only one that exercises the
    FIRST `_try_poison` attempt landing rather than the second.

    So this injects a failure that is not a duplicate key: `relay.EventDelivery`
    is wrapped so constructing the FIRST `state="pending"` delivery for one
    target consumer raises. The real-world analogue is a transient failure —
    a serialization conflict under concurrent relay workers, say — that a
    single test connection cannot force deterministically; injecting it is
    the only way to exercise this branch at all. Everything downstream of the
    injected raise is the REAL code and the REAL database: `_record_poisoned`,
    the CHECK constraints, the unique index.
    """
    from application.services import relay
    from database import actor_scope
    from domain.policies import consumer_registry

    review_id = _create_review("fan-out-poisoned-write")
    with actor_scope(_actor()) as session:
        event_ids = _outbox_event_ids(session, review_id)
    assert len(event_ids) == 1

    expected_consumers = consumer_registry.consumers_for("review.created.v1")
    assert expected_consumers, "review.created.v1 has no registered consumer to target"
    target_consumer = expected_consumers[0]

    real_event_delivery = relay.EventDelivery
    triggered = {"count": 0}
    marker = "synthetic fan-out failure, injected by this test"

    def _faulty_event_delivery(**kwargs: Any) -> Any:
        if kwargs.get("state") == "pending" and kwargs.get("consumer") == target_consumer and not triggered["count"]:
            triggered["count"] += 1
            raise SQLAlchemyError(marker)
        return real_event_delivery(**kwargs)

    relay.EventDelivery = _faulty_event_delivery
    try:
        with actor_scope(_actor()) as session:
            result = relay.relay_pending(session, limit=50)
    finally:
        relay.EventDelivery = real_event_delivery

    assert triggered["count"] == 1, "the injected failure never fired — this test proves nothing"
    relayed, _deliveries = result
    assert relayed >= 1, "the event never reached the log"
    assert result.poisoned >= 1, "the injected failure should have produced a persisted poisoned delivery"

    with actor_scope(_actor()) as session:
        rows = (
            session.execute(
                text(
                    "SELECT state, poison_reason, poison_detail, lease_expires_at, acknowledged_at "
                    "  FROM event_deliveries WHERE event_id = ANY(:ids) AND consumer = :consumer"
                ),
                {"ids": event_ids, "consumer": target_consumer},
            )
            .mappings()
            .all()
        )

    assert len(rows) == 1, (
        f"expected exactly one delivery for {target_consumer} on this event, got {len(rows)} — the failed "
        "'pending' attempt must not leave a row behind alongside the poisoned one"
    )
    row = rows[0]
    assert row["state"] == "poisoned"
    assert row["poison_reason"] == "handler_failed_repeatedly"
    assert row["poison_detail"] is not None and marker in row["poison_detail"], (
        f"the poison detail does not carry the injected failure: {row['poison_detail']!r}"
    )
    assert row["lease_expires_at"] is None
    assert row["acknowledged_at"] is None


# ──────────────────────────────────────────────────────────────────────────
# 11. DEFECT 3: the log gets the outbox's closed event-set CHECK (`0024`)
# ──────────────────────────────────────────────────────────────────────────


def test_the_log_rejects_an_uncatalogued_event_type() -> None:
    """(d) `ck_rde_registered_event` (`0024`) closes the gap
    `ck_domain_outbox_registered_event` (`0019`) already closed on the outbox.

    Before `0024`, `research_domain_events` carried shape/regex checks
    (`ck_rde_event_name_shape`, `ck_rde_event_name_not_regulatory`) but no
    closed-set CHECK, so a direct INSERT with an uncatalogued `event_type`
    succeeded — measured live before that migration, with exactly the literal
    value this test uses. `relay.py`'s own copy path never produces one (it
    only ever copies a `domain_outbox` row, which
    `ck_domain_outbox_registered_event` already restricts to the same
    twenty-one names), so this is purely a database-level guarantee,
    deliberately exercised with a hand-written INSERT the way
    `test_the_log_is_append_only` exercises its trigger directly rather than
    through the service.
    """
    from database import actor_scope

    with pytest.raises(IntegrityError) as excinfo:
        with actor_scope(_actor()) as session:
            session.execute(
                text(
                    "INSERT INTO research_domain_events "
                    "  (event_id, event_type, schema_ref, producer, resource_scope, tenant_id, "
                    "   aggregate_type, aggregate_id, aggregate_version, occurred_at, payload) "
                    "VALUES (:event_id, 'review.not_catalogued.v1', 'urn:x:test:1', 'evidence-review', "
                    "        'tenant', :tenant_id, 'review', :aggregate_id, 1, now(), '{}'::jsonb)"
                ),
                {
                    "event_id": uuid.uuid4(),
                    "tenant_id": TENANT_UUID,
                    "aggregate_id": uuid.uuid4(),
                },
            )
    assert "ck_rde_registered_event" in str(excinfo.value), (
        f"an uncatalogued event_type was refused by something other than the new closed-set CHECK: {excinfo.value}"
    )

    with actor_scope(_actor()) as session:
        survivors = session.execute(
            text(
                "SELECT count(*) FROM research_domain_events "
                "WHERE event_type = 'review.not_catalogued.v1' AND tenant_id = :tenant_id"
            ),
            {"tenant_id": TENANT_UUID},
        ).scalar_one()
    assert survivors == 0, "the refused row was written anyway"
