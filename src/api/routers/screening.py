"""`/api/reviews/{id}` — deduplication (`ER-003`) and blinded screening (`ER-004`).

Both live in one router because they share the same `{review_id}` prefix and the
same actor plumbing. The blinding-sensitive handlers are grouped at the bottom
and every one of them either goes through `blinded_reads` or returns nothing
about another reviewer at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from api.body_guard import guard_body
from api.dependencies import ActorDep
from api.problem_details import NotFoundError
from application.services import deduplication as dedup_svc
from application.services import reviews as review_svc
from application.services import screening as svc
from database import actor_scope

router = APIRouter(prefix="/api/reviews", tags=["Screening"])

#: Ceiling on `round_number`, on every route that carries one.
#:
#: The HARD requirement is `int4`: every `round_number` column in `0005` is
#: `sa.Integer()`, and `ck_assignment_round_positive` bounds it below and not
#: above. So `round_number=2147483648` reached the driver and left as
#: `409 state_conflict` carrying `"integer out of range"` — raw Postgres text, on
#: `POST /decisions`, on `POST /assignments`, and on the SAFE method
#: `GET /screening/queue`. `_integrity_handler` passes constraint text through
#: deliberately, and says what it is promising: "our own `RAISE EXCEPTION`
#: strings, written to be read by whoever tripped them". A driver's overflow
#: message is neither, and 409 tells the caller the review's state is the
#: problem when no state could ever satisfy the request.
#:
#: 1000 rather than `2**31 - 1`, because the ceiling should describe the domain
#: and not the storage. A screening stage runs round 1; a pilot or
#: agreement-calibration pass followed by the real screen makes 2; a re-screen
#: after a protocol amendment makes 3. Four is already beyond anything this
#: service has seen, so 1000 is ~250x the realistic ceiling — it cannot refuse
#: real traffic — while sitting six orders of magnitude below the column's limit,
#: which is what makes a typo'd or hostile literal a 422 at the edge instead of a
#: mistyped round nobody will ever screen.
MAX_ROUND_NUMBER = 1000

#: Ceiling on how many reviewers ONE `POST /assignments` may name.
#:
#: This is a multiplier, not a list. `create_assignments` assigns every corpus
#: member to every named reviewer — deliberately, because dual screening means
#: both reviewers see every record — so rows written are `records x reviewers`,
#: synchronously, inside one transaction, on a route with no job behind it. That
#: function's own docstring sizes the real workload at "3,000-8,000 records at
#: two or three reviewers" and the insert was batched into multi-VALUES chunks to
#: survive it; the batching bounds the STATEMENT count and nothing bounded the
#: row count, which the caller supplied. Measured on `4ea77a0`: 10,000 reviewer
#: ids against a 3-record corpus returned 201 in 5.8s having written 30,000 rows.
#: On the docstring's own 8,000-record corpus the same request is 80,000,000.
#:
#: 20 is chosen against what a systematic review does, not against a round
#: number. Cochrane-style dual screening is 2; a third for a tie-break or a
#: methodologist makes 3-4. The only real case for more is a whole team screening
#: the same records to measure inter-rater agreement, and a screening team is
#: single figures to low tens. At 20 the worst realistic corpus is 160,000 rows —
#: large, bounded, and knowable from the request — and anything past it is a
#: caller who has confused this route with something else.
MAX_REVIEWERS_PER_ASSIGNMENT = 20

#: Ceiling on ONE reviewer subject id.
#:
#: The columns are `Text`, so there was no length at all: a 100,000-character id
#: was accepted and stored, once per record, and then read back on every queue
#: page and every decision list.
#:
#: 256 is sized to the identifiers that actually arrive. A Keycloak `sub` is a
#: 36-character UUID; the longest human-shaped alternative anyone substitutes is
#: an email address, which RFC 5321 caps at 254. 256 clears both with nothing to
#: spare that matters, and stays well inside the 512 the export contract already
#: allows for the free-form `tenant_id` beside it.
MAX_SUBJECT_ID_LENGTH = 256

#: Page bounds for `GET /decisions`, matching `/records` and `/conflicts`.
#:
#: That route had no `limit` at all — `?limit=1` returned `count=10`, because the
#: parameter did not exist and FastAPI dropped it silently, which is worse than a
#: large page: a client that believes it asked for one row was handed the review.
#: For a lead on a real dual review the unpaged response is `records x reviewers`
#: decisions.
#:
#: 500 is the ceiling both siblings already use, and 200 the default `/conflicts`
#: uses; a decision list is the same kind of lead-facing read, so it is the same
#: pair rather than a third convention.
MAX_DECISIONS_PAGE = 500
DEFAULT_DECISIONS_PAGE = 200


class DedupRunBody(BaseModel):
    """Engine configuration, merged over `DEFAULT_CONFIG`.

    `dict[str, Any]`, so Pydantic validates that it is an object and nothing
    about what is in it. That makes it the same shape of hole `ProtocolBody` had,
    and it went further: this body reached neither the depth bound NOR the
    denylist. See `create_dedup_run` for what each of those cost.
    """

    config: dict[str, Any] | None = None


class ResolveBody(BaseModel):
    resolution_type: str = Field(pattern="^(merge|split|no_match)$")
    rationale: str = Field(min_length=1)
    survivor_record_id: uuid.UUID | None = None
    #: field name -> the record VERSION the survivor's value comes from.
    survivorship: dict[str, uuid.UUID] | None = None


class AssignmentBody(BaseModel):
    stage_id: uuid.UUID
    round_number: int = Field(default=1, ge=1, le=MAX_ROUND_NUMBER)
    #: BOUNDED IN THREE WAYS, and each one closes a different hole. The list
    #: length is the write multiplier; the item length is what gets stored once
    #: per record; `min_length=1` on the item is the same emptiness rule
    #: `UnblindBody.disclose_to` documents — an assignment to `""` names the
    #: caller-less actor that `bind_actor` produces for a request with no
    #: subject at all. See the three constants above for how each number was
    #: chosen.
    reviewer_subject_ids: list[Annotated[str, Field(min_length=1, max_length=MAX_SUBJECT_ID_LENGTH)]] = Field(
        min_length=1, max_length=MAX_REVIEWERS_PER_ASSIGNMENT
    )
    corpus_version_id: uuid.UUID | None = None


class DecisionBody(BaseModel):
    """No reviewer field, deliberately.

    `01-…:687` — "reject or ignore actor/reviewer/tenant body fields". The
    strongest form of that is a schema with nowhere to put one; Pydantic's
    default `extra='ignore'` then drops an attempted `reviewer_subject_id`
    before it can reach any code.
    """

    stage_id: uuid.UUID
    round_number: int = Field(default=1, ge=1, le=MAX_ROUND_NUMBER)
    review_record_id: uuid.UUID
    disposition: str = Field(pattern="^(include|exclude|uncertain)$")
    exclusion_reason_code: str | None = None
    rationale: str | None = None
    supersedes_decision_id: uuid.UUID | None = None


class UnblindBody(BaseModel):
    reason: str = Field(min_length=1)
    #: WHO the disclosure is for, beyond the lead taking it (`ER-004.2`). L1's
    #: third clause reads this list, so omitting it discloses to the lead alone —
    #: the fail-closed default. Naming the two conflicting reviewers is the usual
    #: panel case and is deliberately a choice, not an assumption.
    #:
    #: `min_length=1` on the ITEM is not cosmetic. `bind_actor` binds
    #: `app.actor_subject_id` to `actor.subject_id or ""`, so an audience holding
    #: `""` matches every caller who presents no subject at all — measured: unblind
    #: with `{"disclose_to": [""]}`, then `GET /decisions` with only a tenant
    #: header, and both dispositions come back. `0008`'s
    #: `ck_unblinding_audience_not_empty` does not catch it: it bounds the
    #: CARDINALITY of the list, not the emptiness of its elements.
    disclose_to: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)


class AdjudicateBody(BaseModel):
    disposition: str = Field(pattern="^(include|exclude|uncertain)$")
    rationale: str = Field(min_length=1)
    exclusion_reason_code: str | None = None
    supersedes_adjudication_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# ER-003 — deduplication
# ---------------------------------------------------------------------------


@router.post(
    "/{review_id}/deduplication-runs",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_deduplication_run",
)
def create_dedup_run(actor: ActorDep, review_id: uuid.UUID, body: DedupRunBody | None = None) -> dict[str, Any]:
    """Freeze a run against the sealed corpus. The config is guarded like any body.

    `config` had no guard of either kind, which cost two separate things,
    both measured on `393ae61`:

    * **A bare `500 text/plain`.** The effective config is fed to
      `deduplication_engine.input_digest`, which canonicalises it under RFC 8785
      — a recursive walk. A 1,200-level `config` exhausted the stack and left as
      a `RecursionError`, which derives from `RuntimeError` and so passed every
      handler on the way out. No problem+json body at all.
    * **A guarded discriminator, accepted.** `{"config": {"work_type": …}}`
      returned `201`. The `_guard` on the reviews router documents itself as
      "`ER-002.7` applied to every inbound body, not only to imports"; this body
      was simply not among the ones it was wired to. The value is persisted to
      `deduplication_runs.config` (JSONB) and folded into `input_digest`, so the
      run's identity depends on a field the programme has refused to accept.

    Guarded before `create_run`, so neither reaches the engine.
    """
    if body is not None and body.config is not None:
        guard_body(body.config, "the deduplication config")
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return dedup_svc.create_run(session, actor, review_id, config=(body.config if body else None))


@router.get("/{review_id}/deduplication-runs", operation_id="list_deduplication_runs")
def list_dedup_runs(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import select

    from models.screening import DeduplicationRun

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(DeduplicationRun)
                .where(DeduplicationRun.review_id == review_id)
                .order_by(DeduplicationRun.created_at)
            )
            .scalars()
            .all()
        )
        return {"items": [dedup_svc.run_summary(session, r) for r in rows], "count": len(rows)}


@router.get("/{review_id}/duplicate-clusters", operation_id="list_duplicate_clusters")
def list_clusters(
    actor: ActorDep, review_id: uuid.UUID, dedup_run_id: Annotated[uuid.UUID | None, Query()] = None
) -> dict[str, Any]:
    from sqlalchemy import select

    from models.screening import DeduplicationRun

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        if dedup_run_id is None:
            run = session.execute(
                select(DeduplicationRun)
                .where(DeduplicationRun.review_id == review_id)
                .order_by(DeduplicationRun.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                raise NotFoundError("this review has no deduplication run")
            dedup_run_id = run.dedup_run_id
        items = dedup_svc.list_clusters(session, review_id, dedup_run_id)
        return {"dedup_run_id": str(dedup_run_id), "items": items, "count": len(items)}


@router.post("/{review_id}/duplicate-clusters/{cluster_id}:resolve", operation_id="resolve_duplicate_cluster")
def resolve_cluster(actor: ActorDep, review_id: uuid.UUID, cluster_id: uuid.UUID, body: ResolveBody) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return dedup_svc.resolve_cluster(
            session,
            actor,
            review_id,
            cluster_id,
            resolution_type=body.resolution_type,
            rationale=body.rationale,
            survivor_record_id=body.survivor_record_id,
            survivorship=body.survivorship,
        )


@router.get("/{review_id}/duplicate-clusters/{cluster_id}/resolutions", operation_id="get_resolution_chain")
def resolution_chain(actor: ActorDep, review_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
    """The full append-only chain — the merge -> split -> merge evidence."""
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        items = dedup_svc.resolution_chain(session, review_id, cluster_id)
        return {"cluster_id": str(cluster_id), "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# ER-004 — assignments, blinded screening, conflicts
# ---------------------------------------------------------------------------


@router.post("/{review_id}/assignments", status_code=status.HTTP_201_CREATED, operation_id="create_assignments")
def create_assignments(actor: ActorDep, review_id: uuid.UUID, body: AssignmentBody) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.create_assignments(
            session,
            actor,
            review_id,
            stage_id=body.stage_id,
            round_number=body.round_number,
            reviewer_subject_ids=body.reviewer_subject_ids,
            corpus_version_id=body.corpus_version_id,
        )


@router.get("/{review_id}/screening/queue", operation_id="get_screening_queue")
def screening_queue(
    actor: ActorDep,
    review_id: uuid.UUID,
    stage_id: Annotated[uuid.UUID, Query()],
    round_number: Annotated[int, Query(ge=1, le=MAX_ROUND_NUMBER)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """L3 + L4. Only the caller's own assignments and their own decisions.

    ## Which numbers describe the PAGE and which describe the STAGE

    This route paginates — `limit` defaults to 50 and is capped at 200 — so every
    counter in the body has to say which population it is over. There is no
    response model to carry that (`dict[str, Any]`), so it is stated here:

    | field | scope |
    |---|---|
    | `items`, `count` | **this page**. `count` is `len(items)`. |
    | `next_cursor` | **this page**: the key to ask for the next one, `null` on the last. |
    | `total`, `assigned_to_me` | **the stage**: every assignment this caller holds in this stage and round. Equal by construction; `total` is the name `/conflicts` uses. |
    | `decided_by_me` | **the stage**: every record in it this caller has decided. |
    | `items[].my_decision` | **one item**, the caller's own current decision or `null`. |

    `decided_by_me` was page-scoped and `assigned_to_me` was not, and a client
    renders the pair as one ratio: a reviewer with 60 assignments who had decided
    every record past the first page read "0 of 60" and could not tell whether
    they were finished. Adding a counter here means choosing a column of that
    table on purpose.
    """
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.screening_queue(
            session,
            actor,
            review_id,
            stage_id=stage_id,
            round_number=round_number,
            limit=limit,
            cursor=cursor,
        )


@router.post("/{review_id}/decisions", status_code=status.HTTP_201_CREATED, operation_id="record_decision")
def record_decision(actor: ActorDep, review_id: uuid.UUID, body: DecisionBody) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.record_decision(
            session,
            actor,
            review_id,
            stage_id=body.stage_id,
            round_number=body.round_number,
            review_record_id=body.review_record_id,
            disposition=body.disposition,
            exclusion_reason_code=body.exclusion_reason_code,
            rationale=body.rationale,
            supersedes_decision_id=body.supersedes_decision_id,
        )


@router.get("/{review_id}/decisions", operation_id="list_visible_decisions")
def list_decisions(
    actor: ActorDep,
    review_id: uuid.UUID,
    review_record_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_DECISIONS_PAGE)] = DEFAULT_DECISIONS_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Every decision the CALLER may see — L1 decides, this does not restate it.

    For a reviewer that is their own decisions plus any conflict that has been
    unblinded. For a lead it is all of them.

    ## Why this is NOT the leak `/conflicts` was, and where it still is one

    Checked against the same inference, and it is a different shape. Before any
    unblinding this route discloses nothing a caller did not already do: a
    reviewer sees their own rows, and a subject who has screened nothing gets
    `{"items": [], "count": 0}` — measured — whatever anybody else has decided.
    There is no `/conflicts`-style channel where a number moves as the other
    reviewer works. The cross-reviewer view opens only through the ONE governed
    door in `ER-M005`'s policy, which requires a lead to have recorded an
    unblinding event carrying an actor and a reason. An audited disclosure is
    what `ER-004.2` asks for, so it stays.

    **The audience of that door is now declared** (`ER-M008`). L1's third clause
    used to key on the CONFLICT and never on the caller, so one unblinding opened
    that record's decisions to every reviewer in the tenant — including a third
    reviewer assigned to the same record who had not yet decided it. For them it
    was a blinding break: they read both dispositions before forming their own.

    The clause now also requires the caller to be named in the unblinding event's
    `disclosed_to_subject_ids`, which the lead supplies as `disclose_to` and which
    defaults to the lead alone. `reviewer_subject_id` is still withheld from a
    non-lead, so a named reviewer sees WHAT was decided and never by whom.

    This paragraph previously argued the residual was not fixable here, and cited
    `test_an_unblinding_opens_the_record_to_every_reviewer_in_the_tenant`. Both
    are gone. The argument was right about the fix it considered — a policy on
    `screening_decisions` cannot subquery `screening_decisions` (`infinite
    recursion detected in policy`), and scoping by ASSIGNMENT does not help
    because the pending reviewer is assigned — and a declared audience is
    neither: it is a column on `screening_unblinding_events`, whose own policy
    never mentions the decisions table. The four gates that replaced that test
    are in `tests/security/test_blinding_layers.py`.

    ## Which numbers describe the PAGE and which describe the REVIEW

    This route had NO page. `?limit=1` returned `count=10`, because the parameter
    did not exist and FastAPI dropped the unknown query string without a word —
    a worse failure than an oversized response, because a client that believes it
    bounded the read did not, and nothing in the body said otherwise. For a lead
    on a real dual review the whole population is `records x reviewers` decisions,
    in one body, on a synchronous route.

    `limit`/`offset` and the counters are `/records`' shape and `has_more` is
    `/conflicts`', both siblings; there is deliberately no third convention here.

    | field | scope |
    |---|---|
    | `items`, `count` | **this page**. `count` is `len(items)`. |
    | `offset` | **this page**: echoed back, so a client paging by arithmetic can see where it landed. |
    | `total` | **the review**: every decision this caller may see under L1 and L2, counted before the page is cut. |
    | `has_more` | **the review**: whether anything was left past this page. Derivable from `total` and stated anyway — `/conflicts` documents what it costs to make a caller do that arithmetic. |

    The page is cut in PYTHON, after the read, and that limit is stated rather
    than hidden: L2's rule — you may not read another reviewer's decision on a
    record you still owe — is applied to the rows after they come back, so a SQL
    `LIMIT` would page a set that has not been filtered yet and would return
    short pages indistinguishable from the end of the list. This bounds the
    RESPONSE, not the query. `/records` cuts its page the same way.
    """
    from application.services import blinded_reads

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        visible = blinded_reads.visible_decisions(
            session, review_id=review_id, actor=actor, review_record_id=review_record_id
        )
        page = visible[offset : offset + limit]
        return {
            "items": [i.to_public() for i in page],
            "count": len(page),
            "total": len(visible),
            "offset": offset,
            "has_more": offset + len(page) < len(visible),
        }


@router.get("/{review_id}/conflicts", operation_id="list_conflicts")
def list_conflicts(
    actor: ActorDep, review_id: uuid.UUID, limit: Annotated[int, Query(ge=1, le=500)] = 200
) -> dict[str, Any]:
    """**Lead only**, like `/flow` and the export routes.

    The gate is inside `svc.list_conflicts` rather than here, matching
    `:unblind` and `:resolve` in the same module — see that function for why the
    mere EXISTENCE of a conflict is a disposition leak to a second reviewer and
    a progress meter to a third.
    """
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.list_conflicts(session, actor, review_id, limit=limit)


@router.post("/{review_id}/conflicts/{conflict_id}:unblind", operation_id="unblind_conflict")
def unblind_conflict(
    actor: ActorDep, review_id: uuid.UUID, conflict_id: uuid.UUID, body: UnblindBody
) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.unblind_conflict(
            session, actor, review_id, conflict_id, reason=body.reason, disclose_to=body.disclose_to
        )


@router.post("/{review_id}/conflicts/{conflict_id}:resolve", operation_id="adjudicate_conflict")
def adjudicate(actor: ActorDep, review_id: uuid.UUID, conflict_id: uuid.UUID, body: AdjudicateBody) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.adjudicate(
            session,
            actor,
            review_id,
            conflict_id,
            disposition=body.disposition,
            rationale=body.rationale,
            exclusion_reason_code=body.exclusion_reason_code,
            supersedes_adjudication_id=body.supersedes_adjudication_id,
        )


@router.get("/{review_id}/stages", operation_id="list_stages")
def list_stages(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import select

    from models.reviews import ReviewStage

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(ReviewStage).where(ReviewStage.review_id == review_id).order_by(ReviewStage.stage_order)
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "stage_id": str(s.stage_id),
                    "stage_key": s.stage_key,
                    "stage_order": s.stage_order,
                    "name": s.name,
                    "reviewers_required": s.reviewers_required,
                }
                for s in rows
            ],
            "count": len(rows),
        }
