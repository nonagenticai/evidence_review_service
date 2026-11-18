"""The Prometheus scrape surface.

## ⛔ Exempt from license-authz, by the same mechanism as `/health`

There is no auth middleware in this service and there is no router-level
`dependencies=[...]`: a route is authenticated iff its signature asks for an
`Actor`, and `api.dependencies.get_actor` is the only constructor of one. That
is the whole of the mechanism — `api/routers/health.py` is "exempt" because
`health()`, `live()` and `ready()` take no such parameter, not because anything
anywhere lists them.

So this handler takes no actor either, and that is deliberate rather than an
oversight: Prometheus scrapes with no credential at all, and a `/metrics` behind
a licence token is a `/metrics` that is never scraped. The exemption is pinned
by `tests/api/test_metrics_is_exempt_from_license_authz.py`, which asserts a
scrape with NO `Authorization` and NO `X-Research-Context` header answers 200 —
a regression there would be a real auth bug in the opposite direction, so it is
worth a test that fails loudly either way.

⚠️ What this endpoint therefore must never carry is per-tenant data. It does not:
`metrics.RelayDepthCollector` emits three whole-table counts and nothing that
identifies a tenant, a review or a subject, and it emits even those only where
the process can honestly see them.

## A plain `def`, deliberately

Same argument as `health.ready`, and the same one-keyword regression. The scrape
runs a synchronous `session_scope()` query through the depth collector; as an
`async def` FastAPI would run that ON the event loop, so a slow database would
block every other request in the process — including `/health/live`, whose whole
justification is that it answers while a dependency is misbehaving. A plain
`def` is dispatched to the threadpool. Pinned structurally by
`tests/unit/test_metrics_endpoint.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()


@router.get("/metrics", include_in_schema=False, summary="Prometheus scrape")
def metrics(request: Request) -> Response:
    """Render this process's registry in the Prometheus text exposition format.

    The registry is built once per app in `app.create_app` and lives on
    `app.state`, rather than being rebuilt here or taken from
    `prometheus_client.REGISTRY`. Per-app means each `create_app()` in a test
    session gets its own, which is what stops the second one raising
    `Duplicated timeseries`; the collectors inside it are lazy, so the DB query
    and the heartbeat read happen on scrape and not at import.
    """
    return Response(generate_latest(request.app.state.metrics_registry), media_type=CONTENT_TYPE_LATEST)
