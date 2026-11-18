"""FastAPI application factory for Evidence Review 8810."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from actor import NotAHumanPrincipalError
from api.problem_details import (
    TYPE_BASE,
    HumanPrincipalRequiredError,
    ProblemError,
    ServiceUnavailableError,
    StateConflictError,
    problem_exception_handler,
)
from api.routers import (
    evidence,
    exports,
    flow,
    health,
    imports,
    review_jobs,
    reviews,
    screening,
    snapshots,
)
from api.routers import (
    metrics as metrics_router,
)
from config import Settings, get_settings
from domain.policies.flow_derivation import ConservationError
from log_config import get_logger
from metrics import build_registry

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()

    # CAP THE THREADPOOL TO THE CONNECTION POOL.
    #
    # Every route in this service except the health probes is a plain `def`, or
    # is dispatched through `run_in_threadpool` (the import route, since it moved
    # off the event loop), so each one takes a thread AND then asks for a
    # connection. anyio's default `CapacityLimiter` is 40; `pool_size +
    # max_overflow` defaults to 15, and the deployment runs ONE replica. So 40
    # handlers could contend for 15 connections, and the 25 that lost spent
    # `pool_timeout` — 30 seconds, the SQLAlchemy default — waiting before
    # failing.
    #
    # Raising the handler mapping below without this would only rename the
    # symptom. Backpressure belongs at the point where work is admitted: with the
    # limiter matched to the pool, an over-subscribed instance QUEUES on the
    # threadpool instead of timing out on checkout, which is a wait rather than
    # an error.
    limit = settings.db_pool_size + settings.db_max_overflow
    try:
        to_thread.current_default_thread_limiter().total_tokens = limit
    except (RuntimeError, ValueError) as exc:  # pragma: no cover - anyio backend detail
        # Never fatal. A service that refuses to boot because it could not tune a
        # limiter is worse than one running on anyio's default.
        logger.warning("could not cap the threadpool", extra={"limit": limit, "error": str(exc)})
    else:
        logger.info("threadpool capped to the connection pool", extra={"limit": limit})

    logger.info(
        "evidence-review starting",
        extra={"version": settings.version, "port": settings.port, "environment": settings.environment},
    )
    yield
    logger.info("evidence-review stopping")


async def _pool_exhausted_handler(request: Request, exc: Exception) -> JSONResponse:
    """`sqlalchemy.exc.TimeoutError` — no connection was free within `pool_timeout`.

    THE ONE FAILURE IN THIS SERVICE THAT HAD NO PROBLEM+JSON.

    `TimeoutError` is a sibling of `StatementError`, not a descendant of
    `DBAPIError`, so it matched neither registered handler and propagated to
    Starlette as a bare `500` — no `code` slug, no `type`, no body a client can
    branch on. Every other failure here carries a stable slug by design.

    A `500` also tells the caller they did something wrong. They did not: the
    request was well-formed, the database is healthy, and the correct response is
    to retry — which is exactly what `ServiceUnavailableError`'s docstring says
    it exists for. `Retry-After` is deliberately short; pool contention clears in
    the time it takes the requests ahead to finish, not in minutes.
    """
    logger.warning("connection pool exhausted", extra={"path": str(request.url.path), "error": str(exc)})
    response = ServiceUnavailableError(
        "no database connection was available within the pool timeout; the request did nothing and is safe to retry"
    ).to_response(instance=str(request.url.path))
    response.headers["Retry-After"] = "2"
    return response


async def _not_a_human_handler(request: Request, exc: Exception) -> JSONResponse:
    """A service principal attempted a human judgement (`12-…:121-124`)."""
    return HumanPrincipalRequiredError(str(exc)).to_response(instance=str(request.url.path))


async def _conservation_handler(request: Request, exc: Exception) -> JSONResponse:
    """A refusal the operator can act on, instead of a bare 500.

    `ConservationError` subclasses `AssertionError`, so nothing was mapping it
    and it reached the client as `500 Internal Server Error` with no body. That
    is a refusal that cannot be acted on: the message names the records or the
    stages involved and the remedy, and none of it left the process.

    Measured on `a578584`, against a review holding two stages at the same
    `stage_order` — the state the derivation-time backstop exists for:

        GET  /flow          200 -> 500      POST /exports      -> 500
        GET  /conflicts     200 -> 500      POST /flow:rebuild -> 500
                                            POST :close        -> 500

    Two of those became reachable today: `list_conflicts` borrows
    `_unresolved_conflicts`, and `transition(closed)` gates on it, and both go
    through `_stage_positions`. So the review could be neither inspected, closed
    nor exported, and the one thing that would have told an operator why was the
    thing being swallowed.

    **Still 5xx, deliberately.** A conservation violation means the stored data
    is in a state the ledger cannot represent — the request was not wrong, the
    database is, and answering 4xx would tell the caller to fix their input and
    stop paging anyone. `500` keeps the alert and the body carries the remedy,
    which is the pair that was missing rather than either one alone.
    """
    logger.error("conservation violation", extra={"path": request.url.path, "detail": str(exc)})
    return JSONResponse(
        status_code=500,
        content={
            "type": f"{TYPE_BASE}/conservation_violation",
            "title": "Conservation Violation",
            "status": 500,
            "detail": str(exc),
            "code": "conservation_violation",
            "instance": request.url.path,
        },
        media_type="application/problem+json",
    )


async def _integrity_handler(request: Request, exc: Exception) -> JSONResponse:
    """Database-enforced invariants surface as typed conflicts, not 500s.

    Every guarantee in this service — append-only, the corpus seal, legal
    lifecycle transitions, the one-conflict-per-disagreement key — is a
    constraint or trigger. When one fires, the request was wrong, not the
    service, and a 500 would tell the caller the opposite.

    But **only** when the request was wrong. `DBAPIError` also covers the
    database being unreachable, and this handler used to answer those with the
    same `409 state_conflict` — so "the database is down" reached the caller as
    "your request conflicts with the current state". An operator would go
    looking for a lifecycle bug while the real fault was a dead connection.
    Operational failures are 503 with a code that says so, and they carry no
    upstream text: a connection error names hosts, ports and roles.

    Constraint text IS passed through, deliberately: those are our own
    `RAISE EXCEPTION` strings, written to be read by whoever tripped them, and
    they carry no user data.
    """
    import psycopg

    original = getattr(exc, "orig", exc)
    detail = str(original).strip().splitlines()[0]

    operational = isinstance(
        original,
        (psycopg.OperationalError, psycopg.InterfaceError, psycopg.errors.AdminShutdown),
    )
    if operational:
        logger.error("database is unavailable", extra={"error": type(original).__name__, "path": request.url.path})
        return ServiceUnavailableError("the review database is not reachable; no state was changed").to_response(
            instance=str(request.url.path)
        )

    logger.info("database invariant refused a write", extra={"detail": detail, "path": request.url.path})
    return StateConflictError(detail).to_response(instance=str(request.url.path))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Evidence Review 8810",
        version=settings.version,
        description=(
            "Protocol-locked reviews, reversible deduplication, blinded dual screening, "
            "conflict adjudication, a derived flow ledger and a hash-sealed inclusion-set export."
        ),
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        lifespan=lifespan,
    )

    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.add_exception_handler(ConservationError, _conservation_handler)
    app.add_exception_handler(NotAHumanPrincipalError, _not_a_human_handler)
    app.add_exception_handler(IntegrityError, _integrity_handler)
    app.add_exception_handler(DBAPIError, _integrity_handler)
    # `research_contracts.canonical.errors.SchemaValidationError` is DELIBERATELY
    # NOT handled here, and that is a decision rather than an omission.
    #
    # It reached production once as a 500: `ER-007` let a caller's
    # `intended_use` through unvalidated, and `compute_content_hash` — which
    # validates before it hashes — raised it. The obvious repair is a handler
    # mapping it to 422. That repair is wrong.
    #
    # By the time a manifest reaches the canonicaliser, this service BUILT it.
    # A validation failure there means either that we failed to validate the
    # caller's input, or that our construction is broken — and nothing at that
    # point can tell those apart. Answering 422 would tell a caller their
    # request is malformed when the truth may be that our code is, which trains
    # them to mangle perfectly good input chasing a server bug. A 500 is the
    # honest answer to "we produced something invalid".
    #
    # The correct fix is the one `snapshots.request_snapshot` applies: validate
    # the caller's fields at the BOUNDARY, so the schema can only ever fail on
    # our own construction. If you are reading this because of a 500, find the
    # unvalidated input field; do not add the handler.

    # NOT covered by the DBAPIError handler above: `sqlalchemy.exc.TimeoutError`
    # descends from `SQLAlchemyError` directly, beside `StatementError`.
    app.add_exception_handler(SQLAlchemyTimeoutError, _pool_exhausted_handler)

    # ONE registry per app, on `app.state`.
    #
    # ⛔ `counters=` is deliberately NOT passed here, and that omission is the
    # load-bearing half of this line. `worker._Counters` lives in the WORKER's
    # process; the six relay counters registered from here would read `0`
    # forever — a flat line that reads as "the relay is healthy and idle" when
    # the truth is "this process has never relayed anything and never will".
    # Absent series cannot be misread; a zero can. See `metrics.build_registry`.
    app.state.metrics_registry = build_registry(settings, component="api")

    # Routes are UNVERSIONED: /api/<resource>, never /api/v1/<resource>.
    app.include_router(health.router, tags=["Health"])
    # Beside the health probes because it belongs to the same exemption: neither
    # takes an `Actor`, and in this service that IS the license-authz exemption.
    app.include_router(metrics_router.router, tags=["Health"])
    app.include_router(reviews.router)
    app.include_router(imports.router)
    app.include_router(screening.router)
    app.include_router(flow.router)
    app.include_router(exports.router)
    app.include_router(evidence.router)
    app.include_router(review_jobs.router)
    app.include_router(snapshots.router)

    return app
