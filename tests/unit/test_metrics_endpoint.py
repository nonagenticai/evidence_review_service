"""`GET /metrics` exists, is in the Prometheus text format, and refuses to lie.

The service exported NO application metric at all — `up{namespace="evidence-review"}`
was empty in the estate's Prometheus and every series carrying that namespace
came from kube-state-metrics watching from outside. This module pins the
endpoint that ends that.

## ⛔ The half of this that is not "does it return 200"

Two of the four tests below are about what is **absent**, and they are the
load-bearing ones. `worker._Counters` lives in the WORKER's process; the API is a
different Deployment. A `Counter` registered in the API's registry would export
`evidence_review_relay_events_relayed_total 0` for the life of the pod — a flat
line that reads exactly like a healthy idle relay, and would keep reading that
way while the relay was wedged. An absent series cannot be misread; a zero can.

So `test_the_api_registry_carries_no_worker_counter` is not a tidiness check. It
is the whole reason `build_registry` takes `counters` as a parameter instead of
creating the counters at import time, and re-creating them at module level is a
three-line edit that nothing else would notice.
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

import metrics
from api.routers import metrics as metrics_router
from app import create_app


def _scrape() -> str:
    return TestClient(create_app()).get("/metrics").text


def test_the_scrape_answers_in_the_prometheus_text_format() -> None:
    response = TestClient(create_app()).get("/metrics")

    assert response.status_code == 200, response.text
    # `text/plain; version=0.0.4` / `version=1.0.0` depending on the client
    # release. The version is the client's to choose; that it is the Prometheus
    # exposition format and not, say, `application/json` is ours.
    assert response.headers["content-type"].startswith("text/plain"), response.headers["content-type"]
    assert "version=" in response.headers["content-type"]
    body = response.text
    assert f"# HELP {metrics.BUILD_INFO}" in body, body[:500]
    assert f'{metrics.BUILD_INFO}{{component="api"' in body, body[:500]


def test_the_scrape_does_not_run_its_database_query_on_the_event_loop() -> None:
    """The same defect `health.ready` carried, and the same one-keyword revert.

    `RelayDepthCollector` opens a synchronous `session_scope()` on every scrape.
    FastAPI runs an `async def` handler ON the event loop and a plain `def` in
    the threadpool, so as a coroutine a slow database would block every other
    request in the process for the duration of the scrape — `/health/live`
    included, which is the exact escalation from "readiness degraded" to
    "liveness failed across the replica" that `live()` exists to avoid.

    Pinned structurally, because the property is invisible at the call site: the
    route behaves identically either way until something is slow.
    """
    assert not inspect.iscoroutinefunction(metrics_router.metrics), (
        "`metrics` is an `async def`, so its synchronous database query now runs on the event "
        "loop and a slow database blocks /health/live along with everything else."
    )


def test_the_api_registry_carries_no_worker_counter() -> None:
    """⛔ Absent, never zero. See this module's docstring.

    Checked against `metrics._COUNTERS` rather than a hand-written list of six
    names, so a seventh counter added to the worker's tally cannot quietly
    escape this test by not being listed here.
    """
    body = _scrape()

    for base, _attribute, _documentation in metrics._COUNTERS:
        assert f"{base}_total" not in body, (
            f"{base}_total is exported by the API process, where it can only ever be 0. "
            "A flat zero reads as a healthy idle relay; this process has never relayed anything "
            "and never will. Register the counters only where the tally lives — worker.run()."
        )


def test_the_observable_flag_is_present_even_when_the_depths_are_not() -> None:
    """The flag is the thing that makes an absence legible.

    Without it, "no depth series" and "this service was never scraped" look the
    same to whoever is reading the dashboard at 03:00. With it, there is always
    exactly one sample saying which.
    """
    body = _scrape()

    assert metrics.DEPTH_OBSERVABLE in body, (
        f"{metrics.DEPTH_OBSERVABLE} is missing. It is the ONLY one of the four depth series that "
        "must always be present: it is what distinguishes 'this process cannot see the queue' "
        "from 'this process was never scraped'."
    )
    reasons = {metrics.REASON_OBSERVABLE, metrics.REASON_ROW_LEVEL_SECURITY, metrics.REASON_QUERY_FAILED}
    assert any(f'{metrics.DEPTH_OBSERVABLE}{{reason="{reason}"}}' in body for reason in reasons), (
        f"the reason label carried a value outside the closed vocabulary {sorted(reasons)}. "
        "A label whose values are invented at the call site cannot be alerted on."
    )


def test_a_second_app_does_not_collide_with_the_first() -> None:
    """One registry per app, not `prometheus_client.REGISTRY`.

    The global default registry is process-wide, so a second `create_app()` in
    the same interpreter — which this suite does dozens of times — would raise
    `Duplicated timeseries in CollectorRegistry`. That failure would not look
    like a metrics bug; it would look like the second test in every module
    breaking for no reason.
    """
    first = TestClient(create_app()).get("/metrics")
    second = TestClient(create_app()).get("/metrics")

    assert first.status_code == 200
    assert second.status_code == 200, second.text
