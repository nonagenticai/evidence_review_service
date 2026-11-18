"""Liveness and readiness probes.

Exempt from license-authz per the fleet rule (`/health`, `/healthz`, `/ready`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bounded_db import SingleFlight, run_bounded
from config import get_settings
from database import session_scope
from instants import utc_z
from log_config import get_logger

logger = get_logger(__name__)
router = APIRouter()

#: ⛔ The wall-clock budget for the readiness probe's database check.
#:
#: ## Why the probe needed one at all
#:
#: `ready()` wrapped `session_scope()` in `except Exception` and reported
#: `database: error: …`. That is correct handling for a database that REFUSES
#: and no handling at all for one that never answers: a hang raises nothing, so
#: the guard could not fire. Measured, the hang is 30.016 s of
#: `socket.getaddrinfo` — psycopg3 resolves the host in Python before libpq is
#: handed anything, so `connect_timeout` never applies. See `bounded_db`.
#:
#: ## Why it outranks the other sites
#:
#: `app.py` caps the anyio threadpool to `pool_size + max_overflow` — 15 in
#: production, declared in the Deployment. `ready()` is a plain `def`, so it is
#: dispatched to that threadpool and holds one of those tokens for as long as it
#: blocks. A stalled probe therefore does not merely make the pod NotReady, it
#: consumes a token every other request handler is queuing for. A blip degrades
#: readiness AND starves serving, from one hung call.
#:
#: With the bound the anyio token comes back in 3 s instead of 30. The abandoned
#: daemon thread is not an anyio thread and holds no token — only a connection,
#: which `connect_timeout` ends.
#:
#: ## Why 3.0 s
#:
#: Read from the live manifest rather than guessed —
#: `gitops-production/clusters/strato/services/evidence-review/deployment.yaml`
#: declares this probe as::
#:
#:     readinessProbe: { path: /health/ready, timeoutSeconds: 5,
#:                       periodSeconds: 10, failureThreshold: 3 }
#:
#: * **It must be below `timeoutSeconds: 5`**, or kubelet gives up first and the
#:   diagnosis this handler writes — `database: error: DatabaseBudgetExceeded` —
#:   is never sent. A bound the client never waits to read reports nothing.
#: * The handler does a second thing after this one: the export-seal check, plus
#:   the JSON render and the round trip. 3.0 s leaves 2.0 s of the kubelet
#:   budget for them.
#: * The inner bounds sum below it: `connect_timeout` 2 s (libpq's floor) +
#:   one statement x :data:`READY_PROBE_STATEMENT_TIMEOUT_MS` = 2.5 s. That
#:   keeps the deadline a BACKSTOP for the resolution nothing else can see,
#:   rather than the only bound in play — if it always fired first, the
#:   abandoned reading would keep its connection with nothing to end it.
#: * It does not change WHEN the pod is marked NotReady: `failureThreshold: 3`
#:   at `periodSeconds: 10` is ~30 s either way. It changes what the pod is
#:   doing while that happens.
#:
#: ## ⚠️ The one way this bound is defeated from OUTSIDE
#:
#: `ready()` being a plain `def` is what keeps the hang off the event loop, and
#: it is also this budget's single point of failure: the handler has to be
#: SCHEDULED on the anyio limiter before `run_bounded` can start its clock. The
#: 52 `actor_scope` routes are plain `def`s on that same 15-token limiter and
#: have no deadline of their own, so fifteen concurrently stalled requests mean
#: this handler never runs at all — kubelet's `timeoutSeconds: 5` then fires
#: against a probe that never reached 3.0 s, and the operator gets a bare
#: timeout instead of `database: error: DatabaseBudgetExceededError`.
#: `database.actor_scope` documents that residual and why it is not fixed here.
READY_PROBE_BUDGET_SECONDS = 3.0

#: `statement_timeout`, in milliseconds, for the probe's `SELECT 1`.
#:
#: A `SELECT 1` on a reachable database is sub-millisecond; 500 ms is ~500x
#: headroom, and a `SELECT 1` that genuinely needs longer is a database in
#: trouble, which is exactly when `not_ready` is the true answer. Chosen so that
#: connect (2 s) + this fits inside :data:`READY_PROBE_BUDGET_SECONDS`.
READY_PROBE_STATEMENT_TIMEOUT_MS = 500

#: ⛔ At most ONE outstanding probe reading, ever.
#:
#: The deadline stops this handler waiting; it does not stop the abandoned
#: thread, which keeps its connection until `connect_timeout` ends it. kubelet
#: re-probes every 10 s and does not wait for the last answer, so against a
#: resolver that takes 30 s to fail, an unguarded probe would have three
#: connections outstanding before the first came back — and by `app.py`'s
#: limiter those are the same 15 slots every request handler needs. The probe
#: would then be causing the starvation it is supposed to report.
#:
#: Module-level, because the guard has to outlive the request that created it.
_PROBE_FLIGHT = SingleFlight()


def _now() -> str:
    """The health payload's instant, through the service-wide renderer.

    Was `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`, which was already
    UTC and already trailing-`Z` — correct, and still worth replacing. It was
    the last place in `src/` that spelled an instant for itself, and an
    exception that is currently harmless is the one somebody copies into a
    place where it is not. `tests/unit/test_one_instant_has_one_spelling.py`
    found it only after its detector was widened to `strftime`.

    The rendered value gains microseconds. Nothing parses this field — it is a
    liveness/readiness payload — and `utc_z` emits RFC 3339 either way.
    """
    return utc_z(datetime.now(UTC)) or ""


@router.get("/health", operation_id="health_check", summary="Liveness probe")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": settings.version,
        "timestamp": _now(),
    }


@router.get("/health/live", operation_id="liveness_check", summary="Liveness probe")
async def live() -> dict[str, Any]:
    """Deliberately checks nothing but the process.

    A liveness probe that touches the database restarts the pod when the
    database blips, which converts a recoverable dependency outage into a
    crash-loop across every replica at once.
    """
    return {"status": "alive", "timestamp": _now()}


def _database_answers() -> None:
    """The probe's whole database question, on the thread it may be abandoned on.

    ⚠️ It opens its OWN scope rather than being handed a session, because
    opening it is the part that hangs — `getaddrinfo`, then the connect. A
    helper that received a ready session would have done the dangerous half on
    the caller's thread, where nothing can walk away from it.
    """
    with session_scope(statement_timeout_ms=READY_PROBE_STATEMENT_TIMEOUT_MS) as session:
        session.execute(text("SELECT 1"))


@router.get("/health/ready", operation_id="readiness_check", summary="Readiness probe")
def ready() -> JSONResponse:
    """Ready means the database answers AND the export seal is importable.

    **A plain `def`, deliberately.** This was `async def` while doing synchronous
    `session_scope()` database I/O below, which FastAPI runs ON the event loop —
    so a slow or hanging database blocked every other request in the process,
    `/health/live` included, and turned a readiness degradation into a liveness
    failure across the replica. A plain `def` is dispatched to the threadpool,
    which is what every other database-touching route in this service already
    relies on. `live()` and `health()` stay `async`: they touch nothing.
    `tests/unit/test_health.py` pins this with `inspect.iscoroutinefunction`,
    because the property is invisible at the call site and reverting it is a
    one-keyword edit.

    The seal is checked because an export that cannot be hashed is not a
    degraded export, it is an unusable one, and a pod that reports ready without
    it would accept review work it cannot finish. The Dockerfile already fails
    the build when the wheel carries no schemas; this is the runtime counterpart,
    which also catches a wheel replaced underneath a running image.

    **The check counted schemas and claimed the seal was ok.** It never asked
    whether `EXPORT_SCHEMA_REF` resolves — and for the whole life of the service
    it did not, so the probe was green while every export sealed through the
    bare-hash fallback. Counting is still the readiness gate, because a wheel
    with no schemas is genuinely unusable; the seal MODE is now reported next to
    it, and it is deliberately NOT a gate.

    That non-gating is a live decision now rather than a hypothetical one. With
    `research-contracts>=0.2.0` the ref resolves and this reports
    `ok (N released schemas); sealed_with=content_hash`. The degraded branch is
    therefore no longer describing the present — it is the ALARM for a wheel
    rolled back or replaced underneath a running image, which is the one way the
    strong seal can silently disappear again. It still must not fail readiness:
    a pod that refuses to serve because its contract wheel regressed would
    convert a weaker seal into no service at all, and the export path already
    records the weaker mode per row and logs it. `degraded` in place of `ok` plus
    `sealed_with=` is what makes the regression legible to an operator who is not
    already suspicious.

    **And the database check is BOUNDED.** The `except Exception` below was the
    whole defence and it could not fire against a hang, which raises nothing —
    measured at 30.016 s of `getaddrinfo`, entirely outside `connect_timeout`.
    The plain `def` above kept that hang off the event loop; it did NOT keep it
    off the threadpool, and `app.py` caps the threadpool to the connection pool,
    so a stalled probe was holding a token every request handler needed. See
    :data:`READY_PROBE_BUDGET_SECONDS` for the number and where it comes from,
    and :data:`_PROBE_FLIGHT` for why one bounded probe is not enough on its own.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        run_bounded(
            _database_answers,
            budget_seconds=READY_PROBE_BUDGET_SECONDS,
            name="readiness_probe",
            single_flight=_PROBE_FLIGHT,
        )
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        checks["database"] = f"error: {type(exc).__name__}"
        healthy = False

    try:
        from research_contracts.canonical.registry import canonical_schemas

        from application.services.flow import EXPORT_SCHEMA_REF, seal_mode

        count = len(canonical_schemas())
        if count == 0:
            raise RuntimeError("no released schemas")
        mode, degraded = seal_mode(EXPORT_SCHEMA_REF)
        checks["export_seal"] = (
            f"ok ({count} released schemas); sealed_with={mode}"
            if degraded is None
            else f"degraded ({count} released schemas); sealed_with={mode}; {degraded}"
        )
    except Exception as exc:  # noqa: BLE001
        checks["export_seal"] = f"error: {type(exc).__name__}: {exc}"
        healthy = False

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready", "checks": checks, "timestamp": _now()},
    )
