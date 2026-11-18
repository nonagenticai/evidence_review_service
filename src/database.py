"""SQLAlchemy engine and session scope for Evidence Review 8810."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from actor import Actor, bind_actor
from config import Settings, get_settings
from log_config import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """Declarative base for every ORM model in this service."""


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    settings = settings or get_settings()
    if _engine is None:
        connect_args: dict = {}
        if settings.database_url.startswith("postgresql"):
            # PgBouncer in transaction-pooling mode cannot carry server-side
            # prepared statements across a pooled connection.
            connect_args["prepare_threshold"] = 0
            # ⛔ Without this libpq waits FOREVER for a connect, holding both a
            # pool slot and the calling thread. See
            # `Settings.db_connect_timeout_seconds` for why the value is 2 and
            # for the one thing it cannot bound (name resolution, which psycopg
            # does in Python before libpq is involved).
            connect_args["connect_timeout"] = settings.db_connect_timeout_seconds
        _engine = create_engine(
            settings.database_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            connect_args=connect_args,
        )
        logger.info("created SQLAlchemy engine", extra={"pool_size": settings.db_pool_size})
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(settings), expire_on_commit=False, future=True)
    return _session_factory


def _bound_statements(session: Session, settings: Settings, statement_timeout_ms: int | None) -> None:
    """`SET LOCAL statement_timeout`, BEFORE the scope's first real statement.

    ⛔ Ordering is the property, not the presence. A bound applied after the
    caller's first `execute` — or after `bind_actor` — leaves that statement
    unbounded, and it is the first statement of a transaction that pays for a
    stale pooled connection being re-established.

    `SET LOCAL` rather than `SET`: the engine is shared across the whole
    process, and a session-scoped timeout would leak this scope's ceiling onto
    unrelated work through a pooled connection. See
    :attr:`Settings.db_statement_timeout_ms` for why this is not on the engine.

    ⚠️ Guarded on the dialect. `statement_timeout` is a Postgres GUC; issuing it
    against anything else would turn "your bound is not enforced here" into
    "nothing works here", which is a worse answer to the same question.

    ## ⚠️ It changes the /metrics scrape path, which `metrics.py` does not say

    `src/metrics.py` is 0 diff bytes in this change — **the file is untouched,
    the path it runs is not.** `metrics.register_collectors` builds its
    collector as `RelayDepthCollector(lambda: session_scope(settings))`, so this
    function now issues the FIRST statement of a scrape, and
    `_DepthReading.run`'s own ``SET LOCAL statement_timeout = 750`` lands
    SECOND. A scrape is five statements now, not the four
    `metrics.DEPTH_STATEMENT_TIMEOUT_MS` derives its value against.

    Nothing is weakened, and the reasons are specific rather than reassuring:

    * The two `SET LOCAL`s are in the same transaction, so the later one WINS.
      The scrape's four real statements still run at 750 ms, exactly as audited.
    * The extra statement is a GUC assignment — no planner, no rows, no locks —
      and it is covered by the 5.0 s wall-clock deadline that is the actual
      bound on a scrape.
    * The one thing it is NOT covered by is a 750 ms server-side ceiling: this
      first statement runs under :attr:`Settings.db_statement_timeout_ms`
      (30 000 ms), because it is the statement that installs the tighter value.
      That is unavoidable in any ordering and is why the deadline exists.

    ⛔ So the honest claim is "`metrics.py`: 0 diff bytes", not "the scrape is
    unchanged". `metrics.py`'s own arithmetic — "4 statements x 750 ms + 2 s
    connect = 5.0 s" — is now one statement short of what a scrape issues. It is
    left alone on purpose: that bound is deployed and audited, and re-deriving a
    proven number to keep a comment tidy is the risk this change already refused
    to take once.
    """
    if not settings.database_url.startswith("postgresql"):
        return
    milliseconds = settings.db_statement_timeout_ms if statement_timeout_ms is None else statement_timeout_ms
    # nosec B608 - an int from configuration, coerced here. `statement_timeout`
    # is a GUC and takes no bind parameter, so this cannot be parameterised.
    session.execute(text(f"SET LOCAL statement_timeout = {int(milliseconds)}"))


@contextmanager
def session_scope(settings: Settings | None = None, *, statement_timeout_ms: int | None = None) -> Generator[Session]:
    """A transactional scope with NO actor bound, and a BOUNDED one.

    Use only for work that is genuinely tenant-agnostic — health probes,
    migrations-adjacent introspection. Anything touching review data wants
    :func:`actor_scope`, because an unbound ``app.current_tenant`` makes every
    row-level-security policy match nothing, and "no rows" is indistinguishable
    from "no data" at the call site.

    ``statement_timeout_ms`` overrides :attr:`Settings.db_statement_timeout_ms`
    for this scope only. ⚠️ It bounds each STATEMENT, server-side; it cannot
    bound the connect that precedes them and it cannot bound the name
    resolution that precedes THAT. A caller that must not wait needs
    :func:`bounded_db.run_bounded` around this as well — see that module for
    the 30.016 s `getaddrinfo` measurement that is the reason.
    """
    settings = settings or get_settings()
    session = get_session_factory(settings)()
    try:
        _bound_statements(session, settings, statement_timeout_ms)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def actor_scope(
    actor: Actor, settings: Settings | None = None, *, statement_timeout_ms: int | None = None
) -> Generator[Session]:
    """A transactional scope with ``actor`` bound to it, and BOUNDED.

    The binding happens INSIDE the transaction, before any statement the caller
    issues, because ``set_config(..., is_local => true)`` is scoped to the
    transaction and is discarded at commit — which is exactly the property that
    makes it safe behind PgBouncer.

    The statement bound goes on before even that. `bind_actor` is itself a
    round trip, and this is the scope every route in `api/routers/` opens — it
    is the single place that gives all **52** of them a server-side ceiling they
    had never had. (Measured: ``grep -c 'with actor_scope(' src/api/routers/*``
    — 52, across screening 13, reviews 10, imports 10, evidence 7, exports 4,
    snapshots 3, flow 3, review_jobs 2.)

    ## ⛔ KNOWN RESIDUAL — these 52 sites get a ceiling, NOT a deadline

    ⚠️ **Read this before trusting the headline "every database wait is
    bounded". These 52 are the sites it is least true of.**

    What they get here is `statement_timeout` (server-side) and
    `connect_timeout` (libpq). What they do NOT get is the third bound —
    :func:`bounded_db.run_bounded`'s wall clock — and that is the only one of
    the three that covers `getaddrinfo`, measured in this environment at
    **30.016 s**. So a stalled resolver blocks a route for ~30 s with all three
    "bounds" nominally in force, exactly as it did before this change.

    ### Why that is worse than 52 slow routes

    Every one of the 52 lives in a plain `def` handler — measured, zero of them
    are `async def` — so FastAPI dispatches each to the anyio threadpool, which
    `app.py` deliberately caps to `db_pool_size + db_max_overflow` (**15** at
    the production `10 + 5`). Fifteen concurrently stalled requests therefore
    hold every token in the process.

    ⛔ **And `health.ready()` is a plain `def` on that same limiter.** With the
    tokens gone it is not slow, it is **unscheduled** — it never enters
    `run_bounded` at all, so `READY_PROBE_BUDGET_SECONDS = 3.0` is never
    reached and kubelet's `timeoutSeconds: 5` fires first. The probe's careful
    3-under-5 derivation is defeated from OUTSIDE the probe, and what an
    operator sees is a kubelet timeout with no body rather than the
    `database: error: DatabaseBudgetExceededError` the handler was built to
    write. The pod still goes NotReady on schedule (`failureThreshold: 3` x
    `periodSeconds: 10`), which is the correct verdict reported for the wrong
    reason — and `/health/live` is `async`, so nothing restarts it.

    This is closer to the title's claim than the JWKS gap documented in
    `auth.verifier._JWKSCache._build`, and it deserves to be recorded to the
    same standard rather than left to be discovered.

    ### Why it is not fixed in this change

    Not effort — shape. `run_bounded`'s contract is that `work` opens its OWN
    scope on the abandonable thread, because establishing the connection is the
    part that hangs. Bounding these 52 therefore means restructuring 52 route
    BODIES, not wrapping 52 calls: each handler's post-query work (serialisation,
    problem-details mapping, the ORM objects it returns) has to be split from
    the part that may be abandoned, and `expire_on_commit=False` makes what is
    safe to return across that boundary a per-route question.

    ⚠️ It also needs pool arithmetic this change has not done. An abandoned
    request keeps its connection until `connect_timeout` or `statement_timeout`
    ends it, so 15 abandoned requests are 15 held slots out of 15 — a deadline
    without a per-route admission policy converts a stall into a faster stall.
    `health.ready()` needed `_PROBE_FLIGHT` for exactly this reason at ONE call
    site; the equivalent across 52 is a design decision, not a wrapper.

    ⛔ So the guarded statement is what this change actually delivers: **every
    database wait that can restart or wedge a pod is bounded** — the readiness
    probe, the relay tick, preflight — **and every remaining wait has a
    server-side ceiling it did not have before.** Not "every database wait is
    bounded".

    ### What would change the decision

    Evidence that stalled `actor_scope` requests actually exhaust the limiter in
    production — anyio token saturation, or `/health/ready` failing with an
    empty body rather than a `DatabaseBudgetExceededError` one. The honest state
    of that today is "not measured", not "fine". If it is taken on, the shape is
    a deadline per route ABOVE the 30 s `db_statement_timeout_ms` backstop, plus
    an admission limit sized against the pool — reported as 503 with
    `Retry-After`, never as a 500.
    """
    settings = settings or get_settings()
    session = get_session_factory(settings)()
    try:
        _bound_statements(session, settings, statement_timeout_ms)
        bind_actor(session, actor)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
