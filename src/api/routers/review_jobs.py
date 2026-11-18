"""`/api/review-jobs/{job_id}` — the durable job read and cooperative cancel (`FND-006`).

`14-…`: "All non-terminal import, deduplication, prioritization, snapshot and
export jobs expose `GET /review-jobs/{job_id}` and
`POST /review-jobs/{job_id}:cancel`. Cancellation is cooperative,
input-hash/idempotency scoped, and cannot roll back an already committed domain
result."

## Why this router is not under `/api/reviews/{review_id}/…`

Every other router in this service is, and the trace table in `14-…` puts these
two routes at the top level deliberately: a caller holding a job id from a `202`
has the job id and nothing else. Requiring the review id as well would mean
either handing back a composite the caller has to keep together, or letting them
pass any review id and having the server ignore it — and a path segment the
server ignores is one a reader will assume is checked.

The job carries its own `review_id`, so the resource scoping is real; it is just
derived from the row rather than from the URL.

## The authorisation is the review's, not the job's

A job is an operation on a review, so `require_review_lead` on the job's review
is the gate — with one addition: the person who submitted the job may read and
cancel their own. Without that, a reviewer permitted to start an import could
not then ask whether it had finished, which is not a security property, it is a
product that appears to hang.

`load_job` inside the service answers a foreign-tenant job with 404 rather than
403, for the reason recorded there: 403 would confirm that a job with that id
exists somewhere.

## ⛔ Nothing executes these jobs yet

`FND-006` builds the substrate. There is no worker loop — see
`application/services/jobs.py` and `domain/policies/job_catalog.OWED_BY`. A job
read here is durable, attributable and cancellable; it is not necessarily
progressing, and a `queued` job may stay `queued` indefinitely until the
producer package that owes it lands its entrypoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from api.dependencies import ActorDep
from api.problem_details import ForbiddenError
from application.services import jobs as svc
from application.services import reviews as review_svc
from database import actor_scope
from instants import utc_z
from models.foundation import DurableJob, JobAttempt

router = APIRouter(prefix="/api/review-jobs", tags=["Review jobs"])


def _authorize(session: Any, actor: Any, job: DurableJob) -> None:
    """Lead over the job's review, or the person who submitted it.

    The originator check compares `originating_subject_id` against the caller's
    verified subject — never anything from the request — because `01-…:687`
    ("derive actor from verified credentials") is the whole reason
    `api/dependencies` is the only constructor of an `Actor`.
    """
    review = review_svc.load_review(session, job.review_id)
    subject = actor.subject_id
    if subject is not None and job.originating_subject_id == subject:
        return
    try:
        review_svc.require_review_lead(actor, review)
    except ForbiddenError:
        raise ForbiddenError(
            f"job {job.job_id} belongs to a review you do not lead, and you did not submit it"
        ) from None


def _iso(value: datetime | None) -> str | None:
    """Kept as a name this module already used; the implementation moved.

    It moved because `ER-007` needed the same normalisation and reached for a
    cross-router import of this private name. `instants.utc_z` carries
    the measurement that justifies it — including the second occurrence, which
    is the reason it is shared rather than private.
    """
    return utc_z(value)


def _render(job: DurableJob, attempts: list[JobAttempt]) -> dict[str, Any]:
    """The wire shape. `cancel_requested` is reported BESIDE `state`, never inside it.

    `13-…:§7.1` is explicit that `cancel_requested` "is not an extra public
    state". A client polling `state` therefore always sees one of the six, and
    learns whether somebody has asked for a stop from a separate boolean — which
    is what lets it render "finishing up" without having to model a seventh
    value that would then have to be handled by every switch statement that
    reads this field.
    """
    return {
        "job_id": str(job.job_id),
        "review_id": str(job.review_id),
        "job_type": job.job_type,
        "state": job.state,
        "cancel_requested": job.cancel_requested_at is not None,
        "cancel_requested_at": _iso(job.cancel_requested_at),
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "command_hash": job.command_hash,
        "input_snapshot_hash": job.input_snapshot_hash,
        "idempotency_key": job.idempotency_key,
        "progress": job.progress,
        "waiting": (
            None
            if job.state != "waiting"
            else {
                "reason": job.waiting_reason,
                "dependency_ref": job.waiting_dependency_ref,
                "reconcile_after": _iso(job.reconcile_after),
            }
        ),
        "result": {
            "summary": job.result_summary,
            "artifact_ref": job.result_artifact_ref,
            "run_manifest_ref": job.run_manifest_ref,
            "error_code": job.error_code,
            "error_detail": job.error_detail,
        },
        "queued_at": _iso(job.queued_at),
        "started_at": _iso(job.started_at),
        "terminal_at": _iso(job.terminal_at),
        # The attempt history is part of the read, not a separate endpoint. It is
        # bounded by `max_attempts`, and the one question an operator always asks
        # about a job that took three tries is what happened on the first two.
        "attempts": [
            {
                "attempt_number": a.attempt_number,
                "worker_id": a.worker_id,
                "started_at": _iso(a.started_at),
                "ended_at": _iso(a.ended_at),
                "outcome": a.outcome,
                "error_code": a.error_code,
                "error_detail": a.error_detail,
                "resource_summary": a.resource_summary,
            }
            for a in attempts
        ],
    }


def _attempts(session: Any, job: DurableJob) -> list[JobAttempt]:
    return list(
        session.execute(
            select(JobAttempt).where(JobAttempt.job_id == job.job_id).order_by(JobAttempt.attempt_number)
        ).scalars()
    )


@router.get("/{job_id}", operation_id="get_review_job")
def get_review_job(actor: ActorDep, job_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        job = svc.load_job(session, job_id)
        _authorize(session, actor, job)
        return _render(job, _attempts(session, job))


# `:cancel` rather than `/cancel`: `13-…:§13` uses the colon form for actions
# that are not resources, and `/cancel` would read as a sub-resource that could
# be fetched. FastAPI matches it as an ordinary literal suffix on the path
# parameter, which is why the route is declared with the colon inside the
# segment rather than as a second segment.
@router.post("/{job_id}:cancel", operation_id="cancel_review_job")
def cancel_review_job(actor: ActorDep, job_id: uuid.UUID) -> dict[str, Any]:
    """Ask for a job to stop. 200 whether or not it was still running.

    `13-…:§7.3` — "cancellation that arrives after terminal commit returns the
    terminal outcome". A finished job answers with its outcome rather than an
    error, so a client that cancels and a client that polls read the same shape
    and neither has to treat "you got what you asked for" as a failure.
    """
    with actor_scope(actor) as session:
        job = svc.load_job(session, job_id)
        _authorize(session, actor, job)
        job = svc.request_cancel(session, actor, job_id)
        return _render(job, _attempts(session, job))
