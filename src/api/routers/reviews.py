"""`/api/reviews` — review lifecycle and protocol locking (`ER-001`).

Routes are UNVERSIONED per the fleet rule: `/api/<resource>`, never
`/api/v1/<resource>`.

Every handler opens its own `actor_scope`, which binds the tenant/actor GUCs
inside the transaction before any statement runs. There is no "get a session"
dependency that could hand a router an unbound one — an unbound tenant makes
every RLS policy match nothing, and "no rows" is indistinguishable from "no data"
at the call site.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from api.body_guard import guard_body
from api.dependencies import ActorDep
from api.problem_details import NotFoundError, StateConflictError, ValidationFailedError
from application.services import reviews as svc
from database import actor_scope
from models.reviews import Review, ReviewProtocolVersion

router = APIRouter(prefix="/api/reviews", tags=["Reviews"])


class CreateReviewBody(BaseModel):
    project_id: str = Field(min_length=1)
    #: Bound MUST equal `urn:datacompany:review-created:1`'s `external_key.maxLength`
    #: (256) — see `test_the_external_key_bound_matches_the_released_contract`
    #: in `tests/unit/test_reviews_api_bounds_match_the_released_contract.py`.
    #: The outbox writes this value verbatim (`events.py::emit_review_created`)
    #: and is append-only, so a value the contract would reject can never be
    #: corrected after the fact — this is the only gate that can stop it.
    external_key: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1)


class ProtocolBody(BaseModel):
    inclusion_criteria: dict[str, Any]
    exclusion_criteria: dict[str, Any]
    stage_definitions: list[dict[str, Any]] = Field(min_length=1)
    exclusion_reason_vocabulary: list[str] = Field(min_length=1)
    reviewers_required: int = Field(default=2, ge=1, le=10)


class TransitionBody(BaseModel):
    rationale: str = Field(min_length=1)
    expected_revision: int | None = None
    approval_attestation_ref: str | None = None


class PatchReviewBody(BaseModel):
    """Only `title` is editable, and only before activation.

    `project_id` and `external_key` are the review's identity — the unique key
    other systems cite — so changing them would silently repoint a reference
    rather than correct a mistake.
    """

    title: str = Field(min_length=1)
    expected_revision: int | None = None


class ActivateBody(BaseModel):
    protocol_version_id: uuid.UUID
    rationale: str = Field(min_length=1)
    expected_revision: int | None = None


def _guard(payload: Any, what: str = "the request body") -> None:
    """`ER-002.7` applied to every inbound body, not only to imports.

    A guarded discriminator in a protocol's criteria would activate the same
    unaccepted model as one in a record, so the check lives at the edge.

    This used to call `find_forbidden_keys` and nothing else, which made it the
    crash site rather than the guard: `ProtocolBody`'s criteria are
    `dict[str, Any]`, Pydantic validates nothing inside them, and the recursive
    denylist walk gave out just past the interpreter's 1,000-frame limit. The
    result was a bare `500 text/plain` — measured on `393ae61` at 1,200 levels —
    reachable with nothing but a tenant header, since this runs before
    `actor_scope`. It now delegates to `api.body_guard`, which bounds the depth
    first and refuses with problem+json.
    """
    guard_body(payload, what)


@router.post("", status_code=status.HTTP_201_CREATED, operation_id="create_review")
def create_review(actor: ActorDep, body: CreateReviewBody, response: Response) -> dict[str, Any]:
    _guard(body.model_dump())
    with actor_scope(actor) as session:
        review = svc.create_review(
            session,
            actor,
            project_id=body.project_id,
            external_key=body.external_key,
            title=body.title,
        )
        summary = svc.review_summary(session, review)
    response.headers["ETag"] = f'"{summary["revision"]}"'
    return summary


@router.get("", operation_id="list_reviews")
def list_reviews(
    actor: ActorDep,
    project_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_archived: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Archived reviews are hidden unless `include_archived=true`.

    Archiving is how a review is disposed of — the database is append-only, so
    there is no delete — and a disposed-of review that still fills the first
    screen has not been disposed of. `archived` has no outgoing edge, so "is
    archived" is exactly "has a history row INTO archived": a NOT EXISTS in the
    same statement, filtered before `LIMIT`, at no extra statement per page.
    """
    from sqlalchemy import exists, select

    from models.reviews import ReviewStateEvent

    with actor_scope(actor) as session:
        stmt = select(Review).order_by(Review.created_at.desc()).limit(limit)
        if project_id:
            stmt = stmt.where(Review.project_id == project_id)
        if not include_archived:
            stmt = stmt.where(
                ~exists().where(
                    ReviewStateEvent.review_id == Review.review_id,
                    ReviewStateEvent.to_state == "archived",
                )
            )
        rows = session.execute(stmt).scalars().all()
        # `review_summaries`, not a comprehension over `review_summary`: the
        # per-row form issued 3 statements each — the history tail, the
        # activation's protocol id, and the `session.get` for it — so this
        # screen cost 1 + 3N, or 151 statements at the default page of 50.
        return {"items": svc.review_summaries(session, rows), "count": len(rows)}


@router.get("/{review_id}", operation_id="get_review")
def get_review(actor: ActorDep, review_id: uuid.UUID, response: Response) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review = svc.load_review(session, review_id)
        summary = svc.review_summary(session, review)
    response.headers["ETag"] = f'"{summary["revision"]}"'
    return summary


@router.patch("/{review_id}", operation_id="patch_review")
def patch_review(actor: ActorDep, review_id: uuid.UUID, body: PatchReviewBody, response: Response) -> dict[str, Any]:
    """`§8.2` — "patch only pre-activation with expected version"."""
    _guard(body.model_dump())
    with actor_scope(actor) as session:
        review = svc.load_review(session, review_id)
        updated = svc.patch_review(session, actor, review, title=body.title, expected_revision=body.expected_revision)
        summary = svc.review_summary(session, updated)
    response.headers["ETag"] = f'"{summary["revision"]}"'
    return summary


@router.get("/{review_id}/state-history", operation_id="get_review_state_history")
def state_history(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    """The append-only history that `state` is derived from.

    Exposed because "current state" without the transitions that produced it is
    a claim; with them it is evidence.
    """
    from sqlalchemy import select

    from models.reviews import ReviewStateEvent

    with actor_scope(actor) as session:
        svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(ReviewStateEvent)
                .where(ReviewStateEvent.review_id == review_id)
                .order_by(ReviewStateEvent.revision_number)
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "state_event_id": str(e.state_event_id),
                    "revision_number": e.revision_number,
                    "from_state": e.from_state,
                    "to_state": e.to_state,
                    "rationale": e.rationale,
                    "actor_subject_id": e.actor_subject_id,
                    "protocol_version_id": str(e.protocol_version_id) if e.protocol_version_id else None,
                    "approval_attestation_ref": e.approval_attestation_ref,
                    "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                }
                for e in rows
            ],
            "count": len(rows),
        }


@router.post(
    "/{review_id}/protocol-versions",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_protocol_version",
)
def create_protocol_version(actor: ActorDep, review_id: uuid.UUID, body: ProtocolBody) -> dict[str, Any]:
    _guard(body.model_dump(), "the protocol body")
    with actor_scope(actor) as session:
        review = svc.load_review(session, review_id)
        version = svc.create_protocol_version(
            session,
            actor,
            review,
            inclusion_criteria=body.inclusion_criteria,
            exclusion_criteria=body.exclusion_criteria,
            stage_definitions=body.stage_definitions,
            exclusion_reason_vocabulary=body.exclusion_reason_vocabulary,
            reviewers_required=body.reviewers_required,
        )
        return _protocol_summary(version)


@router.get("/{review_id}/protocol-versions", operation_id="list_protocol_versions")
def list_protocol_versions(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import select

    with actor_scope(actor) as session:
        svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(ReviewProtocolVersion)
                .where(ReviewProtocolVersion.review_id == review_id)
                .order_by(ReviewProtocolVersion.version_number)
            )
            .scalars()
            .all()
        )
        return {"items": [_protocol_summary(v) for v in rows], "count": len(rows)}


@router.post(
    "/{review_id}/protocol-versions/{protocol_version_id}:lock",
    operation_id="lock_protocol_version",
)
def lock_protocol_version(actor: ActorDep, review_id: uuid.UUID, protocol_version_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        svc.load_review(session, review_id)
        version = session.get(ReviewProtocolVersion, protocol_version_id)
        if version is None or version.review_id != review_id:
            raise NotFoundError("no such protocol version for this review")
        locked = svc.lock_protocol_version(session, actor, version)
        return _protocol_summary(locked)


@router.post("/{review_id}:activate", operation_id="activate_review")
def activate_review(actor: ActorDep, review_id: uuid.UUID, body: ActivateBody, response: Response) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review = svc.load_review(session, review_id)
        svc.activate(
            session,
            actor,
            review,
            protocol_version_id=body.protocol_version_id,
            rationale=body.rationale,
            expected_revision=body.expected_revision,
        )
        summary = svc.review_summary(session, review)
    # ETag on the WRITE, not only on the read. Without it a client must issue a
    # GET after every transition before it can send the next
    # `expected_revision` — and the one that does not bother silently drops to
    # last-write-wins. `fr`'s reviews client already documents this header as
    # present on lifecycle writes; it was not.
    response.headers["ETag"] = f'"{summary["revision"]}"'
    return summary


def _lifecycle_route(name: str, to_state: str, from_states: tuple[str, ...]):  # noqa: ANN202
    """One handler shape for pause/resume/close/reopen/archive.

    They are *distinct commands* per `01-…:123`, and `from_states` is what makes
    them distinct. Without it `:resume` and `:reopen` were the SAME operation
    wearing two URLs: both mapped to `to_state="active"`, and legality was
    decided only by `(from_state, to_state)` — which `LEGAL_TRANSITIONS` allows
    from BOTH `paused` and `closed`. So `:resume` on a closed review performed a
    reopen, and `:reopen` on a paused one performed a resume that skipped the
    attestation. A UI rendering a "Resume" button would have reopened a closed
    review.
    """

    def handler(actor: ActorDep, review_id: uuid.UUID, body: TransitionBody, response: Response) -> dict[str, Any]:
        with actor_scope(actor) as session:
            review = svc.load_review(session, review_id)
            state, _ = svc.current_state(session, review.review_id)
            if state not in from_states:
                # A typed 409 naming the command, not the generic
                # "illegal transition" the shared machine would emit — the
                # caller used the wrong verb, which is a different mistake from
                # asking for an impossible transition.
                raise StateConflictError(
                    f"{name.split('_')[0]} applies to a review in {' or '.join(from_states)}; this review is {state}",
                    current_state=state,
                    allowed_from=list(from_states),
                )
            svc.transition(
                session,
                actor,
                review,
                to_state=to_state,
                rationale=body.rationale,
                expected_revision=body.expected_revision,
                approval_attestation_ref=body.approval_attestation_ref,
            )
            summary = svc.review_summary(session, review)
        response.headers["ETag"] = f'"{summary["revision"]}"'
        return summary

    handler.__name__ = name
    return handler


#: command -> (to_state, the states it may be issued from).
#: `resume` and `reopen` both land on `active` and are told apart ONLY by where
#: they start; that is the whole point of the second element.
LIFECYCLE_COMMANDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pause_review", "paused", ("active",)),
    ("resume_review", "active", ("paused",)),
    ("close_review", "closed", ("active", "paused")),
    ("reopen_review", "active", ("closed",)),
    # `draft` as well as `closed` (`0028`): the database is append-only, so an
    # abandoned draft has no delete to fall back on — archiving is its only exit.
    # Every other state is a 409: an active or paused review must be closed
    # first, so the close gate (no unresolved disagreements) still holds.
    ("archive_review", "archived", ("draft", "closed")),
)

_LIFECYCLE_HANDLERS = {name: _lifecycle_route(name, state, frm) for name, state, frm in LIFECYCLE_COMMANDS}

for _name, _state, _from in LIFECYCLE_COMMANDS:
    router.add_api_route(
        f"/{{review_id}}:{_name.split('_')[0]}",
        _LIFECYCLE_HANDLERS[_name],
        methods=["POST"],
        operation_id=_name,
        name=_name,
    )

#: `POST /api/reviews/{id}/archive` — the same handler as `:archive`, under the
#: slash spelling kag-frontend's archive button is built against. One handler,
#: two URLs: the state guard, lead check, history row and outbox event cannot
#: drift between them because there is nothing to drift.
router.add_api_route(
    "/{review_id}/archive",
    _LIFECYCLE_HANDLERS["archive_review"],
    methods=["POST"],
    operation_id="archive_review_slash",
    name="archive_review_slash",
)


def _protocol_summary(version: ReviewProtocolVersion) -> dict[str, Any]:
    return {
        "protocol_version_id": str(version.protocol_version_id),
        "review_id": str(version.review_id),
        "version_number": version.version_number,
        "schema_ref": version.schema_ref,
        "content_hash": version.content_hash,
        "inclusion_criteria": version.inclusion_criteria,
        "exclusion_criteria": version.exclusion_criteria,
        "stage_definitions": version.stage_definitions,
        "exclusion_reason_vocabulary": version.exclusion_reason_vocabulary,
        "reviewers_required": version.reviewers_required,
        "locked": version.locked_at is not None,
        "locked_at": version.locked_at.isoformat() if version.locked_at else None,
        "locked_by_subject_id": version.locked_by_subject_id,
    }


__all__ = ["router", "ValidationFailedError"]
