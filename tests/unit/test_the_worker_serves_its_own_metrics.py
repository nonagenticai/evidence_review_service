"""The worker exports its own tally, from the only process that holds it.

`python -m worker_heartbeat` against the live pod already printed the six
numbers this module exports:

    alive: last completed tick 1.1s ago, bound 30.0s.
    ticks=1356 relayed=0 deliveries=0 poisoned=0 reaped=0 errors=0

They existed; nothing could scrape them. That is an EXPORT problem, and the
constraint that shapes the answer is that **the API cannot export them.**
`worker._Counters` is an object in the worker's memory, the API is a separate
Deployment, and a counter registered there reads `0` for the life of the pod.

So the worker gets its own listener. This module pins the three properties that
make that listener trustworthy: it renders the live tally rather than a copy of
it, it is never fatal, and a one-shot tick does not bind a port.
"""

from __future__ import annotations

import socket

import pytest

import metrics
import worker
from config import Settings


def _samples(registry) -> dict[str, float]:  # noqa: ANN001
    return {
        sample.name: sample.value
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name.startswith(metrics.PREFIX)
    }


def test_the_counters_render_the_live_tally_rather_than_a_copy() -> None:
    """⛔ ONE source of truth.

    The obvious implementation is a `prometheus_client.Counter` `.inc()`d beside
    every `counters.record(...)` call. That makes two tallies for one fact, and
    the failure mode of two tallies is that they drift silently and the exported
    one is the one nobody cross-checks.

    `WorkerCounterCollector` READS `_Counters`, so this test can mutate the
    dataclass after the collector was built and see the scrape move. A mirrored
    implementation would report the value it was told about at registration time
    and this assertion would fail — which is the whole point of writing it this
    way round.
    """
    counters = worker._Counters()
    collector = metrics.WorkerCounterCollector(counters)

    counters.record(relayed=3, deliveries=5, reaped=2, poisoned=1)
    counters.errors += 7

    rendered = {sample.name: sample.value for metric in collector.collect() for sample in metric.samples}

    assert rendered[f"{metrics.TICKS}_total"] == 1.0
    assert rendered[f"{metrics.RELAYED}_total"] == 3.0
    assert rendered[f"{metrics.DELIVERIES}_total"] == 5.0
    assert rendered[f"{metrics.POISONED}_total"] == 1.0
    assert rendered[f"{metrics.REAPED}_total"] == 2.0
    assert rendered[f"{metrics.ERRORS}_total"] == 7.0


def test_every_counter_the_worker_keeps_is_exported() -> None:
    """A field added to `_Counters` and not to `_COUNTERS` is a number that stays invisible.

    `_Counters` also carries `last_logged_at`, which is bookkeeping for the
    periodic log line and not a tally, so it is named here as a deliberate
    exclusion rather than silently missed.
    """
    tallies = {
        name
        for name, annotation in worker._Counters.__annotations__.items()
        if annotation == "int" and name != "last_logged_at"
    }
    exported = {attribute for _base, attribute, _documentation in metrics._COUNTERS}

    assert tallies == exported, (
        f"the worker keeps {sorted(tallies)} but exports {sorted(exported)}. A tally the worker "
        "counts and nobody can scrape is the exact gap this change exists to close."
    )


def test_the_counters_exist_only_where_the_tally_does() -> None:
    """⛔ The registry is what decides, and it decides by whether `counters` was passed.

    Absent in the API's registry, present in the worker's. A zero in the API
    would read as a healthy idle relay forever.
    """
    settings = Settings()
    api = _samples(metrics.build_registry(settings, component="api"))
    worker_side = _samples(metrics.build_registry(settings, component="worker", counters=worker._Counters()))

    for base, _attribute, _documentation in metrics._COUNTERS:
        assert f"{base}_total" not in api, f"{base}_total exists in a registry with no tally behind it"
        assert f"{base}_total" in worker_side, f"{base}_total is missing from the worker's own registry"


def test_a_metrics_port_that_cannot_be_bound_does_not_stop_the_relay(monkeypatch) -> None:  # noqa: ANN001
    """⛔ NEVER fatal.

    A worker that refuses to relay because it could not bind an observability
    port has converted a missing dashboard into an outage, which inverts the
    entire point of this change. The condition is real: a restart racing its
    predecessor's socket produces exactly this.

    The port is genuinely occupied by a socket this test holds, rather than
    simulated by monkeypatching `start_http_server` to raise — a mocked
    `OSError` would pass just as well against a `start_metrics_server` that
    catches the wrong exception type.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        monkeypatch.setenv("WORKER_METRICS_PORT", str(port))
        monkeypatch.setenv("WORKER_METRICS_HOST", "127.0.0.1")
        started = worker.start_metrics_server(Settings(), worker._Counters())
    finally:
        holder.close()

    assert started is False, "a bind failure was reported as a running metrics server"


def test_the_metrics_server_can_be_switched_off(monkeypatch) -> None:  # noqa: ANN001
    """`WORKER_METRICS_PORT=0`.

    The `evidence-review` namespace has no worker Service today. A deployment
    that cannot be scraped anyway should be able to stop opening a listener
    without a code change — and the off switch has to be a value nothing can
    bind, so that "disabled" and "failed to bind" are not the same observation.
    """
    monkeypatch.setenv("WORKER_METRICS_PORT", "0")

    assert worker.start_metrics_server(Settings(), worker._Counters()) is False


def test_the_listener_serves_the_worker_registry(monkeypatch) -> None:  # noqa: ANN001
    """It really answers, on a real socket, with the counters in the body.

    `prometheus_client.start_http_server` spawns a DAEMON thread, which is
    load-bearing rather than incidental: it is what lets a scrape answer while
    the relay loop is wedged inside a blocked query — the only condition under
    which the heartbeat-age gauge is worth anything.
    """
    from urllib.request import urlopen

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setenv("WORKER_METRICS_PORT", str(port))
    monkeypatch.setenv("WORKER_METRICS_HOST", "127.0.0.1")
    counters = worker._Counters()
    counters.record(relayed=2, deliveries=4, reaped=0, poisoned=0)

    assert worker.start_metrics_server(Settings(), counters) is True

    with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as response:  # noqa: S310
        body = response.read().decode()

    assert f'{metrics.BUILD_INFO}{{component="worker"' in body, body[:600]
    assert f"{metrics.RELAYED}_total 2.0" in body, body[:600]


def test_a_one_shot_tick_binds_no_port(monkeypatch) -> None:  # noqa: ANN001
    """A single tick is not a scrape target and must not try to become one.

    `run(once=True)` is driven repeatedly from this suite and from embedding
    processes; each call binding the same port would log a failure per call that
    says nothing about the code under test. Same argument as the signal handlers
    a few lines above it in `run()`, which are installed only on the looping
    path for the same reason.

    Structural rather than behavioural: a test that asserted "no port is
    listening" would pass equally against a `start_metrics_server` that bound and
    immediately failed.
    """
    calls: list[object] = []
    monkeypatch.setattr(worker, "start_metrics_server", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(worker, "preflight", lambda _settings: [])
    monkeypatch.setattr(worker, "tick", lambda _settings: (0, 0, 0))
    monkeypatch.setattr(worker.worker_heartbeat, "advance", lambda *_args, **_kwargs: None)

    assert worker.run(Settings(), once=True) == 0
    assert calls == [], "run(once=True) started a metrics server; only the looping path may bind a port"


@pytest.mark.parametrize("component", ["api", "worker"])
def test_the_build_info_names_which_process_answered(component: str) -> None:
    """Two processes scrape into one namespace; without this label they are one blur."""
    registry = metrics.build_registry(Settings(), component=component)

    rendered = [
        sample for metric in registry.collect() for sample in metric.samples if sample.name == metrics.BUILD_INFO
    ]

    assert len(rendered) == 1
    assert rendered[0].labels["component"] == component
