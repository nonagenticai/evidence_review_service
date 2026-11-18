"""`T3`: the lease reaper must read the DATABASE's clock, never the pod's.

## The defect, as measured

`jobs.reap_expired_leases` computed `at = now or _now()` — `datetime.now(UTC)`
inside whichever process happened to be running — and compared it against
`lease_expires_at`, which `claim_next` had written from the CLAIMING process's
`datetime.now(UTC)`. Two different pods, two different wall clocks, one
comparison.

A reaper whose host clock ran ten minutes fast therefore reaped a lease that
Postgres still considered live: `lease_expires_at > now()` in the database, and
the job nevertheless went back to `queued`, was claimed by a second pod, and ran
concurrently with the first — the exact duplication `13-…:§18.5` names as the
exit gate ("one job survives API and worker restart without duplication") and the
exact thing a lease exists to prevent. Nothing raised. The reaper's own log line
said it had recovered a stranded job.

## Why the skew is simulated by monkeypatching `jobs._now` and not by `now=`

`now=` is an explicit argument. A pod with a wrong clock does not pass one — it
calls `reap_expired_leases(session)` exactly as `worker.tick` does, and the
skew enters through the module-level clock function. Patching `_now` is
therefore the faithful reproduction: it changes what the PROCESS believes the
time is, leaves the call site identical to production's, and cannot pass merely
because the code stopped honouring a keyword.

## The controls

Two assertions here would pass against a reaper that is simply broken — one that
reaps nothing at all would satisfy "the live lease survived", and one that never
issues a long lease would satisfy "the stamp is not ten minutes ahead". So each
has a control beside it:

* the skewed reaper leaves the live lease alone, AND the same skewed reaper DOES
  reap a lease that has genuinely expired in the database (claimed for one
  second, then waited out). "It reaps nothing" is thereby excluded.
* the lease written under a skewed clock sits ~`lease_seconds` ahead of the
  DATABASE's `now()`, AND far less than the skew ahead of it. A stamp that
  merely looked plausible in isolation is thereby excluded.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import text  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

HASH = "sha256:" + "a" * 64

#: The skew the audit measured. Ten minutes is longer than
#: `DEFAULT_LEASE_SECONDS`, which is what makes a live lease look expired.
SKEW = timedelta(minutes=10)

#: Fresh per process, for the reason `test_two_workers_and_a_reaper.py` gives at
#: length: `claim_next` takes the OLDEST runnable row in scope, so a stable
#: tenant makes a re-run against a dirty database claim the previous run's job.
_RUN = uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class _Ctx:
    tenant_id: uuid.UUID
    subject: str

    def actor(self) -> Any:
        from actor import Actor, ActorRole

        return Actor(tenant_id=self.tenant_id, subject_id=self.subject, role=ActorRole("lead"))

    def review(self, title: str) -> uuid.UUID:
        from application.services import reviews as review_svc
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            review = review_svc.create_review(
                session,
                self.actor(),
                project_id="lease-clock",
                external_key=f"job-{uuid.uuid4()}",
                title=title,
            )
            return review.review_id

    def enqueue(self, review_id: uuid.UUID, **kwargs: Any) -> uuid.UUID:
        from application.services import jobs as svc
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            job = svc.enqueue(
                session,
                self.actor(),
                review_id=review_id,
                job_type="export",
                command_hash=HASH,
                **kwargs,
            )
            return job.job_id

    def state(self, job_id: uuid.UUID) -> dict[str, Any]:
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            row = (
                session.execute(
                    text(
                        "SELECT state, worker_id, attempt, lease_expires_at, "
                        "       now() AS database_now "
                        "  FROM durable_jobs WHERE job_id = :j"
                    ),
                    {"j": job_id},
                )
                .mappings()
                .one()
            )
            return dict(row)


def _ctx(label: str) -> _Ctx:
    return _Ctx(
        tenant_id=uuid.UUID(tenant(f"tenant-lease-clock-{label}-{_RUN}")),
        subject=f"lead-{label}",
    )


def _run_the_clock_fast(monkeypatch: pytest.MonkeyPatch, ahead: timedelta) -> None:
    """Make THIS process believe it is `ahead` of real time.

    Patches the module-level clock rather than passing `now=`, so the call sites
    under test stay byte-identical to `worker.tick`'s.
    """
    from application.services import jobs as svc

    monkeypatch.setattr(svc, "_now", lambda: datetime.now(UTC) + ahead)


def test_a_reaper_ten_minutes_fast_does_not_reap_a_live_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, reproduced end to end. Before the fix this reaped the job."""
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("skewed-reaper")
    job_id = ctx.enqueue(ctx.review("A live lease under a fast clock"))

    with actor_scope(ctx.actor()) as session:
        claimed = svc.claim_next(session, worker_id="worker-holding-it", tenant_id=ctx.tenant_id)
        assert claimed is not None and claimed.job_id == job_id

    before = ctx.state(job_id)
    assert before["state"] == "running"
    assert before["lease_expires_at"] > before["database_now"], (
        "precondition: the database itself must consider this lease live, otherwise the "
        "assertion below would pass for the wrong reason"
    )

    _run_the_clock_fast(monkeypatch, SKEW)

    # Exactly the call `worker.tick` makes: no `now=`, no arguments at all.
    with actor_scope(ctx.actor()) as session:
        reaped = svc.reap_expired_leases(session)

    assert job_id not in reaped, (
        "a reaper whose process clock ran fast took a lease the DATABASE still considers live; "
        "the job returns to `queued` and a second pod claims work the first is still running"
    )

    after = ctx.state(job_id)
    assert after["state"] == "running"
    assert after["worker_id"] == "worker-holding-it"
    assert after["lease_expires_at"] is not None


def test_the_same_fast_reaper_still_reaps_a_genuinely_expired_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the test above: the reaper is not merely inert.

    Without this, "the live lease survived" would be satisfied by a reaper that
    had stopped working altogether — and the fix under test is precisely a
    change to what the reaper's WHERE clause compares.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("expired-under-skew")
    job_id = ctx.enqueue(ctx.review("A dead lease under the same fast clock"))

    with actor_scope(ctx.actor()) as session:
        claimed = svc.claim_next(
            session,
            worker_id="worker-that-died",
            tenant_id=ctx.tenant_id,
            lease_seconds=1,
        )
        assert claimed is not None and claimed.job_id == job_id

    # Real time, not simulated: the lease has to be expired in the DATABASE,
    # which is the whole point of the fix.
    time.sleep(1.5)

    _run_the_clock_fast(monkeypatch, SKEW)

    with actor_scope(ctx.actor()) as session:
        reaped = svc.reap_expired_leases(session)

    assert job_id in reaped
    after = ctx.state(job_id)
    assert after["state"] == "queued"
    assert after["worker_id"] is None
    assert after["lease_expires_at"] is None


def test_a_claimant_ten_minutes_fast_writes_a_database_clocked_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lease ISSUE is database-clocked too, and it has to be.

    Fixing only the reaper moves the defect rather than closing it: a claimant
    ten minutes fast would stamp `lease_expires_at` ten minutes into the future,
    and an honest reaper would then refuse to reclaim a genuinely dead worker's
    job for ten extra minutes. `13-…:§7.4`'s recovery would silently acquire a
    ten-minute floor that nothing measures.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("skewed-claimant")
    job_id = ctx.enqueue(ctx.review("A lease issued under a fast clock"))

    _run_the_clock_fast(monkeypatch, SKEW)

    with actor_scope(ctx.actor()) as session:
        claimed = svc.claim_next(
            session,
            worker_id="worker-with-a-bad-clock",
            tenant_id=ctx.tenant_id,
            lease_seconds=60,
        )
        assert claimed is not None and claimed.job_id == job_id

    row = ctx.state(job_id)
    ahead = row["lease_expires_at"] - row["database_now"]

    # Generous both ways: the point is the ORDER OF MAGNITUDE. A pod-clocked
    # lease sits ~10 minutes + 60s ahead; a database-clocked one sits ~60s ahead
    # minus however long this assertion took to reach.
    assert ahead < timedelta(minutes=5), (
        f"lease_expires_at is {ahead} ahead of the database's own now(); the claiming process's "
        "skewed wall clock was written into the row, so a dead worker's job cannot be reclaimed "
        "until the skew has also elapsed"
    )
    assert ahead > timedelta(seconds=30), (
        f"lease_expires_at is only {ahead} ahead of the database's now(); the lease is far shorter "
        "than the 60s requested, which would let a reaper take a job off a healthy worker"
    )
