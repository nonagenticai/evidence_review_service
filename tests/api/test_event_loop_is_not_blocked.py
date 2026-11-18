"""The process must keep answering while an import is running.

FastAPI runs an `async def` handler ON the event loop and a plain `def` handler
in a threadpool. `create_import` was `async def` with the entire import inside
it — a JSON decode of up to 32 MB, the parser, the denylist walk and three
synchronous SQLAlchemy INSERTs per record, none of which yields. Every other
route in this service is a plain `def`; this one was the outlier, and it is the
slowest route there is.

The consequence is not "requests queue". `/health/live` is the liveness probe,
and its own docstring explains that it deliberately touches no dependency so a
database blip cannot restart the pod. A blocked event loop defeats that from the
other side: the probe is unanswerable not because a dependency is down but
because the process is busy, kubelet sees the timeout, and a large import
restarts the pod that is running it. A realistic systematic-review first pass is
3,000-8,000 records.

## Why this test stubs the service call instead of importing 5,000 records

The property under test is "the blocking work is not on the event loop", and
`svc.import_payload` is the last and heaviest blocking step — so a stand-in that
blocks for a known duration measures exactly that, deterministically, in about a
second. Driving it with a real corpus large enough to take seconds would measure
the same thing more slowly and less reliably: a 200-record import takes 0.09s
here, so the corpus would have to be enormous before the timing was unambiguous,
and how enormous depends on the machine.

Assertion is on ORDERING, not only on elapsed time. `not task.done()` is the
half that cannot be satisfied by a fast machine: if the loop were blocked, the
probe could not return until the import had finished, so the import task would
necessarily be complete by the time the probe's response existed.

## The second blocking path: authentication (added with the licence-token rung)

The import was the loudest way to hold the loop. It was not the only one, and
the other one arrived by accident.

`get_actor` was a plain `def`, so FastAPI ran it — and the synchronous JWKS fetch
inside `auth.verifier` — in the threadpool. Introspecting an opaque licence token
needs `await`, so `get_actor` became `async def`, and that one keyword moved the
JWKS fetch onto the event loop. `PyJWKClient` reads over `urllib.request.urlopen`
with a 10s timeout, and the unknown-`kid` branch does it TWICE, so a single token
carrying a `kid` this pod has not seen — which is exactly what a Keycloak key
rotation looks like from here — would stall every other request in the process
for up to 20 seconds, `/health/live` included. Same failure as the import: the
pod is restarted for being busy, not for being broken.

`auth.verifier.verify_token_async` is what keeps it off the loop. The two tests
at the bottom of this file pin both halves — that the coroutine exists and is
what `api.dependencies` calls, and that a blocking verification genuinely leaves
the loop free.

⚠️ Those two need no database, but the module-level `skipif` above gates the whole
file on one. They therefore run in CI (which provisions an ephemeral postgres)
and skip on a dev box without one. Left that way deliberately rather than
re-scoping the mark onto the tests that need it, which would newly run the
structural import test in an environment it has never run in.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-event-loop")
LEAD = "loop-lead"

#: How long the stubbed import blocks for. Long enough that a blocked loop is
#: unmistakable, short enough that the test costs a second.
IMPORT_BLOCKS_FOR = 1.0

#: What a served-while-busy probe may take. The gap to `IMPORT_BLOCKS_FOR` is
#: the flake margin: a blocked loop would make the probe take ~1.0s, so anything
#: under 0.4s cannot be a blocked loop on any machine where the ratio holds.
PROBE_BUDGET = 0.4


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def app():  # noqa: ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    return create_app()


@pytest.fixture(scope="module")
def review(app) -> str:  # noqa: ANN001
    """A review the import route will accept, created over the same ASGI app."""
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/reviews",
            json={"project_id": "loop", "external_key": f"loop-{uuid.uuid4()}", "title": "Event loop"},
            headers=headers(),
        )
        assert created.status_code == 201, created.text
        return created.json()["review_id"]


def test_the_import_handler_does_not_hold_the_blocking_work_itself() -> None:
    """The structural half: the handler delegates rather than doing the work inline.

    `create_import` stays `async def` — `Request.body()` is a coroutine and there
    is no synchronous body reader, and the `bytes`-`Body()` alternative would
    hand the handler a parsed dict, destroying the raw bytes
    `import_batches.raw_payload_digest` is computed over. So "is it a coroutine
    function?" cannot be the pin here the way it is for `health.ready`. What can
    be pinned is that the blocking body lives in a separate SYNC function, which
    is the thing `run_in_threadpool` needs in order to have something to hand
    off.
    """
    from api.routers import imports

    assert inspect.iscoroutinefunction(imports.create_import)
    assert not inspect.iscoroutinefunction(imports._import_payload_blocking), (
        "the blocking import body must be a plain `def` so it can be dispatched off the loop"
    )


async def test_health_live_is_served_while_an_import_is_in_flight(app, review, monkeypatch) -> None:
    """The behavioural half. On `393ae61` both assertions below fail.

    The stand-in blocks for `IMPORT_BLOCKS_FOR` seconds in place of the real
    import. With the work on the event loop the probe cannot be served until that
    is over, so it measures ~1.0s AND the import task is already `done()` when
    the probe's response exists. With the work in the threadpool the probe is
    answered immediately and the import is still running.
    """
    from httpx import ASGITransport, AsyncClient

    def _slow_import(session, actor, review_id, **kwargs) -> dict[str, Any]:  # noqa: ANN001, ARG001
        time.sleep(IMPORT_BLOCKS_FOR)
        return {"import_batch_id": str(uuid.uuid4()), "accepted": 0, "quarantined": 0, "replayed": False}

    # Patched on the service module, which is what the router resolves through at
    # call time. This is the last blocking step in the handler, so proving IT is
    # off the loop proves the whole tail is.
    monkeypatch.setattr("application.services.imports.import_payload", _slow_import)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://evidence-review.test") as client:
        importing = asyncio.create_task(
            client.post(
                f"/api/reviews/{review}/imports?source_kind=canonical_json",
                content=json.dumps({"records": [{"title": "A study", "year": 2020}]}),
                headers={**headers(), "Content-Type": "application/octet-stream"},
                timeout=30.0,
            )
        )
        # Hand the loop over so the import actually reaches its blocking section
        # before the probe is issued; otherwise the probe could win by starting
        # first, which would prove nothing.
        await asyncio.sleep(0.05)

        started = time.perf_counter()
        live = await client.get("/health/live", timeout=30.0)
        elapsed = time.perf_counter() - started

        assert live.status_code == 200, live.text
        assert live.json()["status"] == "alive"
        assert not importing.done(), (
            "the liveness probe only returned once the import had finished, which means the "
            "import was running on the event loop"
        )
        assert elapsed < PROBE_BUDGET, (
            f"the liveness probe took {elapsed:.2f}s while an import was in flight; the event "
            f"loop was blocked (the import blocks for {IMPORT_BLOCKS_FOR}s)"
        )

        response = await importing
        assert response.status_code == 201, response.text


#: How long the stubbed token verification blocks for. Same argument as
#: `IMPORT_BLOCKS_FOR`, and the same flake margin against `PROBE_BUDGET`.
VERIFY_BLOCKS_FOR = 1.0


def test_the_auth_dependency_delegates_its_blocking_verification() -> None:
    """The structural half, and all three parts of it are load-bearing.

    `get_actor` must be a coroutine — it awaits licence introspection, and a
    plain `def` could not. `verify_token` must stay a plain `def` — it is what
    gets handed to a thread, and there has to be something synchronous to hand
    over (and `tests/security/test_token_verification.py` drives it directly).
    `verify_token_async` must be a coroutine and must be the name
    `api.dependencies` actually holds: importing the offloading wrapper and then
    calling the blocking one is a one-character regression that no timing test on
    a fast machine would reliably catch.
    """
    from api import dependencies
    from auth import verifier

    assert inspect.iscoroutinefunction(dependencies.get_actor)
    assert inspect.iscoroutinefunction(verifier.verify_token_async)
    assert not inspect.iscoroutinefunction(verifier.verify_token), (
        "verify_token must stay a plain `def` so there is something to dispatch to a thread"
    )
    assert dependencies.verify_token_async is verifier.verify_token_async, (
        "api.dependencies must call the offloading wrapper, not the blocking verifier"
    )


async def test_token_verification_does_not_hold_the_event_loop(monkeypatch) -> None:
    """The behavioural half. Fails if `_verified_claims` stops offloading.

    The stand-in blocks for `VERIFY_BLOCKS_FOR` seconds in place of the real JWKS
    fetch + `jwt.decode`. With the work offloaded, the `sleep(0.05)` below returns
    on time and the verification is still running. With the work on the loop, that
    sleep cannot resume until the verification has finished — so it measures ~1.0s
    AND the task is already `done()`, which is the assertion a fast machine cannot
    accidentally satisfy.

    Nothing is stubbed above the seam under test: `_verified_claims` is the real
    one and `verify_token_async` is the real one, so the only way this passes is
    if the dispatch is genuinely there.
    """
    from api import dependencies
    from auth import verifier
    from config import Settings

    claims = {"sub": "loop-subject", "tenant_id": str(uuid.uuid4())}

    def _slow_verify(token: str, settings: Any) -> dict[str, Any]:  # noqa: ARG001
        time.sleep(VERIFY_BLOCKS_FOR)
        return claims

    monkeypatch.setattr(verifier, "verify_token", _slow_verify)

    verifying = asyncio.create_task(dependencies._verified_claims("header.payload.signature", Settings()))
    # Hand the loop over so the verification actually reaches its blocking
    # section before the measurement starts; otherwise the measurement could win
    # by starting first, which would prove nothing.
    await asyncio.sleep(0.05)

    started = time.perf_counter()
    await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - started

    assert not verifying.done(), (
        "the event loop only resumed once token verification had finished, which means the "
        "JWKS fetch is running on the loop — an unknown `kid` would stall /health/live"
    )
    assert elapsed < PROBE_BUDGET, (
        f"a 0.05s sleep took {elapsed:.2f}s while a token was being verified; the event loop "
        f"was blocked (verification blocks for {VERIFY_BLOCKS_FOR}s)"
    )
    assert await verifying == claims
