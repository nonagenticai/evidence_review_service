"""The research event relay: outbox → durable log → per-consumer deliveries.

`FND-007`, steps 2, 3 and 6 of `13-…:§8.1`. `ER-M022` created the tables; this
module is the movement between them.

## ⛔ Nothing is delivered, because nothing is registered

`consumer_registry.REGISTERED` is empty and `13-…:§8.3` is why: no event has
its schema, example, consumer matrix and compatibility test merged, so emitting
one is forbidden. `relay_pending` therefore copies outbox rows into the durable
log — which is still inside this service — and creates deliveries for exactly
the consumers the registry names, which is none.

The log fills; nothing leaves. That is the whole design, and it is the same
argument `ER-M019` made one step earlier.

## How "idempotently" is achieved (step 2)

`ON CONFLICT (event_id) DO NOTHING`. The relay is at-least-once by
construction — it can crash after inserting and before recording that it did —
so the log's primary key is what makes a repeat harmless. The alternative,
marking outbox rows as relayed, is exactly what `§8.1` forbids: "A single
global `consumed_at` flag is not sufficient."

So progress is derived, not stored: an outbox row is un-relayed iff its
`event_id` is absent from the log. That is a `NOT EXISTS`, it is correct after
any crash, and it needs no column on the outbox.

## Ordering (step 3, and `§8.2`)

Deliveries are created in `(resource_scope, tenant_id, aggregate_type,
aggregate_id, aggregate_version)` order — the tuple `§8.2` guarantees ordering
per, and the exact index `0019` and `ER-M022` both carry. A consumer still has
to defer a future version, because a relay reading in order does not make a
CONSUMER read in order; `§8.2` puts that obligation on the consumer and this
module does not pretend to discharge it.

## Replay (step 6)

`replay` creates NEW delivery rows for events already in the log and never
touches `event_id`. The partial unique index only covers originals
(`replay_of_delivery_id IS NULL`), so a replay is additive rather than refused —
and a consumer that already recorded an inbox receipt for that event will skip
it, which is what makes replay safe rather than duplicating effects.

⚠️ **A replay row NEVER has a NULL `replay_of_delivery_id`**, including in the
case `consumer_registry` documents as the reason a consumer may safely stay
unregistered: a consumer that registers late and backfills from position 0.
There is no original there — nothing ever fanned out to a consumer that did not
exist — and a row with a NULL `replay_of` in that state IS an original, in the
index, in every query that separates the two, and to the next replay, which
then names it as the original it is not. So a replay with no original names
ITSELF, which is not a trick: it makes the row a non-original in the partial
index, gives `replay_of` a value that resolves to a real delivery rather than a
dangling id, and terminates a walk up the chain at a root that says "this is
where this consumer's history starts". See `replay`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, PendingRollbackError, ResourceClosedError, SQLAlchemyError
from sqlalchemy.orm import Session

from api.problem_details import ValidationFailedError
from domain.policies import consumer_registry
from log_config import get_logger
from models.foundation import EventDelivery, ResearchDomainEvent

logger = get_logger(__name__)

#: How many outbox rows one relay pass moves. Bounded so a backlog is drained
#: in steady increments rather than in one transaction that holds locks for
#: however long the backlog happens to be.
DEFAULT_BATCH = 200

#: The one `poison_reason` (of `0022`'s four) reachable from the relay's own
#: fan-out. `incompatible_schema`, `impossible_transition` and
#: `unknown_consumer` describe a CONSUMER's own handler rejecting an event —
#: nothing downstream of the relay runs yet (see the module docstring), so
#: those three have no code path here. `handler_failed_repeatedly` is the
#: least-wrong bucket for "the relay's own attempt to CREATE this consumer's
#: delivery row did not succeed" — from the relay's side, that insert is the
#: whole of the handling this step performs.
_FAN_OUT_POISON_REASON = "handler_failed_repeatedly"

#: Mirrors `MINIMAL_FACT_MAX` in `0019`/`0022`. A poisoned row is exactly the
#: one most likely to be copied into an alert, so the detail is bounded rather
#: than carrying a full driver traceback.
_POISON_DETAIL_MAX = 512

#: Where a poisoned row is attributed when the consumer's own name is the thing
#: the database refused. `ck_event_deliveries_consumer_shape` applies to EVERY
#: row in the table, poisoned ones included, so a poison row carrying the
#: offending name verbatim is refused by the identical CHECK that refused the
#: delivery — which is how "the poison path is unreachable" was true for a
#: malformed name. The real name goes into `poison_detail`, where nothing
#: constrains it.
#:
#: NOT derived from the bad name (`flow-projector` → `flow_projector`): that
#: derivation can land on a DIFFERENT, real consumer and attribute a failure to
#: a team that had nothing to do with it. A constant nobody can register by
#: accident is the honest place.
_UNSHAPED_CONSUMER = "invalid_consumer"


def _now() -> datetime:
    return datetime.now(UTC)


class RelayResult(tuple):
    """`(relayed, deliveries)` — a `tuple` subclass, so it unpacks exactly like
    the plain tuple `relay_pending` used to return. `src/worker.py` does
    `relayed, deliveries = relay.relay_pending(session)` and keeps working
    unmodified; nothing about the return CONTRACT changed.

    `poisoned` rides along as an attribute rather than a third tuple element on
    purpose: a 3-tuple would break that same `worker.py` line with a
    `ValueError: too many values to unpack` the moment this landed. A caller
    that wants to know whether anything was poisoned reads `.poisoned` (and
    `.poisoned_events`, the `(event_id, consumer)` pairs) explicitly; a caller
    that does not is unaffected.
    """

    # No `__slots__` here on purpose: CPython refuses a NON-EMPTY `__slots__`
    # on a subclass of a variable-length builtin like `tuple` ("nonempty
    # __slots__ not supported for subtype of 'tuple'") — there is no room
    # behind the tuple's own variable-length storage for extra per-instance
    # slots. Omitting `__slots__` gives the instance a `__dict__` instead,
    # which is exactly one small object per relay pass and not a cost worth
    # avoiding.

    # DECLARED at class level, not merely assigned in `__new__`. Without these the
    # type checker cannot see attributes created on an instance of a builtin
    # subclass, and `ty` rejected `self.poisoned` in `__repr__` with
    # `unresolved-attribute` on `Self@__repr__`. Annotations without values create
    # no slots — they are exactly the declaration the checker needs and nothing
    # more, which matters because a non-empty `__slots__` is impossible here (see
    # the note above).
    poisoned: int
    poisoned_events: tuple[tuple[uuid.UUID, str], ...]

    def __new__(
        cls,
        relayed: int,
        deliveries: int,
        poisoned: int = 0,
        poisoned_events: tuple[tuple[uuid.UUID, str], ...] = (),
    ) -> RelayResult:
        self = super().__new__(cls, (relayed, deliveries))
        self.poisoned = poisoned
        self.poisoned_events = poisoned_events
        return self

    def __repr__(self) -> str:
        relayed, deliveries = self
        return f"RelayResult(relayed={relayed}, deliveries={deliveries}, poisoned={self.poisoned})"


def relay_pending(
    session: Session,
    *,
    limit: int = DEFAULT_BATCH,
    now: datetime | None = None,
) -> RelayResult:
    """Move outbox rows into the log and fan out deliveries.

    Returns a `RelayResult` — unpack it as `(events relayed, deliveries
    created)`, exactly as before; see `RelayResult` for what else it carries.

    Never commits. The caller owns the transaction, so a relay pass and
    whatever bookkeeping the caller wants around it are one unit.

    ## Per-row isolation, and why it exists

    Before this fix, `_fan_out` accumulated every delivery for the WHOLE batch
    with `session.add()` and this function flushed ONCE at the end, with no
    `try`/`except` anywhere in between. One failing insert — demonstrated live
    with a duplicate consumer registration colliding on
    `uq_event_deliveries_consumer_event` — raised out of that single flush.
    Because this function never commits, the caller's transaction absorbed the
    exception and rolled back WHOLE: every clean delivery this pass had
    produced, and the log rows the `INSERT ... SELECT` below had already
    written for the entire batch. The failing outbox row sorts first under the
    relay's deterministic `ORDER BY`, so the next pass re-selected the exact
    same row first and failed the exact same way — a PERMANENT head-of-line
    block, not a transient one: every tenant behind that one row stopped
    making progress, forever, with nobody paged.

    `_fan_out` now isolates each (event, consumer) insert behind its own
    `SAVEPOINT`, so a bad entry can only ever cost that one delivery — see its
    docstring for the full argument, including where the failure is recorded
    instead of lost.
    """
    at = now or _now()
    if limit < 1:
        raise ValidationFailedError("relay batch limit must be at least 1")

    # ⚠️ ONE `INSERT … SELECT`, so the payload never round-trips through Python.
    #
    # The first version read the outbox into Python and re-inserted row by row.
    # It failed immediately: psycopg decodes `JSONB` to a `dict` on the way out
    # and cannot adapt a `dict` back to a placeholder on the way in
    # ("cannot adapt type 'dict'"). Serialising it back to JSON text would have
    # worked and would have been the wrong repair — a relay that re-encodes a
    # payload is a relay that can change one, and `content_hash` is computed
    # over the original.
    #
    # Copying inside the database also makes step 2 literally one idempotent
    # statement, which is what `§8.1` asks for: `ON CONFLICT (event_id) DO
    # NOTHING` is the whole of the at-least-once tolerance, and there is no
    # window between choosing a row and inserting it.
    relayed_rows = (
        session.execute(
            text(
                """
            INSERT INTO research_domain_events
                (event_id, event_type, schema_ref, producer, resource_scope, tenant_id,
                 aggregate_type, aggregate_id, aggregate_version, occurred_at, payload,
                 correlation_id, causation_id, content_hash, relayed_at)
            SELECT o.event_id, o.event_type, o.schema_ref, o.producer, o.resource_scope,
                   o.tenant_id, o.aggregate_type, o.aggregate_id, o.aggregate_version,
                   o.occurred_at, o.payload, o.correlation_id, o.causation_id,
                   o.content_hash, :relayed_at
              FROM (
                    SELECT * FROM domain_outbox o2
                     WHERE NOT EXISTS (
                             SELECT 1 FROM research_domain_events e
                              WHERE e.event_id = o2.event_id
                           )
                     ORDER BY o2.resource_scope, o2.tenant_id, o2.aggregate_type,
                              o2.aggregate_id, o2.aggregate_version, o2.occurred_at
                     LIMIT :limit
                   ) AS o
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id, event_type, tenant_id
            """
            ),
            {"relayed_at": at, "limit": limit},
        )
        .mappings()
        .all()
    )

    relayed = len(relayed_rows)
    deliveries = 0
    poisoned = 0
    poisoned_events: list[tuple[uuid.UUID, str]] = []
    for row in relayed_rows:
        created, row_poisoned = _fan_out(session, row, at)
        deliveries += created
        poisoned += len(row_poisoned)
        poisoned_events.extend(row_poisoned)

    session.flush()
    if relayed:
        logger.info("relay.pass", relayed=relayed, deliveries=deliveries, poisoned=poisoned)
    return RelayResult(relayed, deliveries, poisoned, tuple(poisoned_events))


def _fan_out(session: Session, row: Any, at: datetime) -> tuple[int, list[tuple[uuid.UUID, str]]]:
    """One delivery row per registered consumer for this event type.

    Takes only what `RETURNING` hands back — `event_id`, `event_type`,
    `tenant_id`. A delivery needs nothing else, and reading the whole event
    here would put the payload back in Python for no reason.

    Returns `(deliveries created, [(event_id, consumer) poisoned])`.

    Each consumer's insert runs inside its own `SAVEPOINT`
    (`session.begin_nested()`). If it fails, only that savepoint rolls back —
    every earlier consumer's delivery in this same call, every other event in
    the batch, and the log row this event already has in
    `research_domain_events` (written by the `INSERT ... SELECT` in
    `relay_pending`, before this loop ever runs) are untouched. See
    `relay_pending`'s docstring for what this repairs and why the isolation is
    per (event, consumer) rather than per batch.

    On a failure this function does not just swallow it: `_record_poisoned`
    writes a `poisoned` delivery — a row `13-…:§8.2` says should "page the
    owning team" once paging is wired up (`15-…` still owes that dashboard;
    see the `0022` module docstring). That write is DIFFERENT from the one that
    just failed in the two ways the two live refusals need it to be; see
    `_record_poisoned`, which used to rewrite the identical tuple and was
    therefore refused by the identical constraint, every time, for every
    failure that has ever been demonstrated.

    ## ⛔ A connection-level failure is not N poison events

    A broken connection or an already-aborted transaction fails for EVERY
    consumer and every remaining event, identically, and it is not the
    consumer's fault. Emitting a `delivery_failed` line and a poison attempt
    per consumer under those conditions produced a log that named N innocent
    consumers for one connection fault — and then `relay_pending`'s own flush
    raised anyway, so the transaction outcome was right and only the record of
    it was a lie. Such an error is re-raised at the FIRST consumer that meets
    it, with one line saying what it was.
    """
    created = 0
    poisoned: list[tuple[uuid.UUID, str]] = []
    for consumer in consumer_registry.consumers_for(row["event_type"]):
        try:
            with session.begin_nested():
                session.add(
                    EventDelivery(
                        delivery_id=uuid.uuid4(),
                        event_id=row["event_id"],
                        tenant_id=row["tenant_id"],
                        consumer=consumer,
                        state="pending",
                        created_at=at,
                        updated_at=at,
                    )
                )
                session.flush()
        except SQLAlchemyError as exc:
            detail = _detail_of(exc)
            if _is_connection_level(exc):
                logger.error(
                    "relay.fan_out.aborted",
                    event_id=str(row["event_id"]),
                    event_type=row["event_type"],
                    consumer=consumer,
                    error=detail,
                    reason=(
                        "the failure is at the connection or transaction level, not this "
                        "consumer's: every remaining consumer would fail identically and none of "
                        "them is at fault, so the pass is abandoned here rather than reported as "
                        "one poison event per consumer"
                    ),
                )
                raise
            logger.error(
                "relay.fan_out.delivery_failed",
                event_id=str(row["event_id"]),
                event_type=row["event_type"],
                consumer=consumer,
                owner=_owner_for(consumer),
                error=detail,
            )
            unrecorded = _record_poisoned(session, row, consumer, at, detail)
            if unrecorded is None:
                poisoned.append((row["event_id"], consumer))
            else:
                # The REASON is the driver's, not a guess. The line this replaces
                # asserted the poison row "fails only when a delivery for this
                # (event, consumer) pair already exists" — an explanation nothing
                # ever tested, and false for the two failures that were actually
                # demonstrated (a refused consumer shape and a permission denial).
                logger.error(
                    "relay.fan_out.delivery_unrecorded",
                    event_id=str(row["event_id"]),
                    event_type=row["event_type"],
                    consumer=consumer,
                    owner=_owner_for(consumer),
                    error=detail,
                    poison_error=unrecorded,
                )
            continue
        created += 1
    return created, poisoned


def _detail_of(exc: BaseException) -> str:
    return str(getattr(exc, "orig", exc))[:_POISON_DETAIL_MAX]


def _is_connection_level(exc: SQLAlchemyError) -> bool:
    """Whether `exc` says the CONNECTION is gone rather than the row is bad.

    `connection_invalidated` is SQLAlchemy's own answer for a DBAPI error the
    pool decided to discard the connection over. `InterfaceError` is the DBAPI
    class for "the connection is not usable"; `PendingRollbackError` and
    `ResourceClosedError` are the session states that follow one. None of them
    can be blamed on the consumer whose insert happened to be next.
    """
    if isinstance(exc, InterfaceError | PendingRollbackError | ResourceClosedError):
        return True
    return bool(getattr(exc, "connection_invalidated", False))


def _record_poisoned(session: Session, row: Any, consumer: str, at: datetime, detail: str) -> str | None:
    """Leave a `poisoned` delivery behind a failed insert. Its own SAVEPOINT.

    Returns `None` when the row landed, else the driver's reason it did not.
    Never raises — a failure here is exactly the case `_fan_out` falls back to
    logging and skipping for, so this function reports rather than propagates.

    ## ⛔ Why this used to be dead code, and what makes it reachable

    It wrote the SAME `(event_id, tenant_id, consumer, replay_of=NULL)` tuple
    the failing insert had just written, changing only `state`,
    `poison_reason` and `poison_detail`. Not one constraint on
    `event_deliveries` mentions those three columns, so there was no refusal
    that could reject the pending row and accept the poisoned one, and three
    separately demonstrated failures each produced ZERO poisoned rows:

    * a duplicate registration — refused by `uq_event_deliveries_consumer_event`
      on `(event_id, consumer)`, which the poison row reproduced exactly;
    * a consumer named `flow-projector` — refused by
      `ck_event_deliveries_consumer_shape`, which applies to the poison row's
      identical `consumer` value;
    * `permission denied` — the poison INSERT needs the same privilege.

    So the fix is not a retry, it is two DIFFERENT rows:

    1. **A name the CHECK accepts.** A consumer whose name the database refuses
       cannot be recorded under that name at all; the poison row is attributed
       to `_UNSHAPED_CONSUMER` and the real name is carried in `poison_detail`,
       which nothing constrains.
    2. **A row the partial unique index does not cover.** When an original
       delivery for this `(event, consumer)` already exists — which is what a
       duplicate registration means — the poison row names it in
       `replay_of_delivery_id`, and `uq_event_deliveries_consumer_event` is
       `WHERE replay_of_delivery_id IS NULL`, so it no longer applies. That is
       also true rather than convenient: the poison row records a SECOND,
       failed attempt against that first delivery, and the column means exactly
       "which delivery this row derives from".

    `permission denied` remains unrecordable, and honestly so: a role that
    cannot INSERT into this table cannot write a poison row about not being
    able to. `_fan_out` logs the driver's own reason in that case rather than
    the invented one it used to print.
    """
    token = consumer if consumer_registry.is_valid_consumer_name(consumer) else _UNSHAPED_CONSUMER
    if token != consumer:
        detail = f"consumer={consumer!r} (name refused by ck_event_deliveries_consumer_shape): {detail}"
    detail = detail[:_POISON_DETAIL_MAX]

    first = _try_poison(session, row, token, at, detail, replay_of=None)
    if first is None:
        return None

    derives_from = _existing_delivery_id(session, row["event_id"], token)
    if derives_from is None:
        return first
    return _try_poison(session, row, token, at, detail, replay_of=derives_from)


def _try_poison(
    session: Session,
    row: Any,
    consumer: str,
    at: datetime,
    detail: str,
    *,
    replay_of: uuid.UUID | None,
) -> str | None:
    try:
        with session.begin_nested():
            session.add(
                EventDelivery(
                    delivery_id=uuid.uuid4(),
                    event_id=row["event_id"],
                    tenant_id=row["tenant_id"],
                    consumer=consumer,
                    state="poisoned",
                    poison_reason=_FAN_OUT_POISON_REASON,
                    poison_detail=detail,
                    replay_of_delivery_id=replay_of,
                    created_at=at,
                    updated_at=at,
                )
            )
            session.flush()
        return None
    except SQLAlchemyError as exc:
        return _detail_of(exc)


def _existing_delivery_id(session: Session, event_id: uuid.UUID, consumer: str) -> uuid.UUID | None:
    """The delivery this `(event, consumer)` already has, if it has one.

    Prefers the ORIGINAL (`replay_of_delivery_id IS NULL`, of which the partial
    unique index permits at most one) and falls back to the earliest replay, so
    a chain has one root however it was built. `LIMIT 1`, not `scalar_one` —
    replays are additive by design and several rows here is the normal case,
    not an error.

    ## ⛔ ITS OWN SAVEPOINT, BECAUSE IT SWALLOWS

    Returning `None` on `SQLAlchemyError` is the right answer for the CALLER —
    see below — and it was a lie about the SESSION. A statement Postgres refuses
    puts the transaction in `25P02` (`current transaction is aborted`), and
    every later statement on it is refused too. Swallowing the exception hides
    that from a caller who has no other way to find out.

    `replay` is where that bites, and it is the ONLY caller that reaches this
    outside a savepoint: it calls this per event and then opens
    `session.begin_nested()` for the insert. On an aborted transaction the
    SAVEPOINT itself is refused, and so is every statement after it. MEASURED
    with the savepoint below removed, one failed lookup on a three-event page:

        ReplayResult(created=0, next_position=393, exhausted=True, failed=3)

    with three `relay.replay.delivery_failed` lines, each naming a different
    innocent `event_id` and each carrying `error="current transaction is
    aborted, commands ignored until end of transaction block"`. That is the
    inverse of the shape `_fan_out` was fixed into — one fault reported as N
    per-row failures, every one of them blamed on a consumer that did nothing —
    and the page is lost either way. A lookup returning `None` is not an error
    condition here at all: it means "no ancestor", which is the normal case for
    the backfill this function exists to serve.

    So the read runs inside its own `SAVEPOINT` and the failure rolls back to
    it. The caller's transaction survives, `None` becomes true again, and a
    genuinely dead connection still propagates as one: `begin_nested` cannot be
    established on a dead connection either, the `InterfaceError` reaches
    `replay`'s insert, and `_is_connection_level` aborts the page once instead
    of writing a failure line per event.

    `_poison`'s call is inside no savepoint of its own either, but it lands
    between two `_try_poison` calls that each hold one, so the same protection
    now covers both sites for one reason rather than two.
    """
    try:
        with session.begin_nested():
            return session.execute(
                select(EventDelivery.delivery_id)
                .where(
                    EventDelivery.event_id == event_id,
                    EventDelivery.consumer == consumer,
                )
                .order_by(
                    EventDelivery.replay_of_delivery_id.is_not(None),
                    EventDelivery.created_at,
                    EventDelivery.delivery_id,
                )
                .limit(1)
            ).scalar_one_or_none()
    except SQLAlchemyError:
        # A read that fails here (no privilege, dead connection) means only that
        # the second attempt cannot be aimed; the caller reports the FIRST
        # failure, which is the one that matters.
        return None


def _owner_for(consumer: str) -> str | None:
    """The team `consumer` belongs to, for the log line a page would use.

    `13-…:§8.2` requires a poisoned delivery to "page the owning team", and
    `ConsumerRegistration.owner` is that team — see `consumer_registry`.
    """
    for registration in consumer_registry.REGISTERED:
        if registration.name == consumer:
            return registration.owner
    return None


class ReplayResult(int):
    """`created`, and the cursor a caller needs to ask for the next page.

    An `int` subclass, so `relay.replay(...) >= 1` and `+=` keep working for a
    caller written against the plain count this used to return. What is added
    is the part a count cannot carry:

    * `next_position` — the log position to pass as the NEXT `from_position`.
      It is the last position EXAMINED, not the last one replayed, which is the
      whole point: a page that matched nothing still advances.
    * `exhausted` — whether the log is finished. A caller loops until this is
      true. `created == 0` is not that signal and never was; see `replay`.
    * `failed` — rows this page could not write. Not zero means somebody has to
      look, and it is separate from `created` so a page is never reported as
      complete because it was partly refused.
    """

    created: int
    next_position: int
    exhausted: bool
    failed: int

    def __new__(cls, created: int, next_position: int, exhausted: bool, failed: int = 0) -> ReplayResult:
        self = super().__new__(cls, created)
        self.created = created
        self.next_position = next_position
        self.exhausted = exhausted
        self.failed = failed
        return self

    def __repr__(self) -> str:
        return (
            f"ReplayResult(created={self.created}, next_position={self.next_position}, "
            f"exhausted={self.exhausted}, failed={self.failed})"
        )


def replay(
    session: Session,
    *,
    consumer: str,
    from_position: int = 0,
    limit: int = DEFAULT_BATCH,
    now: datetime | None = None,
) -> ReplayResult:
    """Recreate deliveries for one consumer from a log position. Step 6.

    `§8.1`: "replay recreates deliveries **without changing `event_id`**". So a
    replay mints a NEW `delivery_id`, names in `replay_of_delivery_id` the
    delivery it derives from, and leaves the event untouched.

    Safe to run twice, but not because it is idempotent — it is not, and
    pretending otherwise would be the lie. A second replay creates a second set
    of deliveries. What makes that harmless is the CONSUMER: it finds its own
    `domain_inbox` receipt for that `event_id` and acknowledges without
    repeating the effect (`§8.2`, "Duplicate or already-applied versions are
    acknowledged without repeating the effect"). Exactly-once lives in the
    inbox's unique index, not here.

    Refuses an unregistered consumer. Creating deliveries nobody claims is how
    a queue fills with work that will never be done, and the refusal is loud
    now rather than a metric somebody notices in a month.

    ## ⛔ A replay is never written as an original

    `consumer_registry` prescribes exactly one order for adding a consumer:
    it registers, the relay delivers going forward, and `replay` backfills the
    rest. In that order there is NO original for anything already in the log —
    nothing fanned out to a consumer that did not exist — and this function
    used to write those backfilled rows with `replay_of_delivery_id IS NULL`,
    which is the definition of an original: they took the partial unique
    index's one slot per `(event, consumer)`, they counted as originals in
    every query that separates the two, and a second replay then pointed at
    them as the originals they were not. Four of four rows, in the documented
    use case. A replay with no ancestor now names ITSELF; see the module
    docstring for why that is the honest value and not a placeholder.

    ## ⛔ `limit` bounds MATCHING events, and the caller gets a cursor

    The event-type filter used to run in Python, AFTER the `LIMIT`, so a page
    of events none of which this consumer wants returned `created == 0` while
    matching events sat further down the log — measured at
    `replay(from_position=129, limit=3)` returning 0 with a match at position
    133. A caller looping "until it returns 0" stopped there and left the rest
    of the log unreplayed, silently. The filter is now in the SQL, so `limit`
    bounds rows this consumer actually receives, and the result carries
    `next_position` and `exhausted` so termination is a fact the caller is told
    rather than one it infers from a count.

    ## Per-row isolation

    One `SAVEPOINT` per delivery, for the reason `relay_pending` documents at
    length: a single flush at the end made the batch all-or-nothing, so two
    concurrent replays colliding on `uq_event_deliveries_consumer_event` cost
    the loser its WHOLE page rather than one row.
    """
    at = now or _now()
    if limit < 1:
        raise ValidationFailedError("replay limit must be at least 1")
    if consumer not in consumer_registry.registered_names():
        raise ValidationFailedError(
            f"'{consumer}' is not a registered consumer; replaying to one would create "
            "deliveries nothing will ever claim"
        )

    wanted = _events_for(consumer)
    events = list(
        session.execute(
            select(ResearchDomainEvent)
            .where(
                ResearchDomainEvent.log_position > from_position,
                # In SQL, not in Python after the LIMIT. See the docstring.
                ResearchDomainEvent.event_type.in_(sorted(wanted)),
            )
            .order_by(ResearchDomainEvent.log_position)
            .limit(limit)
        ).scalars()
    )

    created = 0
    failed = 0
    next_position = from_position
    for event in events:
        next_position = event.log_position
        delivery_id = uuid.uuid4()
        derives_from = _existing_delivery_id(session, event.event_id, consumer)
        try:
            with session.begin_nested():
                session.add(
                    EventDelivery(
                        delivery_id=delivery_id,
                        event_id=event.event_id,
                        tenant_id=event.tenant_id,
                        consumer=consumer,
                        state="pending",
                        # NEVER None: a replay that names nothing IS an original.
                        replay_of_delivery_id=derives_from or delivery_id,
                        created_at=at,
                        updated_at=at,
                    )
                )
                session.flush()
        except SQLAlchemyError as exc:
            detail = _detail_of(exc)
            if _is_connection_level(exc):
                logger.error(
                    "relay.replay.aborted",
                    consumer=consumer,
                    event_id=str(event.event_id),
                    error=detail,
                )
                raise
            failed += 1
            logger.error(
                "relay.replay.delivery_failed",
                consumer=consumer,
                owner=_owner_for(consumer),
                event_id=str(event.event_id),
                error=detail,
            )
            continue
        created += 1

    session.flush()
    exhausted = len(events) < limit
    logger.info(
        "relay.replay",
        consumer=consumer,
        from_position=from_position,
        created=created,
        failed=failed,
        next_position=next_position,
        exhausted=exhausted,
    )
    return ReplayResult(created, next_position, exhausted, failed)


def _events_for(consumer: str) -> frozenset[str]:
    for registration in consumer_registry.REGISTERED:
        if registration.name == consumer:
            return registration.events
    return frozenset()


def unrelayed_count(session: Session) -> int:
    """How many outbox rows have not reached the log. The lag metric's numerator.

    Derived, like everything else about relay progress — see the module
    docstring. `15-…` lists "outbox age, delivery lag, inbox duplicate and
    poison event" among the dashboards owed; this is the first of those four,
    and the other three are not built.
    """
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM domain_outbox o WHERE NOT EXISTS "
                "(SELECT 1 FROM research_domain_events e WHERE e.event_id = o.event_id)"
            )
        ).scalar_one()
    )
