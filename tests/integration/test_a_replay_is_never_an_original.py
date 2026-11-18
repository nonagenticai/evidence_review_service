"""`replay` in the case `consumer_registry` prescribes: a late consumer backfills.

`consumer_registry` states the order for adding a consumer — "the consumer gains
a real inbox → it is added here → the relay creates its deliveries going forward
→ `replay` backfills the rest" — and rests the whole argument for leaving four
named consumers unregistered on it: "Leaving it unregistered costs **nothing**."

In exactly that case there is no original delivery for anything already in the
log, because nothing ever fanned out to a consumer that did not exist. `replay`
looked for one, found none, and wrote `replay_of_delivery_id = NULL` — which is
not "no original", it is *being* an original: it occupies the one slot
`uq_event_deliveries_consumer_event` allows per `(event, consumer)`, it counts
as an original in every query that separates the two, and the NEXT replay reads
it as the original it is not. Four of four rows, in the documented use case.

Two more defects sat in the same twenty lines and are held here as well:

* **`limit` was applied BEFORE the event-type filter.** The filter ran in
  Python over a page the database had already truncated, so a page of events
  this consumer does not want returned `created == 0` while matching events sat
  further down the log — measured at `replay(from_position=129, limit=3)`
  returning 0 with a match at position 133. A caller looping "until it returns
  0" stopped there and left the rest unreplayed, silently. Hence the cursor:
  `exhausted` is a fact the caller is told, not one it infers from a count.
* **One flush for the whole page.** A single refused row cost the entire batch —
  the same head-of-line shape `relay_pending` had already been fixed for.

## ⚠️ Isolation

Per-RUN tenant, and every assertion names our own `event_id`s: the log is
global, `replay` has no tenant predicate of its own, and only RLS narrows it.
Every replay below is aimed with a `from_position` floor captured immediately
before the events it is about.
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

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

RUN = uuid.uuid4().hex[:12]
TENANT = tenant(f"tenant-replay-origin-{RUN}")
TENANT_UUID = uuid.UUID(TENANT)
LEAD = "lead-replay"

_MAX_PASSES = 50


def _actor(role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=LEAD, role=ActorRole(role))


def _consumer(suffix: str) -> str:
    return f"c_{suffix}_{RUN}"


def _registration(name: str, *event_types: str) -> Any:
    from domain.policies.consumer_registry import ConsumerRegistration

    return ConsumerRegistration(
        name=name,
        owner="replay-test",
        events=frozenset(event_types),
        effect="none; this registration exists to exercise replay",
    )


def _create_review(title: str) -> uuid.UUID:
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="replay",
            external_key=f"{title}-{uuid.uuid4()}",
            title=title,
        )
        return review.review_id


def _create_review_with_a_lifecycle(title: str) -> uuid.UUID:
    """Five events on one aggregate, only the LAST of which is `closed`.

    The shape the `limit`-before-filter defect needs: several events a consumer
    does not want, and then one it does.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="replay",
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


def _drain() -> None:
    from application.services import relay
    from database import actor_scope

    for _ in range(_MAX_PASSES):
        with actor_scope(_actor()) as session:
            relayed, _ = relay.relay_pending(session)
        if relayed == 0:
            return
    raise AssertionError("the relay made 50 passes without draining; it is not making progress")


def _scalar(sql: str, **params: Any) -> Any:
    from database import actor_scope

    with actor_scope(_actor()) as session:
        return session.execute(text(sql), params).scalar()


def _floor() -> int:
    return int(_scalar("SELECT coalesce(max(log_position), 0) FROM research_domain_events") or 0)


def _log_rows_after(position: int) -> list[dict[str, Any]]:
    from database import actor_scope

    with actor_scope(_actor()) as session:
        rows = session.execute(
            text(
                "SELECT event_id, log_position, event_type FROM research_domain_events "
                " WHERE log_position > :p ORDER BY log_position"
            ),
            {"p": position},
        ).mappings()
        return [dict(r) for r in rows]


def _deliveries_for(consumer: str) -> list[dict[str, Any]]:
    from database import actor_scope

    with actor_scope(_actor()) as session:
        rows = session.execute(
            text(
                "SELECT delivery_id, event_id, state, replay_of_delivery_id, created_at "
                "  FROM event_deliveries WHERE consumer = :c ORDER BY created_at, delivery_id"
            ),
            {"c": consumer},
        ).mappings()
        return [dict(r) for r in rows]


def _replay(consumer: str, *, from_position: int, limit: int | None = None) -> Any:
    from application.services import relay
    from database import actor_scope

    kwargs: dict[str, Any] = {"consumer": consumer, "from_position": from_position}
    if limit is not None:
        kwargs["limit"] = limit
    with actor_scope(_actor()) as session:
        return relay.replay(session, **kwargs)


# ──────────────────────────────────────────────────────────────────────────
# 1. The documented case: a consumer registers late and backfills
# ──────────────────────────────────────────────────────────────────────────


def test_a_backfill_for_a_newly_registered_consumer_writes_no_originals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four events already in the log, then the consumer appears. Zero originals.

    This is `consumer_registry`'s own prescribed order, and it is the case that
    produced 4/4 rows with `replay_of_delivery_id IS NULL` — rows the partial
    unique index, every "how many originals does this event have" query, and the
    next replay all read as originals.
    """
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    for i in range(4):
        _create_review(f"backfill-{i}")
    _drain()

    name = _consumer("late")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    mine = [r for r in _log_rows_after(floor) if r["event_type"] == "review.created.v1"]
    assert len(mine) == 4, f"expected four review.created.v1 in the log after {floor}, got {len(mine)}"

    result = _replay(name, from_position=floor)
    assert result.created == 4, f"the backfill did not recreate every event: {result!r}"

    rows = _deliveries_for(name)
    assert len(rows) == 4
    originals = [r for r in rows if r["replay_of_delivery_id"] is None]
    assert originals == [], (
        "a backfill wrote rows with replay_of_delivery_id IS NULL. That is not 'no original', it "
        "IS an original: it takes the partial unique index's one slot per (event, consumer), it "
        "counts as an original in every query that separates the two, and the next replay will "
        f"name it as the original it is not. Rows: {originals}"
    )
    assert all(r["replay_of_delivery_id"] == r["delivery_id"] for r in rows), (
        "a replay with no ancestor must name ITSELF — a value that resolves to a real delivery and "
        "roots the chain, rather than a dangling id or a NULL that means 'original'"
    )


# ──────────────────────────────────────────────────────────────────────────
# 2. And the SECOND replay does not read the first one's rows as originals
# ──────────────────────────────────────────────────────────────────────────


def test_a_second_backfill_does_not_mistake_the_first_for_an_original(monkeypatch: pytest.MonkeyPatch) -> None:
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    _create_review("second-backfill")
    _drain()

    name = _consumer("twice")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    first = _replay(name, from_position=floor)
    second = _replay(name, from_position=floor)
    assert first.created == 1 and second.created == 1, (
        f"replay is additive by design and both passes must create a row: {first!r} {second!r}"
    )

    rows = _deliveries_for(name)
    assert len(rows) == 2
    assert [r for r in rows if r["replay_of_delivery_id"] is None] == [], (
        "a second replay produced an original; nothing about this consumer has ever had one"
    )
    root, later = rows[0], rows[1]
    assert root["replay_of_delivery_id"] == root["delivery_id"], "the first replay is its own root"
    assert later["replay_of_delivery_id"] == root["delivery_id"], (
        "the second replay must derive from the first — a chain with one root, walkable, and never "
        "a claim that the first row was an original delivery that happened"
    )


# ──────────────────────────────────────────────────────────────────────────
# 3. A REAL original is still named — the control for the two above
# ──────────────────────────────────────────────────────────────────────────


def test_a_replay_still_names_the_original_when_one_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registered BEFORE the event: the relay makes an original, replay names it.

    Without this, "never NULL" could be satisfied by always self-referencing,
    which would throw away the ancestry `§8.1` step 6 exists to record.
    """
    from domain.policies import consumer_registry

    _drain()
    name = _consumer("early")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    floor = _floor()
    _create_review("registered-first")
    _drain()

    rows = _deliveries_for(name)
    assert len(rows) == 1 and rows[0]["replay_of_delivery_id"] is None, (
        f"the relay's own fan-out must write an ORIGINAL, not a replay: {rows}"
    )
    original_id = rows[0]["delivery_id"]

    assert _replay(name, from_position=floor).created == 1
    rows = _deliveries_for(name)
    assert len(rows) == 2
    replayed = [r for r in rows if r["delivery_id"] != original_id]
    assert len(replayed) == 1
    assert replayed[0]["replay_of_delivery_id"] == original_id, (
        "the replay did not name the original it replays; §8.1 step 6 exists to answer 'why do I "
        "have two deliveries for one event'"
    )


# ──────────────────────────────────────────────────────────────────────────
# 4. One refused row costs one row, not the page
# ──────────────────────────────────────────────────────────────────────────


def test_one_refused_row_does_not_cost_the_whole_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `delivery_id` collision on row 2 of 3. The other two still land.

    With one `session.flush()` for the whole page and no per-row savepoint, the
    refusal raised out of `replay` and the caller's transaction rolled the WHOLE
    page back — the head-of-line shape `relay_pending` was already fixed for,
    left standing in `replay`. Two concurrent replays are the production route
    to it; a duplicate primary key is the deterministic one.
    """
    from application.services import relay
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    for i in range(3):
        _create_review(f"head-of-line-{i}")
    _drain()

    name = _consumer("hol")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    first = _replay(name, from_position=floor)
    assert first.created == 3, f"the page under test did not have three rows to lose: {first!r}"
    taken = _deliveries_for(name)[1]["delivery_id"]

    real_uuid4 = uuid.uuid4
    calls: list[int] = []

    def colliding_uuid4() -> uuid.UUID:
        calls.append(1)
        # The SECOND delivery of the next page reuses a primary key that is
        # already in the table, so its INSERT is refused by the database.
        return taken if len(calls) == 2 else real_uuid4()

    monkeypatch.setattr(relay.uuid, "uuid4", colliding_uuid4)
    second = _replay(name, from_position=floor)
    monkeypatch.undo()

    assert second.failed == 1, f"the refused row was not counted as a failure: {second!r}"
    assert second.created == 2, f"one refused row cost more than itself. The page must be per-row isolated: {second!r}"
    assert len(_deliveries_for(name)) == 3 + 2, (
        "the two rows that were never refused are not in the table, so the whole page was rolled "
        "back for one bad row — the head-of-line block, in replay"
    )


# ──────────────────────────────────────────────────────────────────────────
# 5. `limit` bounds MATCHING events, and the caller gets a cursor
# ──────────────────────────────────────────────────────────────────────────


def test_limit_bounds_matching_events_not_the_page_before_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer wanting only the LAST of five events, with `limit=3`.

    The measured failure: `replay(from_position=129, limit=3)` returned 0
    created while a matching event sat at position 133. The page was truncated
    to three rows by the database and the event-type filter then ran in Python
    over those three. A caller looping until 0 stops here, permanently, and no
    log line says anything is missing.
    """
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    _create_review_with_a_lifecycle("cursor")
    _drain()

    written = _log_rows_after(floor)
    closed = [r for r in written if r["event_type"] == "review.closed.v1"]
    assert len(closed) == 1, f"expected exactly one review.closed.v1 after {floor}: {written}"
    before = [r for r in written if r["log_position"] < closed[0]["log_position"]]
    assert len(before) >= 3, (
        "fewer than three non-matching events precede the matching one, so a limit of 3 would "
        f"reach it even with the filter applied after the LIMIT. This test would be vacuous: {written}"
    )

    name = _consumer("cursor")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.closed.v1"),))

    result = _replay(name, from_position=floor, limit=3)
    assert result.created == 1, (
        "the limit was spent on events this consumer does not receive. It must bound the rows the "
        f"consumer actually gets, which means the filter belongs in the SQL: {result!r}"
    )
    assert result.exhausted, (
        "the page returned fewer rows than the limit, so the log is finished and the caller must "
        f"be told so rather than having to infer it from a count: {result!r}"
    )
    assert result.next_position == closed[0]["log_position"], (
        f"the cursor does not name the last position examined: {result!r}"
    )

    rows = _deliveries_for(name)
    assert [r["event_id"] for r in rows] == [closed[0]["event_id"]]


def test_a_caller_paging_by_the_cursor_reaches_every_matching_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop a caller is supposed to write: page until `exhausted`.

    And the loop it used to write — until `created == 0` — recorded here as
    what it would have reached, because "the fix works" and "the old stopping
    rule was wrong" are two claims and this file owes both.
    """
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    _create_review_with_a_lifecycle("paging-a")
    _create_review_with_a_lifecycle("paging-b")
    _drain()

    name = _consumer("paging")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.closed.v1"),))

    total = 0
    position = floor
    pages = 0
    stopped_at_zero: int | None = None
    for _ in range(_MAX_PASSES):
        page = _replay(name, from_position=position, limit=1)
        pages += 1
        if page.created == 0 and stopped_at_zero is None:
            stopped_at_zero = total
        total += page.created
        position = page.next_position
        if page.exhausted:
            break
    else:
        raise AssertionError("paging by the cursor did not terminate")

    assert total == 2, f"paging reached {total} of the 2 matching events in {pages} pages"
    assert stopped_at_zero is None or stopped_at_zero == 2, (
        "a page returned 0 created before the log was exhausted, so a caller using 'until it "
        "returns 0' as its stopping rule would have stopped early and left events unreplayed"
    )


# ──────────────────────────────────────────────────────────────────────────
# 7. The ancestor lookup is outside the per-row savepoint
# ──────────────────────────────────────────────────────────────────────────


def test_a_failed_ancestor_lookup_does_not_take_the_whole_replay_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_existing_delivery_id` swallows, and swallowing hid an ABORTED transaction.

    The read is a courtesy — `None` means "no ancestor", which is the normal
    answer in the backfill case this whole file is about — so returning `None`
    when it fails is right for the caller. It was wrong about the SESSION: a
    statement Postgres refuses leaves the transaction in `25P02`, and `replay`
    calls this OUTSIDE its per-row savepoint and then opens
    `session.begin_nested()` for the insert. On an aborted transaction the
    SAVEPOINT itself is refused, and so is everything after it. MEASURED with
    the savepoint taken back out:

        ReplayResult(created=0, next_position=393, exhausted=True, failed=3)

    — the whole page lost to a read that was never load-bearing, and reported as
    three separate `relay.replay.delivery_failed` lines each blaming a different
    innocent event for `"current transaction is aborted"`.

    Injected as a REAL server-side error rather than a raised Python exception,
    because a `raise` from a mock leaves the transaction perfectly usable and
    would reproduce nothing: the defect is the abort, not the exception. The
    substitution is aimed at the exact statement `_existing_delivery_id` issues,
    and the test asserts it was actually hit — an injection that misses would
    otherwise leave a green test measuring an ordinary replay.

    Sibling of `test_one_refused_row_does_not_cost_the_whole_page`: same
    head-of-line property, one layer further out.
    """
    from application.services import relay
    from database import actor_scope
    from domain.policies import consumer_registry

    _drain()
    floor = _floor()
    for i in range(3):
        _create_review(f"ancestor-lookup-{i}")
    _drain()

    name = _consumer("anclookup")
    monkeypatch.setattr(consumer_registry, "REGISTERED", (_registration(name, "review.created.v1"),))

    intercepted: list[str] = []
    with actor_scope(_actor()) as session:
        real_execute = session.execute

        def failing_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(str(statement).split()).upper()
            if rendered.startswith("SELECT EVENT_DELIVERIES.DELIVERY_ID"):
                intercepted.append(rendered)
                return real_execute(text("SELECT 1 / 0"))
            return real_execute(statement, *args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]
        try:
            result = relay.replay(session, consumer=name, from_position=floor)
        finally:
            session.execute = real_execute  # type: ignore[method-assign]

    assert intercepted, (
        "the ancestor lookup this test breaks was never issued, so the injection missed its "
        "statement and this test is measuring an ordinary replay"
    )
    assert result.created == 3, (
        f"a failed ancestor lookup cost the page {3 - result.created} of its 3 rows: {result!r}. "
        "The lookup is not load-bearing — `None` is its normal answer — so nothing may be lost to it"
    )
    assert result.failed == 0, f"rows were counted as failures for a read that means 'no ancestor': {result!r}"
    assert len(_deliveries_for(name)) == 3, (
        "the deliveries are not in the table, so the replay's transaction did not survive the aborted lookup"
    )
