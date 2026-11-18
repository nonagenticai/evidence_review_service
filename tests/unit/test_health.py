"""Health surface. Thin by design — the probes are the deployment contract."""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from api.routers import health
from app import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_health_is_liveness_only() -> None:
    response = _client().get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "evidence-review"


def test_liveness_touches_no_dependency() -> None:
    """A liveness probe that checks the database turns a blip into a crash-loop."""
    response = _client().get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_reports_the_export_seal() -> None:
    """Readiness must name the seal, not just the database.

    A pod that cannot hash an export cannot finish review work, so it must not be
    sent any. The check is present whether or not it passes in this environment.
    """
    response = _client().get("/health/ready")
    assert response.status_code in (200, 503)
    checks = response.json()["checks"]
    assert "export_seal" in checks
    assert "database" in checks


def test_the_readiness_probe_does_not_run_its_db_call_on_the_event_loop() -> None:
    """`ready` was `async def` while doing synchronous `session_scope()` I/O.

    FastAPI runs an `async def` handler ON the event loop and a plain `def`
    handler in a threadpool, so the database call at `health.py:82-83` blocked
    every other request in the process for its duration — including
    `/health/live`, whose entire justification is that it must answer while a
    dependency is misbehaving. A hanging database therefore escalated a
    readiness degradation into a liveness failure on every replica at once,
    which is the crash-loop `live()` exists to avoid.

    Pinned structurally rather than by timing. The property is invisible at the
    call site — the route works identically either way until something is slow —
    and restoring the defect is a one-keyword edit.
    """
    assert not inspect.iscoroutinefunction(health.ready), (
        "ready() is `async def` again; its synchronous database call is back on the event loop"
    )
    # The two probes that touch nothing stay on the loop, where they are cheapest.
    assert inspect.iscoroutinefunction(health.live)
    assert inspect.iscoroutinefunction(health.health)
