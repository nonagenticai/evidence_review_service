"""`/metrics` answers a caller carrying no credential at all — and only `/metrics`.

Prometheus scrapes with no token. A `/metrics` behind license-authz is a
`/metrics` nobody ever reads, so the endpoint has to be exempt exactly the way
`/health`, `/health/live` and `/health/ready` are.

## How the exemption actually works here, which is worth stating

There is no auth middleware in this service and no router-level
`dependencies=[...]`. A route is authenticated **iff its signature asks for an
`Actor`**, and `api.dependencies.get_actor` is the only constructor of one. So
`/health` is not exempt because a list somewhere names it; it is exempt because
`health()` takes no such parameter. `metrics()` takes none either. That is the
whole mechanism, and this module is what stops it being re-derived by hand.

## ⛔ The positive control is the point

A test that only asserts `/metrics` → 200 passes just as well against an app
with authentication switched OFF, which is the default in this suite
(`ENABLE_AUTH=false`). It would then be pinning nothing at all: it could not
distinguish "the exemption works" from "nothing is protected".

So auth is turned **ON** for this module and the SAME unauthenticated client is
pointed at a protected route first. `/api/reviews` must answer `401`. Only once
that has been observed does `/metrics` → 200 mean the exemption is real, and a
future change that broke authentication wholesale would red the control rather
than silently greening the claim.

This runs in the opposite direction too, and that direction matters more: if
somebody later adds a blanket auth dependency, `/metrics` starts answering 401,
Prometheus starts scraping nothing, and NOTHING else in this repo would notice —
a scrape that fails produces no series to alert on, which is the same silence the
whole metrics change exists to end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):  # noqa: ANN201, ANN001
    """The app with authentication genuinely ON.

    `ENVIRONMENT=test` rather than `production` because `get_actor` refuses
    header identity outright in production; the flag under test here is
    `ENABLE_AUTH`, and `test` is the environment in which it is honoured.
    """
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")

    import config

    config.get_settings.cache_clear()
    from app import create_app

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as connected:
            yield connected
    finally:
        config.get_settings.cache_clear()


def test_a_protected_route_refuses_the_same_uncredentialed_caller(client) -> None:  # noqa: ANN001
    """THE CONTROL. Without this, the test below proves nothing."""
    response = client.get("/api/reviews")

    assert response.status_code == 401, (
        "a route with no credential answered "
        f"{response.status_code}, so authentication is not actually on in this fixture and the "
        "exemption test below would pass against an app where nothing is protected."
    )


def test_metrics_answers_a_caller_with_no_credential(client) -> None:  # noqa: ANN001
    """No `Authorization`, no `X-Research-Context`, no `X-Tenant-Id`. 200."""
    response = client.get("/metrics")

    assert response.status_code == 200, (
        f"/metrics answered {response.status_code} to an uncredentialed caller. Prometheus sends "
        "no token, so this endpoint would never be scraped and every alert written against it "
        "would sit permanently in 'no data' — which looks identical to 'nothing is wrong'.\n"
        f"{response.text[:400]}"
    )
    assert response.headers["content-type"].startswith("text/plain")


def test_the_health_probes_are_exempt_by_the_same_mechanism(client) -> None:  # noqa: ANN001
    """The rule this endpoint was made to follow, measured beside it.

    If `/health` ever stops being reachable uncredentialed, then `/metrics`
    matching it is no longer evidence of anything, and this file's premise —
    "follow the health probes' exemption exactly" — has quietly expired.
    """
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code in (200, 503)
