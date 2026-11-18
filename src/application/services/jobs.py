"""The durable job subsystem: enqueue, claim, lease, reap, cancel.

`FND-006` (`13-…:§7`). `ER-M020` created `durable_jobs` and `job_attempts`; this
module is the only code that moves a row between states, and every function in
it takes the caller's `Session` and neither commits nor flushes on its own —
`13-…:§5.3` gives one application command exactly one transaction, and a service
that commits inside it takes that guarantee away from every caller.

## What is enforced here and what is enforced in the database

Almost nothing here is the control. The legal transitions, terminal
immutability, the six states, "running implies a lease", "waiting declares why"
and the payload bound are all triggers and CHECKs in `0020`, because the thing
that violates them is a worker — the process that is by definition partitioned,
crashed, or running an older image than the row beside it. What this module adds
is a *callsite* failure with the job id in the message, several frames closer to
the mistake than an integrity error at commit.

The one exception, and it is the important one:

## The claim is a single statement, and that is the whole design

`claim_next` is one `UPDATE … WHERE job_id IN (SELECT … FOR UPDATE SKIP LOCKED
LIMIT 1) RETURNING *`. Two workers running it concurrently cannot both get the
same row: the inner select takes a row lock, `SKIP LOCKED` makes the loser step
over it rather than block on it, and because the select and the update are the
same statement there is no window between "I chose this row" and "I own it".

Read-then-update in Python would have that window, and the failure it produces
is not a crash — it is two workers running the same scientific job, each
believing it holds the lease, both writing a terminal result. The second write
is refused by the terminal trigger, so the visible symptom is a spurious error
on a job that ran twice and paid for it twice. That is the exact defect
`13-…:§18.5`'s exit gate names: "one job survives API and worker restart
without duplication".

## Cooperative cancellation, and why a terminal job's cancel is a 200

`13-…:§7.3` — "cancellation that arrives after terminal commit returns the
terminal outcome". `request_cancel` on a finished job is therefore NOT an error.
It answers with the job as it stands, because the caller asked for the work to
stop and the work has stopped; reporting a 409 would make every caller write a
branch that treats "you got what you wanted" as a failure, and the first thing
written in that branch is a retry.

The flag is monotonic — `0020`'s trigger refuses to unset it — so a race between
a cancel and a terminal commit has exactly two outcomes and both are correct:
the terminal write lands first and the cancel reads it back, or the flag lands
first and the worker applies it at its next safe point.

## ⛔ Nothing calls `claim_next` on a loop

There is no worker process. These are library functions with an integration
suite behind them; the deployment that runs them is not part of `FND-006` and
each producer package (`ER-002/003/007/008/009`) lands its own entrypoint with
its conversion. `job_catalog.OWED_BY` records who owes what. Enqueuing a job
today means the row is durable, readable and cancellable — not that anything
will execute it, and `README.md` says so beside the endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from actor import Actor
from api.problem_details import NotFoundError, StateConflictError, ValidationFailedError
from domain.policies import job_catalog
from log_config import get_logger
from models.foundation import DurableJob, JobAttempt

logger = get_logger(__name__)

#: How long a claim holds a job before a reaper may take it back. Chosen to be
#: comfortably longer than a heartbeat interval and comfortably shorter than a
#: human's patience: a worker that dies silently must not hold a queue for an
#: hour, and one that is merely slow must not lose its lease mid-write.
DEFAULT_LEASE_SECONDS = 120

#: `13-…:§7.2` — "Heartbeats extend a BOUNDED lease". Unbounded extension is how
#: a wedged worker holds a job forever while looking healthy, which is the shape
#: `a-heartbeat-makes-a-standstill-look-healthy` describes.
MAX_LEASE_SECONDS = 900

#: `13-…:§7.2` — "Expired work is reclaimed only when the method's retry policy
#: says execution is safe."
#:
#: All five registered types compute over inputs pinned by `input_snapshot_hash`
#: and write only into this database, so re-executing one cannot double an
#: external effect. That is a property of the types that exist TODAY, not a
#: property of jobs, which is why this is an explicit allowlist rather than a
#: default: the first type that dispatches to a non-repeatable external provider
#: must be added deliberately, and `13-…:§7.2` says what it must do first —
#: "reconcile by idempotency key before retry".
#:
#: A type absent from here whose lease expires is failed, not requeued.
RECLAIMABLE_ON_LEASE_EXPIRY: frozenset[str] = frozenset(job_catalog.REGISTERED_JOB_TYPES)

#: The error code a job carries when its retry budget is gone. Stable, because a
#: caller distinguishing "this job failed" from "this job was never finished by
#: anybody" is the difference between a scientific result and an operational one.
LEASE_EXHAUSTED = "lease_exhausted"


def _now() -> datetime:
    return datetime.now(UTC)


def _db_now(session: Session) -> datetime:
    """The DATABASE's clock — the only one the lease lifecycle is allowed to read.

    ## The defect this exists to close

    A lease is a promise between two processes that do not share a clock. The
    holder writes `lease_expires_at` from ITS wall clock; the reaper compares
    that column against ITS wall clock; and until this function existed both
    clocks were `datetime.now(UTC)` inside whichever pod happened to be running.

    Measured: a reaper whose host clock ran ten minutes fast reaped a lease that
    Postgres still considered live (`lease_expires_at > now()`). The job went
    back to `queued`, a second pod claimed it, and the first pod was still
    executing it — which is precisely the duplication `13-…:§7.2`'s
    `FOR UPDATE SKIP LOCKED` claim and this lease exist to prevent. Nothing
    raised; the reaper's own log said it had recovered a stranded job.

    NTP is not the answer to this. Clock skew between nodes is a normal,
    recurring, unalarmed condition, and a correctness property must not rest on
    an operational one. The database is already the single serialization point
    for every other part of the queue, so it is also the clock: the lease is
    ISSUED from `now()` and EXPIRED against `now()`, and the two comparands then
    come from the same source no matter which pod ran which half.

    `now()` rather than `clock_timestamp()`: `now()` is transaction-start time,
    so every timestamp a single reap or claim pass writes is identical and the
    value returned here is exactly the value a later `now()` in the same
    transaction would produce. A reaper must not see time advance between its
    own SELECT and its own UPDATE.

    ⚠️ The explicit `now=` argument on the callers below survives as a TEST
    SEAM and nothing else. Every production call site — `worker.tick` is the
    only one — passes nothing and is therefore database-clocked. A caller that
    passes `now=` is declaring a simulated clock on purpose; a pod that merely
    has a wrong clock cannot reach that path.
    """
    return session.execute(text("SELECT now()")).scalar_one()


def _require_registered(job_type: str) -> None:
    """A convenience refusal, not the control — `ck_durable_jobs_registered_type` is.

    `raise` rather than `assert`, for the reason `events._write` gives: an
    assertion is stripped under `python -O`, so the check would be present in
    development and absent in production, which is the one arrangement in which
    it can mislead.
    """
    if job_type not in job_catalog.REGISTERED_JOB_TYPES:
        raise ValidationFailedError(
            f"'{job_type}' is not a registered job type; the registry is {', '.join(job_catalog.REGISTERED_JOB_TYPES)}"
        )


def enqueue(
    session: Session,
    actor: Actor,
    *,
    review_id: uuid.UUID,
    job_type: str,
    command_hash: str,
    input_snapshot_hash: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    profile_version: str | None = None,
    capability_version: str | None = None,
    pack_refs: list[str] | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    workflow_ref: str | None = None,
) -> DurableJob:
    """Record a job to be executed later, once, for this command.

    Idempotent on `(tenant, review, job_type, idempotency_key)`. A resubmission
    with the same key returns the EXISTING job rather than creating a second
    one, which is what makes `POST …/evidence-snapshots` "idempotent"
    (`14-…:ER-007`) rather than a second run of a scientific build.

    A resubmission that reuses a key with a DIFFERENT command is refused. Both
    other answers are worse: returning the old job silently gives the caller a
    handle to work they did not ask for, and creating a new one makes the key
    meaningless. `409` is the honest report that the key is already spoken for.

    The originating actor is recorded as an identity REFERENCE. `13-…:§9.3` — a
    job "never retains a human credential" — and there is deliberately no column
    it could go in.
    """
    _require_registered(job_type)

    if idempotency_key is not None:
        existing = session.execute(
            select(DurableJob).where(
                DurableJob.tenant_id == actor.tenant_id,
                DurableJob.review_id == review_id,
                DurableJob.job_type == job_type,
                DurableJob.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.command_hash != command_hash:
                raise StateConflictError(
                    f"idempotency key is already bound to job {existing.job_id} with a different command"
                )
            return existing

    job = DurableJob(
        job_id=uuid.uuid4(),
        tenant_id=actor.tenant_id,
        review_id=review_id,
        job_type=job_type,
        state="queued",
        command_hash=command_hash,
        input_snapshot_hash=input_snapshot_hash,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        profile_version=profile_version,
        capability_version=capability_version,
        pack_refs=pack_refs or [],
        correlation_id=correlation_id,
        causation_id=causation_id,
        workflow_ref=workflow_ref,
        originating_subject_id=actor.subject_id,
        originating_actor_role=str(actor.role),
        queued_at=_now(),
    )
    session.add(job)
    session.flush()
    return job


def claim_next(
    session: Session,
    *,
    worker_id: str,
    job_types: tuple[str, ...] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    tenant_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> DurableJob | None:
    """Take the oldest runnable job, atomically. `None` when the queue is empty.

    One statement — see the module docstring. The inner select is the only place
    in this service that uses `FOR UPDATE SKIP LOCKED`, and it is what
    `13-…:§7.2` requires.

    `tenant_id` is optional and the two callers are different animals. A
    cross-tenant worker passes `None` and needs the additive
    `evidence_review_worker` policy `0020` installs, then **derives** the tenant
    from the row it got and rebinds before touching any domain table
    (`13-…:§5.1`). A tenant-bound caller — the test suite, and any future
    per-tenant runner — passes its own tenant and is governed by the ordinary
    RLS policy, which is why this works at all in a database with no worker role
    provisioned.

    ⚠️ Only `queued` rows are claimable. A `waiting` job whose `reconcile_after`
    has passed is NOT claimed directly — `13-…:§7.1` draws `waiting -> queued`
    and `queued -> running`, with no edge between the two ends, and
    `reconcile_waiting` is the step in between.

    The first draft of this function did claim `waiting` rows straight to
    `running`, and `trg_durable_jobs_transition` refused it. That refusal is the
    whole argument for putting the transition table in the database: the
    shortcut is invisible in a diff, it would have worked in every test that did
    not park a job, and what it skips is the reconciler's duty to
    **reauthorize** — `§7.1` requires the resource and policy to be re-checked
    before a parked job runs, and a claim that jumps the queue never asks.
    """
    # The DATABASE's clock, not this pod's. See `_db_now`: an explicit `now=`
    # is a declared test seam; the default path can never read a skewed host clock.
    at = now if now is not None else _db_now(session)
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        raise ValidationFailedError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")

    types = tuple(job_types or job_catalog.REGISTERED_JOB_TYPES)
    for job_type in types:
        _require_registered(job_type)

    params: dict[str, Any] = {
        "worker_id": worker_id,
        "now": at,
        "lease": at + timedelta(seconds=lease_seconds),
        "types": list(types),
    }
    tenant_clause = ""
    if tenant_id is not None:
        tenant_clause = "AND tenant_id = :tenant_id"
        params["tenant_id"] = tenant_id

    # `cancel_requested_at IS NULL` in the candidate predicate: a job whose
    # cancellation was requested before anybody claimed it must not be started
    # at all. Claiming it and cancelling it at the first safe point would be
    # correct and would also burn an attempt, and the attempt history would then
    # record an execution that never had permission to run.
    claimed = session.execute(
        text(
            f"""
            UPDATE durable_jobs
               SET state = 'running',
                   worker_id = :worker_id,
                   lease_expires_at = :lease,
                   heartbeat_at = :now,
                   started_at = COALESCE(started_at, :now),
                   attempt = attempt + 1
             WHERE job_id = (
                     SELECT job_id
                       FROM durable_jobs
                      WHERE state = 'queued'
                        AND cancel_requested_at IS NULL
                        AND attempt < max_attempts
                        AND job_type = ANY(:types)
                        {tenant_clause}
                      ORDER BY queued_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                   )
            RETURNING job_id
            """  # noqa: S608 - `tenant_clause` is a constant string, its value is bound
        ),
        params,
    ).scalar_one_or_none()

    if claimed is None:
        return None

    job = session.get(DurableJob, claimed)
    if job is None:  # pragma: no cover - the UPDATE above returned this id
        return None
    session.refresh(job)

    session.add(
        JobAttempt(
            attempt_id=uuid.uuid4(),
            job_id=job.job_id,
            tenant_id=job.tenant_id,
            attempt_number=job.attempt,
            worker_id=worker_id,
            capability_version=job.capability_version,
            started_at=at,
        )
    )
    session.flush()
    logger.info(
        "job.claimed",
        job_id=str(job.job_id),
        job_type=job.job_type,
        worker_id=worker_id,
        attempt=job.attempt,
    )
    return job


def claim(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> DurableJob:
    """Claim ONE NAMED job. The targeted sibling of :func:`claim_next`.

    `claim_next` answers "give me work"; this answers "I am about to run THIS
    job". They are not interchangeable, and confusing them is a real defect
    rather than a style question: `snapshots.request_snapshot` first called
    `claim_next` immediately after enqueuing, on the assumption that the job it
    had just created was the one it would get back. It is not. `claim_next`
    takes the OLDEST runnable row in the tenant, so a second snapshot request
    arriving while an earlier one was still queued would have claimed the
    earlier job and then failed in `finish`, which refuses a caller that is not
    the lease holder — under concurrency only, and never in a single-request
    test.

    Same statement shape as `claim_next`, so the exclusivity argument is
    unchanged: one `UPDATE` with `FOR UPDATE SKIP LOCKED` on the row, and a
    caller that loses the race gets a refusal rather than a second lease.
    """
    # The DATABASE's clock, not this pod's. See `_db_now`: an explicit `now=`
    # is a declared test seam; the default path can never read a skewed host clock.
    at = now if now is not None else _db_now(session)
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        raise ValidationFailedError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")

    claimed = session.execute(
        text(
            """
            UPDATE durable_jobs
               SET state = 'running',
                   worker_id = :worker_id,
                   lease_expires_at = :lease,
                   heartbeat_at = :now,
                   started_at = COALESCE(started_at, :now),
                   attempt = attempt + 1
             WHERE job_id = (
                     SELECT job_id FROM durable_jobs
                      WHERE job_id = :job_id
                        AND state = 'queued'
                        AND cancel_requested_at IS NULL
                        AND attempt < max_attempts
                      FOR UPDATE SKIP LOCKED
                   )
            RETURNING job_id
            """
        ),
        {"worker_id": worker_id, "now": at, "lease": at + timedelta(seconds=lease_seconds), "job_id": job_id},
    ).scalar_one_or_none()

    if claimed is None:
        job = load_job(session, job_id)
        raise StateConflictError(
            f"job {job_id} is {job.state} and cannot be claimed"
            + (" (a cancellation was requested)" if job.cancel_requested_at else "")
        )

    job = session.get(DurableJob, claimed)
    if job is None:  # pragma: no cover - the UPDATE above returned this id
        raise NotFoundError(f"job {job_id} was not found")
    session.refresh(job)
    session.add(
        JobAttempt(
            attempt_id=uuid.uuid4(),
            job_id=job.job_id,
            tenant_id=job.tenant_id,
            attempt_number=job.attempt,
            worker_id=worker_id,
            capability_version=job.capability_version,
            started_at=at,
        )
    )
    session.flush()
    logger.info("job.claimed", job_id=str(job.job_id), job_type=job.job_type, worker_id=worker_id, attempt=job.attempt)
    return job


def heartbeat(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    progress: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DurableJob:
    """Extend a bounded lease, and optionally publish bounded progress.

    Refuses when the caller is not the lease holder. That is not paranoia about
    malice: a worker whose lease was reaped and reassigned will keep
    heartbeating until it notices, and if that extended the NEW holder's lease
    the queue would have two workers convinced they own one job and no way for
    either to find out.

    `progress` is bounded by `ck_durable_jobs_progress_is_minimal` — the same
    recursive 512-character guard `0019` installed for event payloads — so a
    worker cannot use it to stream extracted text out of a licensed source.
    """
    # The DATABASE's clock, not this pod's. See `_db_now`: an explicit `now=`
    # is a declared test seam; the default path can never read a skewed host clock.
    at = now if now is not None else _db_now(session)
    if lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        raise ValidationFailedError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")

    job = load_job(session, job_id)
    if job.state != "running" or job.worker_id != worker_id:
        raise StateConflictError(
            f"job {job_id} is {job.state} and is not held by {worker_id}; the lease may have been reaped"
        )

    job.lease_expires_at = at + timedelta(seconds=lease_seconds)
    job.heartbeat_at = at
    if progress is not None:
        job.progress = progress
    session.flush()
    return job


def finish(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    outcome: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    result_summary: dict[str, Any] | None = None,
    result_artifact_ref: str | None = None,
    run_manifest_ref: str | None = None,
    resource_summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DurableJob:
    """Commit a terminal outcome and seal the attempt that produced it.

    `outcome` is one of `succeeded`, `failed`, `cancelled`. The job row and its
    open attempt row move together in the caller's transaction, so there is no
    ordering of failures that leaves a finished job with an attempt still
    recorded as running.
    """
    if outcome not in {"succeeded", "failed", "cancelled"}:
        raise ValidationFailedError(f"'{outcome}' is not a terminal job outcome")
    if (outcome == "failed") != (error_code is not None):
        raise ValidationFailedError("a failed job names its error code, and only a failed job has one")

    at = now or _now()
    job = load_job(session, job_id)
    if job.state != "running" or job.worker_id != worker_id:
        raise StateConflictError(
            f"job {job_id} is {job.state} and is not held by {worker_id}; only the lease holder finishes it"
        )

    _seal_open_attempt(
        session,
        job,
        outcome=outcome,
        error_code=error_code,
        error_detail=error_detail,
        resource_summary=resource_summary,
        at=at,
    )

    job.state = outcome
    job.terminal_at = at
    job.error_code = error_code
    job.error_detail = error_detail
    job.result_summary = result_summary
    job.result_artifact_ref = result_artifact_ref
    job.run_manifest_ref = run_manifest_ref
    # `ck_durable_jobs_running_holds_lease` is an equivalence: a job that is not
    # running holds no lease. Leaving one behind would make the reaper's
    # population undecidable.
    job.lease_expires_at = None
    session.flush()
    logger.info("job.finished", job_id=str(job.job_id), outcome=outcome, worker_id=worker_id)
    return job


def wait_for(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    reason: str,
    reconcile_after: datetime,
    dependency_ref: str | None = None,
    now: datetime | None = None,
) -> DurableJob:
    """Park a job that cannot progress until a declared dependency resolves.

    `13-…:§7.1`: entering `waiting` "records a typed reason, dependency/provider
    attempt reference and `reconcile_after`; it never retains a human
    credential." The attempt that parked the job is left OPEN rather than
    sealed: the job has not been executed twice when it resumes, and closing the
    attempt here would make the history claim it had.
    """
    at = now or _now()
    if not reason.strip():
        raise ValidationFailedError("a waiting job declares why it is waiting")
    if reconcile_after <= at:
        raise ValidationFailedError("reconcile_after must be in the future")

    job = load_job(session, job_id)
    if job.state != "running" or job.worker_id != worker_id:
        raise StateConflictError(f"job {job_id} is {job.state} and is not held by {worker_id}")

    job.state = "waiting"
    job.waiting_reason = reason
    job.waiting_dependency_ref = dependency_ref
    job.reconcile_after = reconcile_after
    job.worker_id = None
    job.lease_expires_at = None
    session.flush()
    return job


def request_cancel(
    session: Session,
    actor: Actor,
    job_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> DurableJob:
    """Record cancellation intent. Cooperative, and never an error on a finished job.

    `13-…:§7.3`. A terminal job is returned as it stands — see the module
    docstring for why that is a 200 and not a 409.

    A `queued` job is NOT terminated here, even though it could be: the row is
    flagged, and `claim_next` refuses to start a flagged job. Terminating it in
    this function would put a second writer of terminal state on the row, and
    the whole reason cancellation is a flag is that there must be exactly one.
    """
    at = now or _now()
    job = load_job(session, job_id)

    if job_catalog.is_terminal(job.state):
        return job
    if job.cancel_requested_at is not None:
        return job

    job.cancel_requested_at = at
    job.cancel_requested_by = actor.subject_id
    session.flush()
    logger.info("job.cancel_requested", job_id=str(job.job_id), state=job.state)
    return job


def reconcile_waiting(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[uuid.UUID]:
    """Return parked jobs whose `reconcile_after` has passed to the queue.

    `13-…:§7.1`: "A reconciler reauthorizes the resource and either returns the
    same job to `queued`, requests cancellation, or applies a terminal provider
    result." This is the first of those three, and it is deliberately the only
    one implemented here.

    ⚠️ **It does not reauthorize.** The `§7.1` sentence puts reauthorization
    before the requeue, and this function cannot do it: authorization in this
    service runs off an `Actor` built from a verified credential
    (`api/dependencies`), a job stores an identity *reference* rather than a
    credential (`§9.3`), and there is no delegation-grant mechanism yet
    (`§9.2`, `FND-004`). So a reconciler that claimed to reauthorize would be
    checking nothing while reading as though it had — the shape
    `a-control-whose-premise-is-unmet-manufactures-evidence` describes.

    What exists instead is the honest half: a parked job returns to `queued`,
    and the claimant then runs it under whatever policy the executing worker
    establishes. The gap is that a job parked before a right was revoked resumes
    without noticing, and it is recorded here and in `README.md` rather than
    papered over. Closing it is `FND-004`'s delegated-context work, not this
    package's.

    A job with a cancellation request is cancelled rather than requeued: it was
    flagged while parked, and returning it to the queue only so the claimant can
    skip it would leave it non-terminal forever.
    """
    at = now or _now()
    due = (
        session.execute(
            text(
                """
            SELECT job_id FROM durable_jobs
             WHERE state = 'waiting' AND reconcile_after <= :now
             ORDER BY reconcile_after
             FOR UPDATE SKIP LOCKED
             LIMIT :limit
            """
            ),
            {"now": at, "limit": limit},
        )
        .scalars()
        .all()
    )

    moved: list[uuid.UUID] = []
    for job_id in due:
        job = session.get(DurableJob, job_id)
        if job is None:  # pragma: no cover - selected under a row lock
            continue
        cancelling = job.cancel_requested_at is not None

        # ⚠️ The attempt is sealed FIRST, before the job row is touched.
        # `_seal_open_attempt` issues a SELECT, which triggers SQLAlchemy's
        # autoflush — and a half-mutated job (still `waiting`, reason already
        # cleared) violates `ck_durable_jobs_waiting_declares_why` at that
        # flush, several frames from the line that caused it. Ordering the
        # writes so no intermediate state is ever flushed is the fix; a
        # `no_autoflush` block would only hide when the bad row is written.
        if cancelling:
            _seal_open_attempt(
                session,
                job,
                outcome="cancelled",
                error_code=None,
                error_detail=None,
                resource_summary=None,
                at=at,
            )

        # `waiting_*` and `state` move in ONE mutation for the same reason:
        # the CHECK is an equivalence, so a row that is no longer waiting must
        # no longer declare a reason.
        job.waiting_reason = None
        job.waiting_dependency_ref = None
        job.reconcile_after = None
        if cancelling:
            job.state = "cancelled"
            job.terminal_at = at
        else:
            job.state = "queued"
        moved.append(job.job_id)
        logger.info("job.reconciled", job_id=str(job.job_id), cancelled=cancelling)

    session.flush()
    return moved


def reap_expired_leases(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[uuid.UUID]:
    """Return jobs whose worker stopped heartbeating to the queue, or fail them.

    `13-…:§7.4` startup and steady-state recovery. Each reaped job's open attempt
    is sealed with `lease_expired`, which is why that outcome exists: without it
    the history cannot tell a crash apart from a failure, and an operator reading
    the attempt table would see a job that "failed" three times when what
    actually happened is that a node was drained three times.

    A type outside `RECLAIMABLE_ON_LEASE_EXPIRY` is failed rather than requeued
    (`13-…:§7.2`, "reclaimed only when the method's retry policy says execution
    is safe"), and so is a job that has spent its attempt budget.

    Returns the job ids it touched, so a caller can log or meter them. `SKIP
    LOCKED` again: a second reaper running concurrently steps over rows the first
    is already handling rather than blocking behind them.

    ⛔ Expiry is decided by the DATABASE's clock, never this pod's — see
    `_db_now` for the measured duplication that reading a local wall clock
    produced.
    """
    # The DATABASE's clock, not this pod's. See `_db_now`: an explicit `now=`
    # is a declared test seam; the default path can never read a skewed host clock.
    at = now if now is not None else _db_now(session)
    expired = (
        session.execute(
            text(
                """
            SELECT job_id FROM durable_jobs
             WHERE state = 'running' AND lease_expires_at <= :now
             ORDER BY lease_expires_at
             FOR UPDATE SKIP LOCKED
             LIMIT :limit
            """
            ),
            {"now": at, "limit": limit},
        )
        .scalars()
        .all()
    )

    reaped: list[uuid.UUID] = []
    for job_id in expired:
        job = session.get(DurableJob, job_id)
        if job is None:  # pragma: no cover - selected under a row lock
            continue

        safe = job.job_type in RECLAIMABLE_ON_LEASE_EXPIRY
        budget_left = job.attempt < job.max_attempts
        requeue = safe and budget_left and job.cancel_requested_at is None

        _seal_open_attempt(
            session,
            job,
            outcome="lease_expired",
            error_code=None,
            error_detail=None,
            resource_summary=None,
            at=at,
        )

        job.worker_id = None
        job.lease_expires_at = None
        if requeue:
            job.state = "queued"
        elif job.cancel_requested_at is not None:
            job.state = "cancelled"
            job.terminal_at = at
        else:
            job.state = "failed"
            job.terminal_at = at
            job.error_code = LEASE_EXHAUSTED
            job.error_detail = (
                "the lease expired and the job is not reclaimable"
                if not safe
                else f"the lease expired after {job.attempt} of {job.max_attempts} attempts"
            )
        reaped.append(job.job_id)
        logger.warning("job.lease_reaped", job_id=str(job.job_id), requeued=requeue, attempt=job.attempt)

    session.flush()
    return reaped


def load_job(session: Session, job_id: uuid.UUID) -> DurableJob:
    """The job, or a 404 that does not distinguish absent from another tenant's.

    RLS makes a foreign-tenant row invisible rather than forbidden, and this
    reads back as "not found" deliberately: a 403 here would confirm that a job
    with that id exists somewhere, which is a cross-tenant existence oracle over
    a guessable identifier space.
    """
    job = session.get(DurableJob, job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} was not found")
    return job


def _seal_open_attempt(
    session: Session,
    job: DurableJob,
    *,
    outcome: str,
    error_code: str | None,
    error_detail: str | None,
    resource_summary: dict[str, Any] | None,
    at: datetime,
) -> None:
    """Close this job's in-flight attempt, if it has one.

    Absence is tolerated rather than an error. A job whose attempt row is
    missing is a job somebody reaped by hand or restored from a partial backup,
    and refusing to let it reach a terminal state would leave it running
    forever — a worse outcome than a history with one gap in it, which is at
    least visible.
    """
    attempt = session.execute(
        select(JobAttempt)
        .where(
            JobAttempt.job_id == job.job_id,
            JobAttempt.attempt_number == job.attempt,
            JobAttempt.ended_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if attempt is None:
        logger.warning("job.attempt_missing", job_id=str(job.job_id), attempt=job.attempt)
        return
    attempt.ended_at = at
    attempt.outcome = outcome
    attempt.error_code = error_code
    attempt.error_detail = error_detail
    attempt.resource_summary = resource_summary
