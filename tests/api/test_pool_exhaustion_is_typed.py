"""Connection-pool exhaustion was the one failure with no problem+json.

Every other failure in this service carries a stable `code` slug —
`api/problem_details.py` exists for that. Pool exhaustion did not, because of a
class-hierarchy detail:

    SQLAlchemyError
      +- TimeoutError        <- QueuePool raises this on checkout timeout
      +- StatementError
           +- DBAPIError     <- the handler `app.py` registers

`sqlalchemy.exc.TimeoutError` is a SIBLING of `StatementError`, not a descendant
of `DBAPIError`, so it matched neither `add_exception_handler` and propagated to
Starlette's `ServerErrorMiddleware` as a bare `500` — no body a client can branch
on, and the wrong signal entirely: the request was well-formed, the database was
healthy, and the correct answer was "retry".

## Why the threadpool cap ships with it, and not after it

Every route here except the health probes is a plain `def`, or is dispatched
through `run_in_threadpool` since the import route moved off the event loop, so
each one takes a thread AND then asks for a connection. anyio's default
`CapacityLimiter` is 40; `pool_size + max_overflow` defaults to 15; the
deployment runs one replica. Forty handlers contending for fifteen connections is
the actual cause, and the 25 that lose spend `pool_timeout` — SQLAlchemy's
default 30 seconds — waiting before they fail.

The handler alone would rename the symptom. Capping the limiter to the pool makes
an over-subscribed instance QUEUE instead of time out, which is a wait rather
than an error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tenants import tenant

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(scope="module", autouse=True)
def _src_on_path():  # noqa: ANN202
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    yield


def test_sqlalchemy_timeout_is_not_a_dbapi_error() -> None:
    """The premise, asserted rather than assumed.

    If a future SQLAlchemy reparents `TimeoutError` under `DBAPIError`, the
    dedicated handler becomes redundant and this file's whole argument changes.
    Better to be told than to keep carrying a handler for a case that moved.
    """
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError, StatementError
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    assert issubclass(SATimeoutError, SQLAlchemyError)
    assert not issubclass(SATimeoutError, DBAPIError), (
        "sqlalchemy.exc.TimeoutError is now a DBAPIError. The generic handler would already catch it "
        "and the dedicated one in app.py is redundant — check which of the two wins before removing "
        "either."
    )
    assert not issubclass(SATimeoutError, StatementError)


def test_the_app_registers_a_handler_for_it() -> None:
    """A handler that is written but never registered catches nothing.

    Asserted against the built app's own registry rather than by reading
    `app.py`, so moving the registration into a helper does not silently drop it.
    """
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    from app import create_app

    app = create_app()
    assert SATimeoutError in app.exception_handlers, (
        "no exception handler is registered for sqlalchemy.exc.TimeoutError; pool exhaustion is a "
        f"bare 500 again. Registered: {sorted(str(k) for k in app.exception_handlers)}"
    )


def test_pool_exhaustion_answers_503_with_a_slug_and_a_retry_after() -> None:
    """Driven end to end: the handler is invoked and its response is the contract.

    A route is added to a throwaway app that raises exactly what `QueuePool`
    raises. That is the honest way to reach it — genuinely exhausting the pool
    would need `pool_timeout` seconds of wall-clock per attempt and would make
    this test a flake generator, and the thing under test is the RESPONSE, not
    SQLAlchemy's ability to raise.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    from app import create_app

    app = create_app()

    @app.get("/api/_test_pool_exhaustion")
    def _boom() -> dict:  # noqa: ANN202
        raise SATimeoutError("QueuePool limit of size 10 overflow 5 reached, connection timed out")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/_test_pool_exhaustion", headers={"X-Tenant-Id": tenant("unit"), "X-Subject-Id": "s"}
        )

    assert response.status_code == 503, (
        f"pool exhaustion answered {response.status_code}, not 503. A 500 tells a caller they did "
        f"something wrong; they did not, and retrying is correct. Body: {response.text}"
    )
    assert response.headers.get("content-type", "").startswith("application/problem+json"), (
        f"the response is not problem+json: {response.headers.get('content-type')!r}"
    )
    body = response.json()
    assert body.get("code") == "service_unavailable", body
    assert "retry" in body.get("detail", "").lower(), (
        f"the detail does not tell the caller the request is safe to retry: {body!r}"
    )
    assert response.headers.get("Retry-After"), "no Retry-After header on a 503 that expects a retry"


def test_the_threadpool_is_capped_to_the_connection_pool() -> None:
    """40 threads against 15 connections on one replica was the actual cause.

    Read after `lifespan` has run, through the same `TestClient` context that
    triggers startup — asserting on the module-level default would measure anyio,
    not this service.
    """
    from anyio import to_thread
    from fastapi.testclient import TestClient

    from app import create_app
    from config import get_settings

    settings = get_settings()
    expected = settings.db_pool_size + settings.db_max_overflow

    app = create_app()

    observed: dict[str, float] = {}

    # `async def` deliberately. `current_default_thread_limiter()` resolves the
    # limiter for the RUNNING event loop, and a plain `def` route is executed in
    # a worker thread that has no async context — it raises `NoEventLoopError`
    # rather than reporting a wrong number, which is at least loud, but it
    # measures nothing. This runs on the same loop `lifespan` set the cap on.
    @app.get("/api/_test_thread_limit")
    async def _limit() -> dict:  # noqa: ANN202
        observed["tokens"] = to_thread.current_default_thread_limiter().total_tokens
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/api/_test_thread_limit").status_code == 200

    assert observed, "the probe route never ran; nothing was measured"
    assert observed["tokens"] == expected, (
        f"the threadpool allows {observed['tokens']} concurrent handlers against a pool of "
        f"{expected}. Every route here takes a thread and then asks for a connection, so the "
        "difference is requests that will spend pool_timeout waiting before failing."
    )
    # The floor: anyio's default is 40. If `expected` ever equalled it, this test
    # would pass without the cap having done anything.
    assert expected != 40, (
        "db_pool_size + db_max_overflow is exactly anyio's default of 40, so this test cannot "
        "distinguish a working cap from no cap at all. Change the fixture, not the assertion."
    )
