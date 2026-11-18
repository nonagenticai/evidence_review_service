"""Heartbeat freshness becomes a metric — in the container that has one, and nowhere else.

`worker_heartbeat` already answers "is this loop finishing ticks?" for the
`livenessProbe`. That answer restarts a pod and then vanishes: nothing off-pod
can see it, so nobody can alert on *"the relay worker has been restarting on a
stale heartbeat all night"*, only on the restart count. This exports it.

## ⛔ Emitted only where the heartbeat FILE is

`WORKER_HEARTBEAT_PATH` is per-pod state written by `worker.run()` in the
worker's own container. In the API's container there is no file and there never
will be — so a `status=2` ("no heartbeat") exported from the API would be a
permanent false alarm, and a permanent false alarm is how an operator learns to
ignore the one metric that is meant to wake them. Absent instead.

## ⛔ The status code is `worker_heartbeat.check`'s, verbatim

Not a re-derivation of "is this stale". The `livenessProbe` and the alert must
not be able to come to different conclusions about the same file, and the only
way to guarantee that is for there to be one function deciding it. A second
implementation here would agree on the day it was written and drift the first
time the staleness bound changed — and the bound is DERIVED from the interval
the loop itself recorded, so it is not even a constant to keep in step.
"""

from __future__ import annotations

import pytest

import metrics
import worker_heartbeat
from config import Settings


@pytest.fixture()
def heartbeat(tmp_path, monkeypatch):  # noqa: ANN201, ANN001
    """A settings object whose heartbeat path is this test's own file."""
    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(path))
    return path, Settings()


def _samples(settings, monkeypatch=None, now=None) -> dict[str, float]:  # noqa: ANN001
    """One clock for the whole scrape.

    ⛔ The clock is driven through `worker_heartbeat._now` — the seam that module
    documents as existing for exactly this — and NOT through a `now` parameter on
    the collector. That is the point of a measurement this suite already made:
    the first version of `HeartbeatCollector` took its own `now` and left
    `check()` on the wall clock, and exported `age=2.0` beside `status=1`
    (stale) from a single scrape. Driving one seam is what makes the two numbers
    describe the same instant, in the test and in production alike.
    """
    if now is not None:
        assert monkeypatch is not None, "driving the clock needs monkeypatch"
        monkeypatch.setattr(worker_heartbeat, "_now", now)
    collector = metrics.HeartbeatCollector(settings)
    return {sample.name: sample.value for metric in collector.collect() for sample in metric.samples}


def test_a_process_with_no_heartbeat_file_exports_nothing(heartbeat) -> None:  # noqa: ANN001
    """The API pod. ⛔ Absent, not `status=2`.

    `status=2` is `worker_heartbeat`'s "this process has not completed a single
    tick", which is a genuine alarm in a worker and a meaningless one in a
    process that is not a worker. Exporting it from the API would fire forever
    and teach whoever is on call to filter the series out.
    """
    path, settings = heartbeat
    assert not path.exists()

    assert _samples(settings) == {}, (
        "a process with no heartbeat file exported heartbeat metrics. In the API's container that "
        "is a permanent false alarm on a series whose whole job is to be believed."
    )


def test_a_fresh_heartbeat_reports_alive_with_its_age_and_its_bound(heartbeat, monkeypatch) -> None:  # noqa: ANN001
    path, settings = heartbeat
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=1_000.0, interval=5.0, ticks=42))
    assert path.exists()

    samples = _samples(settings, monkeypatch, now=lambda: 1_002.0)

    assert samples[metrics.HEARTBEAT_STATUS] == float(worker_heartbeat.EXIT_ALIVE)
    assert samples[metrics.HEARTBEAT_AGE] == pytest.approx(2.0)
    # 6 x the 5s interval the LOOP recorded, over the 20s floor. Exported beside
    # the age so an alert does not hard-code a cadence the loop may not be
    # running at — `WORKER_INTERVAL_SECONDS=60` makes this 360.
    assert samples[metrics.HEARTBEAT_BOUND] == pytest.approx(30.0)


def test_a_stale_heartbeat_is_visible_as_stale_and_not_merely_as_a_large_age(heartbeat, monkeypatch) -> None:  # noqa: ANN001
    """Both facts, because an alert should not have to recompute the bound.

    `age > bound` is derivable from the two gauges, and `status` says it outright
    using the SAME function the livenessProbe runs. The redundancy is deliberate:
    the derived form is what a dashboard graphs, the status is what cannot
    disagree with the probe that restarts the pod.
    """
    _path, settings = heartbeat
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=1_000.0, interval=5.0, ticks=7))

    samples = _samples(settings, monkeypatch, now=lambda: 1_500.0)

    assert samples[metrics.HEARTBEAT_STATUS] == float(worker_heartbeat.EXIT_STALE)
    assert samples[metrics.HEARTBEAT_AGE] > samples[metrics.HEARTBEAT_BOUND]


def test_an_unreadable_heartbeat_is_a_refusal_and_not_a_missing_age(heartbeat) -> None:  # noqa: ANN001
    """A file that exists and is not the document this module writes.

    `worker_heartbeat.read` returns `None` for it, deliberately: nothing about
    that state says the loop is working. So `status` reports the refusal and NO
    age is emitted — an age would have to be invented, and an invented freshness
    is the one number here that must never exist.
    """
    path, settings = heartbeat
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    samples = _samples(settings)

    assert samples[metrics.HEARTBEAT_STATUS] == float(worker_heartbeat.EXIT_UNREADABLE)
    assert metrics.HEARTBEAT_AGE not in samples, "an age was invented for a heartbeat nothing could read"


def test_the_status_code_is_the_probes_own(heartbeat) -> None:  # noqa: ANN001
    """One function decides "stale", so the alert and the probe cannot disagree.

    Asserted against `worker_heartbeat.check` directly rather than against a
    literal, so a change to the staleness rule moves both together or reds here.
    """
    _path, settings = heartbeat
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=1_000.0, interval=5.0))

    exported = _samples(settings)[metrics.HEARTBEAT_STATUS]
    probe_code, _message = worker_heartbeat.check(settings)

    assert exported == float(probe_code)


def test_the_heartbeat_reaches_the_scrape_body(heartbeat, monkeypatch) -> None:  # noqa: ANN001
    """End to end through the registry, not just the collector.

    A collector that is never registered emits nothing, and every test above
    would still pass.
    """
    from fastapi.testclient import TestClient

    from app import create_app

    _path, settings = heartbeat
    worker_heartbeat.advance(settings, worker_heartbeat.Heartbeat(at=1_000.0, interval=5.0))

    body = TestClient(create_app(settings)).get("/metrics").text

    assert metrics.HEARTBEAT_STATUS in body, body[:600]
