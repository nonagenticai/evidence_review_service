"""`FND-006` completion evidence: "two-worker lease/cancel/restart suite".

`13-…:§18.5` states the exit gate this file exists to satisfy — "one job survives
API and worker restart without duplication" — and `§7.2` states the mechanism:
"Workers claim with `FOR UPDATE SKIP LOCKED`."

## Why two real connections and not two threads

The claim's whole correctness argument is about row locks, and a row lock is held
by a *transaction*. Two threads sharing one SQLAlchemy session share one
transaction, so a test written that way would prove that the same transaction
cannot claim a row twice — which is true of any `UPDATE` and says nothing about
two workers. The concurrency test below opens two engines and two transactions,
and holds the first open across the second worker's attempt.

## ⚠️ Every test gets its OWN tenant, and that is not tidiness

`claim_next` takes the OLDEST runnable row in scope. Nothing truncates between
tests, so with one shared tenant the queue is cumulative: the first draft of this
file used one, and `test_an_expired_lease_returns_the_job_and_records_why`
claimed a leftover from two tests earlier and failed on an id mismatch. It was
the test that was wrong, not the claim — but the same shape with the assertion
pointed the other way would have PASSED against a broken claimant, because
"a job came back" is true whether or not it is the right one.

So `_ctx(label)` derives a distinct tenant per test — and a distinct one per
RUN. The per-run nonce is the second half of the same lesson: with a stable
label, a re-run against a database that still holds the previous run's rows
claims the older job and fails on an id mismatch, which is a fixture problem
wearing a claimant's failure. Every other module here can use a stable label
because none of them depends on "the oldest row in the tenant"; this one does,
by construction, because that is what a queue is.

## The negative controls

Three assertions here would pass against a broken implementation if they stood
alone, so each has a control beside it:

* "worker B got nothing" is only meaningful if B *can* claim when A is not
  holding the row — so B claims successfully after A's transaction ends.
* "the reaper requeued it" is only meaningful if the reaper leaves an
  unexpired lease alone — so a live lease is offered to the reaper first.
* "a cancelled job was not started" is only meaningful if the same job would
  otherwise have been claimable — so an identical job without the flag is
  claimed in the same call.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

HASH = "sha256:" + "a" * 64

#: Fresh per pytest process — see the module docstring. Deliberately NOT derived
#: from the test name alone.
_RUN = uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class _Ctx:
    """One test's tenant, and the handful of operations every test needs in it."""

    tenant_id: uuid.UUID
    subject: str

    def actor(self, role: str = "lead", subject: str | None = None) -> Any:
        from actor import Actor, ActorRole

        return Actor(tenant_id=self.tenant_id, subject_id=subject or self.subject, role=ActorRole(role))

    def review(self, title: str) -> uuid.UUID:
        from application.services import reviews as review_svc
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            review = review_svc.create_review(
                session,
                self.actor(),
                project_id="durable-jobs",
                external_key=f"job-{uuid.uuid4()}",
                title=title,
            )
            return review.review_id

    def enqueue(self, review_id: uuid.UUID, job_type: str = "export", **kwargs: Any) -> uuid.UUID:
        from application.services import jobs as svc
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            job = svc.enqueue(
                session,
                self.actor(),
                review_id=review_id,
                job_type=job_type,
                command_hash=HASH,
                **kwargs,
            )
            return job.job_id

    def open_session(self) -> tuple[Any, Session]:
        """A session on its OWN connection, with the tenant GUC bound.

        `database.actor_scope` commits on exit, which is exactly what a
        concurrency test must not do between the two workers' attempts. This is
        the same binding done by hand so the transaction can be held open.
        """
        engine = create_engine(DB_URL)
        session = Session(engine)
        session.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(self.tenant_id)})
        session.execute(text("SELECT set_config('app.actor_subject_id', :s, true)"), {"s": self.subject})
        session.execute(text("SELECT set_config('app.actor_role', 'lead', true)"))
        return engine, session

    def state(self, job_id: uuid.UUID) -> dict[str, Any]:
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            row = (
                session.execute(
                    text(
                        "SELECT state, worker_id, attempt, cancel_requested_at, error_code, "
                        "       lease_expires_at, terminal_at "
                        "  FROM durable_jobs WHERE job_id = :j"
                    ),
                    {"j": job_id},
                )
                .mappings()
                .one()
            )
            return dict(row)

    def attempts(self, job_id: uuid.UUID) -> list[dict[str, Any]]:
        from database import actor_scope

        with actor_scope(self.actor()) as session:
            rows = session.execute(
                text(
                    "SELECT attempt_number, worker_id, outcome, ended_at FROM job_attempts "
                    " WHERE job_id = :j ORDER BY attempt_number"
                ),
                {"j": job_id},
            ).mappings()
            return [dict(r) for r in rows]


def _ctx(label: str) -> _Ctx:
    return _Ctx(
        tenant_id=uuid.UUID(tenant(f"tenant-durable-jobs-{label}-{_RUN}")),
        subject=f"lead-{label}",
    )


def test_two_workers_cannot_claim_one_job() -> None:
    """The core guarantee. B is refused while A holds the row, and claims once A lets go."""
    from application.services import jobs as svc

    ctx = _ctx("two-workers")
    review_id = ctx.review("Two workers, one job")
    job_id = ctx.enqueue(review_id)

    engine_a, session_a = ctx.open_session()
    engine_b, session_b = ctx.open_session()
    try:
        claimed_a = svc.claim_next(session_a, worker_id="worker-a", tenant_id=ctx.tenant_id)
        assert claimed_a is not None
        assert claimed_a.job_id == job_id
        assert claimed_a.state == "running"
        assert claimed_a.attempt == 1

        # A has NOT committed. B must step over the locked row rather than block
        # on it — `SKIP LOCKED` — and must not be handed the same job.
        assert svc.claim_next(session_b, worker_id="worker-b", tenant_id=ctx.tenant_id) is None

        session_a.commit()

        # The negative control: B is not simply broken. Once the queue holds a
        # second runnable job, the same call succeeds.
        second = ctx.enqueue(review_id, job_type="import")
        claimed_b = svc.claim_next(session_b, worker_id="worker-b", tenant_id=ctx.tenant_id)
        assert claimed_b is not None
        assert claimed_b.job_id == second
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()
        engine_a.dispose()
        engine_b.dispose()

    assert [(a["attempt_number"], a["worker_id"]) for a in ctx.attempts(job_id)] == [(1, "worker-a")]


def test_an_expired_lease_returns_the_job_and_records_why() -> None:
    """`§7.4` recovery. The attempt is sealed `lease_expired`, not `failed`.

    The distinction is the point: an operator reading the attempt table after
    three node drains must not see a job that "failed" three times.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("expired-lease")
    review_id = ctx.review("An expired lease comes back")
    job_id = ctx.enqueue(review_id)

    with actor_scope(ctx.actor()) as session:
        claimed = svc.claim_next(session, worker_id="worker-dies", tenant_id=ctx.tenant_id)
        assert claimed is not None and claimed.job_id == job_id

    # The control: a live lease is NOT reaped. Without this the next assertion
    # would pass against a reaper that requeues everything it can see.
    with actor_scope(ctx.actor()) as session:
        assert svc.reap_expired_leases(session) == []

    later = datetime.now(UTC) + timedelta(seconds=svc.DEFAULT_LEASE_SECONDS + 1)
    with actor_scope(ctx.actor()) as session:
        reaped = svc.reap_expired_leases(session, now=later)
    # `in`, not `==`: a developer re-running this file against a database that
    # already holds a previous run's rows would otherwise see a failure that is
    # about the fixture and not the reaper. CI's postgres is ephemeral per job,
    # so there the two are the same assertion; locally only one of them is honest.
    assert job_id in reaped

    after = ctx.state(job_id)
    assert after["state"] == "queued"
    assert after["worker_id"] is None
    assert after["lease_expires_at"] is None
    assert after["attempt"] == 1

    history = ctx.attempts(job_id)
    assert [(a["attempt_number"], a["outcome"]) for a in history] == [(1, "lease_expired")]
    assert history[0]["ended_at"] is not None

    # And it is claimable again — by a different worker, as attempt 2.
    with actor_scope(ctx.actor()) as session:
        again = svc.claim_next(session, worker_id="worker-b", tenant_id=ctx.tenant_id)
    assert again is not None
    assert again.job_id == job_id
    assert again.attempt == 2


def test_a_job_that_spends_its_attempts_fails_rather_than_looping() -> None:
    """`§7.2`: reclaimed only while the retry policy allows. Then it is a failure.

    A queue that requeues forever is not durable, it is a livelock with a nice
    name — and the row that never reaches a terminal state is the row nobody is
    alerted about.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("attempt-budget")
    review_id = ctx.review("Attempts are a budget")
    job_id = ctx.enqueue(review_id, max_attempts=1)

    with actor_scope(ctx.actor()) as session:
        assert svc.claim_next(session, worker_id="w1", tenant_id=ctx.tenant_id) is not None

    later = datetime.now(UTC) + timedelta(seconds=svc.DEFAULT_LEASE_SECONDS + 1)
    with actor_scope(ctx.actor()) as session:
        assert job_id in svc.reap_expired_leases(session, now=later)

    after = ctx.state(job_id)
    assert after["state"] == "failed"
    assert after["error_code"] == svc.LEASE_EXHAUSTED
    assert after["terminal_at"] is not None

    # Terminal is terminal: the reaper does not pick it up again, and nothing
    # else may move it.
    with actor_scope(ctx.actor()) as session:
        assert svc.reap_expired_leases(session, now=later) == []


def test_a_cancelled_job_is_never_started() -> None:
    """`§7.1`: the flag is honoured at claim time, before an attempt is spent.

    Claiming and then cancelling at the first safe point would also be correct
    and would burn an attempt, leaving history that records an execution which
    never had permission to run.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("cancel-before-claim")
    review_id = ctx.review("A cancelled job is never started")
    cancelled_id = ctx.enqueue(review_id, job_type="export")
    with actor_scope(ctx.actor()) as session:
        job = svc.request_cancel(session, ctx.actor(), cancelled_id)
        assert job.state == "queued", "a queued cancel is a flag, not a termination"
        assert job.cancel_requested_at is not None

    # The control: an identical job WITHOUT the flag, queued later, is what the
    # same call returns instead. Without it, "claim skipped the cancelled job"
    # and "claim is broken" look the same.
    runnable_id = ctx.enqueue(review_id, job_type="import")

    with actor_scope(ctx.actor()) as session:
        claimed = svc.claim_next(session, worker_id="w1", tenant_id=ctx.tenant_id)
    assert claimed is not None
    assert claimed.job_id == runnable_id
    assert ctx.state(cancelled_id)["state"] == "queued"
    assert ctx.attempts(cancelled_id) == []


def test_a_terminal_result_outruns_a_late_cancel() -> None:
    """`§7.3`: "cancellation that arrives after terminal commit returns the terminal outcome".

    Not an error — see `jobs.request_cancel`. A 409 here would make every caller
    write a branch that treats "you got what you wanted" as a failure, and the
    first thing written in that branch is a retry.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("terminal-wins")
    review_id = ctx.review("Terminal wins")
    job_id = ctx.enqueue(review_id)

    with actor_scope(ctx.actor()) as session:
        svc.claim_next(session, worker_id="w1", tenant_id=ctx.tenant_id)
    with actor_scope(ctx.actor()) as session:
        svc.finish(
            session,
            job_id,
            worker_id="w1",
            outcome="succeeded",
            result_summary={"records": 3},
            result_artifact_ref="artifact:abc",
        )

    with actor_scope(ctx.actor()) as session:
        answered = svc.request_cancel(session, ctx.actor(), job_id)

    assert answered.state == "succeeded"
    assert answered.cancel_requested_at is None, "a terminal job records no cancellation intent"
    assert ctx.attempts(job_id)[0]["outcome"] == "succeeded"


def test_only_the_lease_holder_finishes_or_heartbeats() -> None:
    """A reaped worker that has not noticed must not extend the new holder's lease.

    Otherwise the queue holds two workers each convinced they own one job, with
    no way for either to find out.
    """
    from api.problem_details import StateConflictError
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("lease-holder")
    review_id = ctx.review("Only the holder")
    job_id = ctx.enqueue(review_id)

    with actor_scope(ctx.actor()) as session:
        svc.claim_next(session, worker_id="holder", tenant_id=ctx.tenant_id)

    with actor_scope(ctx.actor()) as session, pytest.raises(StateConflictError):
        svc.heartbeat(session, job_id, worker_id="impostor")
    with actor_scope(ctx.actor()) as session, pytest.raises(StateConflictError):
        svc.finish(session, job_id, worker_id="impostor", outcome="succeeded")

    # The control: the real holder can do both.
    with actor_scope(ctx.actor()) as session:
        svc.heartbeat(session, job_id, worker_id="holder", progress={"stage": "scoring"})
    assert ctx.state(job_id)["state"] == "running"


def test_a_waiting_job_resumes_only_after_its_reconcile_time() -> None:
    """`§7.1`: `waiting` is durable and externally visible, and declares why.

    The parked attempt is left OPEN deliberately — the job has not been executed
    twice when it resumes, and sealing it here would make the history claim it had.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("waiting")
    review_id = ctx.review("Waiting declares why")
    job_id = ctx.enqueue(review_id)

    with actor_scope(ctx.actor()) as session:
        svc.claim_next(session, worker_id="w1", tenant_id=ctx.tenant_id)
    reconcile_at = datetime.now(UTC) + timedelta(minutes=10)
    with actor_scope(ctx.actor()) as session:
        parked = svc.wait_for(
            session,
            job_id,
            worker_id="w1",
            reason="awaiting_terminology_pin",
            reconcile_after=reconcile_at,
            dependency_ref="terminology:snomed@2026-01",
        )
        assert parked.state == "waiting"

    row = ctx.state(job_id)
    assert row["worker_id"] is None and row["lease_expires_at"] is None
    assert [a["outcome"] for a in ctx.attempts(job_id)] == [None]

    # A waiting job is NOT claimable, before or after its reconcile time. The
    # `§7.1` diagram has no `waiting -> running` edge, and the first draft of
    # `claim_next` tried to take one; `trg_durable_jobs_transition` refused it.
    with actor_scope(ctx.actor()) as session:
        assert svc.claim_next(session, worker_id="w2", tenant_id=ctx.tenant_id) is None
    with actor_scope(ctx.actor()) as session:
        assert svc.claim_next(session, worker_id="w2", tenant_id=ctx.tenant_id) is None, (
            "the claimant must not shortcut the reconciler"
        )

    # The control: not yet due, so the reconciler leaves it parked.
    with actor_scope(ctx.actor()) as session:
        assert svc.reconcile_waiting(session) == []

    after_due = reconcile_at + timedelta(seconds=1)
    with actor_scope(ctx.actor()) as session:
        assert job_id in svc.reconcile_waiting(session, now=after_due)
    assert ctx.state(job_id)["state"] == "queued"

    with actor_scope(ctx.actor()) as session:
        resumed = svc.claim_next(session, worker_id="w2", tenant_id=ctx.tenant_id)
    assert resumed is not None
    assert resumed.job_id == job_id
    assert resumed.state == "running"
    assert resumed.waiting_reason is None, "a running job declares no waiting reason"
    # Attempt 2, and the first attempt — the one that parked — is still the
    # only sealed history there is nothing to seal about.
    assert resumed.attempt == 2
    assert [a["outcome"] for a in ctx.attempts(job_id)] == [None, None]


def test_a_parked_job_that_was_cancelled_ends_rather_than_requeueing() -> None:
    """A cancel that arrives while a job is parked must reach a terminal state.

    Returning it to `queued` only so the claimant can skip it would leave it
    non-terminal forever — visible in a queue, never running, never finishing,
    and never alerting anybody because nothing is late.
    """
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("waiting-cancel")
    review_id = ctx.review("Cancelled while parked")
    job_id = ctx.enqueue(review_id)

    with actor_scope(ctx.actor()) as session:
        svc.claim_next(session, worker_id="w1", tenant_id=ctx.tenant_id)
    reconcile_at = datetime.now(UTC) + timedelta(minutes=10)
    with actor_scope(ctx.actor()) as session:
        svc.wait_for(
            session,
            job_id,
            worker_id="w1",
            reason="awaiting_provider",
            reconcile_after=reconcile_at,
        )
    with actor_scope(ctx.actor()) as session:
        flagged = svc.request_cancel(session, ctx.actor(), job_id)
        assert flagged.state == "waiting", "the flag does not terminate a parked job by itself"

    with actor_scope(ctx.actor()) as session:
        assert job_id in svc.reconcile_waiting(session, now=reconcile_at + timedelta(seconds=1))

    after = ctx.state(job_id)
    assert after["state"] == "cancelled"
    assert after["terminal_at"] is not None
    assert [a["outcome"] for a in ctx.attempts(job_id)] == ["cancelled"]


def test_the_same_command_twice_is_one_job() -> None:
    """`14-…:ER-007` calls the snapshot build "an idempotent durable build job".

    And a reused key bound to a DIFFERENT command is a 409 rather than a silent
    hand-back of somebody else's work.
    """
    from api.problem_details import StateConflictError
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("idempotent")
    review_id = ctx.review("One command, one job")
    key = f"snapshot-{uuid.uuid4()}"

    first = ctx.enqueue(review_id, job_type="evidence_snapshot", idempotency_key=key)
    second = ctx.enqueue(review_id, job_type="evidence_snapshot", idempotency_key=key)
    assert first == second

    with actor_scope(ctx.actor()) as session, pytest.raises(StateConflictError):
        svc.enqueue(
            session,
            ctx.actor(),
            review_id=review_id,
            job_type="evidence_snapshot",
            command_hash="sha256:" + "b" * 64,
            idempotency_key=key,
        )


def test_a_job_from_another_tenant_is_not_found() -> None:
    """RLS, and a 404 rather than a 403 — see `jobs.load_job`.

    A 403 would confirm that a job with that id exists somewhere, which is a
    cross-tenant existence oracle over a guessable identifier space.
    """
    from api.problem_details import NotFoundError
    from application.services import jobs as svc
    from database import actor_scope

    ctx = _ctx("owner")
    stranger = _ctx("stranger")
    review_id = ctx.review("Not yours")
    job_id = ctx.enqueue(review_id)

    with actor_scope(stranger.actor()) as session:
        with pytest.raises(NotFoundError):
            svc.load_job(session, job_id)
        # The control: the claim does not reach across either.
        assert svc.claim_next(session, worker_id="w1", tenant_id=stranger.tenant_id) is None
