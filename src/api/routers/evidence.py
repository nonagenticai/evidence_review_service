"""`/api/reviews/{id}` — accepted evidence (`ER-006`).

Research objects, manual extraction, research assertions and acceptance. The
authority split lives in `application.services.evidence`, not here: recording is
a reviewer's work, accepting is the lead's.

Every handler guards its body. `api.body_guard` is imported PER ROUTER in this
service rather than installed as middleware, so a new router that forgets it is
a new router with no depth bound and no pre-ADR denylist — which is exactly how
`DedupRunBody.config` came to accept a `work_type` and return `201`. `ER-006.8`
and `DM-AT-18` are the requirement; the import below is the mechanism.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from api.body_guard import guard_body
from api.dependencies import ActorDep
from application.services import evidence as svc
from application.services import reviews as review_svc
from database import actor_scope

router = APIRouter(prefix="/api/reviews", tags=["Accepted evidence"])

#: Ceiling on observations in one `POST .../extractions`.
#:
#: The same shape of limit `screening.MAX_ASSIGNMENT_REVIEWERS` exists for: this
#: route writes `2 x len(observations)` rows synchronously in one transaction
#: with no job behind it. A data-extraction form for one study carries tens of
#: variables; 500 is an order of magnitude above anything real, and far enough
#: below `int4` that a typo'd or hostile payload is a 422 at the edge rather
#: than a slow transaction and a raw driver message.
MAX_OBSERVATIONS_PER_EXTRACTION = 500

#: Ceiling on cited observations in one assertion. Same reasoning.
MAX_SUPPORT_REFS = 500


class RecordLink(BaseModel):
    review_record_id: uuid.UUID
    review_record_version_id: uuid.UUID
    corpus_version_id: uuid.UUID
    link_role: str = Field(pattern="^(primary|companion)$")
    rationale: str | None = Field(default=None, max_length=4000)


class ResearchObjectBody(BaseModel):
    object_key: str = Field(min_length=1, max_length=200)
    content: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=4000)
    records: list[RecordLink] = Field(min_length=1, max_length=100)


class ReviseObjectBody(BaseModel):
    content: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=4000)
    revision_kind: str = Field(pattern="^(correction|material_change)$")


class ObservationBody(BaseModel):
    variable_key: str = Field(min_length=1, max_length=200)
    value: Any
    unit: str | None = Field(default=None, max_length=100)
    #: `16-…:§3.7` arm 2. Required, and not defaulted: an expert assertion whose
    #: rationale the service invented is not an expert assertion.
    expert_rationale: str = Field(min_length=1, max_length=4000)
    expert_subject_id: str | None = Field(default=None, max_length=200)
    expert_role: str | None = Field(default=None, max_length=100)
    basis_kind: str = Field(default="expert_assertion", pattern="^(expert_assertion|evidence_located)$")


class ExtractionBody(BaseModel):
    dataset_key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    observations: list[ObservationBody] = Field(min_length=1, max_length=MAX_OBSERVATIONS_PER_EXTRACTION)


class SupportRef(BaseModel):
    observation_version_id: uuid.UUID
    stance: str = Field(pattern="^(supports|contradicts)$")


class AssertionBody(BaseModel):
    #: Bound and pattern MUST equal `urn:datacompany:review-assertion-recorded:1`'s
    #: `assertion_key` constraints (`maxLength: 128`,
    #: `pattern: ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`) — see
    #: `test_the_assertion_key_bound_and_pattern_match_the_released_contract`
    #: in `tests/unit/test_evidence_api_bounds_match_the_released_contract.py`.
    #: The old `max_length=200` with no pattern was looser than the contract
    #: on both axes independently (length AND character set); the outbox
    #: writes this value verbatim (`events.py::emit_assertion_recorded`) and
    #: is append-only, so this is the only place either can be caught.
    assertion_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    statement: str = Field(min_length=1, max_length=8000)
    content: dict[str, Any] = Field(default_factory=dict)
    support: list[SupportRef] = Field(min_length=1, max_length=MAX_SUPPORT_REFS)


class AcceptanceBody(BaseModel):
    subject_kind: str = Field(pattern="^(research_object_version|extraction_dataset_version|observation_version)$")
    subject_version_id: uuid.UUID
    decision: str = Field(pattern="^(accept|withdraw)$")
    rationale: str = Field(min_length=1, max_length=4000)
    supersedes_acceptance_id: uuid.UUID | None = None


@router.post(
    "/{review_id}/research-objects",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_research_object",
)
def create_research_object(actor: ActorDep, review_id: uuid.UUID, body: ResearchObjectBody) -> dict[str, Any]:
    """Group one or more included reports into the study they report on."""
    guard_body(body.content, "the research object content")
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.create_research_object(
            session,
            actor,
            review_id,
            object_key=body.object_key,
            content=body.content,
            rationale=body.rationale,
            records=[link.model_dump(mode="json") for link in body.records],
        )


@router.get("/{review_id}/research-objects", operation_id="list_research_objects")
def list_research_objects(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.list_research_objects(session, review_id)


@router.post(
    "/{review_id}/research-objects/{research_object_id}:revise",
    status_code=status.HTTP_201_CREATED,
    operation_id="revise_research_object",
)
def revise_research_object(
    actor: ActorDep, review_id: uuid.UUID, research_object_id: uuid.UUID, body: ReviseObjectBody
) -> dict[str, Any]:
    """A new version, superseding the current one. Never an edit."""
    guard_body(body.content, "the research object content")
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.revise_research_object(
            session,
            actor,
            review_id,
            research_object_id,
            content=body.content,
            rationale=body.rationale,
            revision_kind=body.revision_kind,
        )


@router.post(
    "/{review_id}/research-objects/{research_object_id}/extractions",
    status_code=status.HTTP_201_CREATED,
    operation_id="record_extraction",
)
def record_extraction(
    actor: ActorDep, review_id: uuid.UUID, research_object_id: uuid.UUID, body: ExtractionBody
) -> dict[str, Any]:
    """One dataset version and the observations in it.

    An observation whose `variable_key` already exists is a correction: a new
    observation version superseding the current one, not a duplicate row.
    """
    for observed in body.observations:
        guard_body({"value": observed.value}, f"the value of {observed.variable_key!r}")
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.record_extraction(
            session,
            actor,
            review_id,
            research_object_id,
            dataset_key=body.dataset_key,
            rationale=body.rationale,
            observations=[o.model_dump(mode="json") for o in body.observations],
        )


@router.post("/{review_id}/assertions", status_code=status.HTTP_201_CREATED, operation_id="record_assertion")
def record_assertion(actor: ActorDep, review_id: uuid.UUID, body: AssertionBody) -> dict[str, Any]:
    """A reviewed claim, citing the observations for and against it."""
    guard_body(body.content, "the assertion content")
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.record_assertion(
            session,
            actor,
            review_id,
            assertion_key=body.assertion_key,
            statement=body.statement,
            content=body.content,
            support=[s.model_dump(mode="json") for s in body.support],
        )


@router.post("/{review_id}/acceptances", status_code=status.HTTP_201_CREATED, operation_id="decide_acceptance")
def decide_acceptance(actor: ActorDep, review_id: uuid.UUID, body: AcceptanceBody) -> dict[str, Any]:
    """Accept or withdraw an exact version. Lead-only — this is the authority act."""
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.decide_acceptance(
            session,
            actor,
            review_id,
            subject_kind=body.subject_kind,
            subject_version_id=body.subject_version_id,
            decision=body.decision,
            rationale=body.rationale,
            supersedes_acceptance_id=body.supersedes_acceptance_id,
        )


@router.get("/{review_id}/acceptances", operation_id="list_acceptances")
def list_acceptances(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.list_acceptances(session, review_id)
