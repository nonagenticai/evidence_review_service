"""`/api/reviews/{id}/exports` — the hash-sealed inclusion set (`ER-009`)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from api.dependencies import ActorDep
from application.services import flow as svc
from application.services import reviews as review_svc
from database import actor_scope

router = APIRouter(prefix="/api/reviews", tags=["Exports"])

# EVERY ROUTE HERE IS LEAD-ONLY, including the reads.
#
# `create_export` was gated inside `flow.create_export`, which made the write
# look guarded and left the three reads open: `load_review` only proves the
# review is visible to the caller's tenant, and nothing else was asked. So the
# ledger `GET /flow` refuses a reviewer with 403 was served to that same reviewer
# through `GET /exports/{id}`, whose payload carries `manifest.counts` verbatim —
# `records_screened`, `excluded`, `exclusion_reasons` — plus an `omissions` array
# naming every record's terminal bucket and exclusion reason. Blinding is only
# ever as strong as its weakest door, and this was a second door onto the same
# room with no lock on it.
#
# `list_exports` is lead-only too, and that is a deliberate call rather than
# symmetry for its own sake: the EXISTENCE of an export is itself progress
# information. An export can only be taken once every record is screened and
# every conflict resolved, so a reviewer who can see one appear learns that the
# other reviewer has finished — the exact inference `ER-004.2` names for counts,
# arriving as a row count instead of a number. An empty list would leak the
# complement just as reliably.
#
# The gate is `require_review_lead`, not `require_lead`: the person who created
# the review governs it whether or not a realm role was ever granted, which is
# the same authority `create_export` already uses.
#
# ⛔ AND "LEAD-ONLY" IS NOT A BLINDING BOUNDARY, WHICH IS THE SECOND GATE.
#
# The paragraph above is sound about WHAT leaks and wrong about what stops it,
# in exactly the way `routers/flow.py`'s docstring was: `require_review_lead`
# grants on `reviews.created_by_subject_id`, so the creator of a review reaches
# every route in this file while still owing a screening decision on the records
# it counts. Measured on `5442ad5`, `POST /exports` returned 201 to a caller
# `GET /flow` had just refused with 403, and `GET /exports/{id}` then served the
# identical counts — permanently, because the row is append-only.
#
# So `flow.require_export_audience` holds BOTH gates, the query for both reads
# moved into `flow.list_exports` / `flow.get_export` with it, and every route
# below is now a call. That function's docstring carries the decision about
# seals created BEFORE the fix and why redacting a sealed payload was rejected.
#
# `create_export`'s own `load_review` stays where it was — it is redundant
# against the service's identity-mapped `session.get`, and it is what makes a
# review in another tenant a 404 before anything else is asked. The reads reach
# the same `load_review` through `require_export_audience`.


@router.post("/{review_id}/exports", status_code=status.HTTP_201_CREATED, operation_id="create_export")
def create_export(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.create_export(session, actor, review_id)


@router.get("/{review_id}/exports", operation_id="list_exports")
def list_exports(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        return svc.list_exports(session, actor, review_id)


# DECLARED BEFORE `get_export`, deliberately. FastAPI matches routes in
# declaration order, and `/{export_id}` with a UUID type would try to parse
# `<uuid>:verify` as a UUID first, fail validation and return 422 — so the
# verify route would be unreachable while looking perfectly well-formed.
@router.get("/{review_id}/exports/{export_id}:verify", operation_id="verify_export")
def verify_export(actor: ActorDep, review_id: uuid.UUID, export_id: uuid.UUID) -> dict[str, Any]:
    """Recompute both hashes from the stored payload (`DM-AT-29`).

    The supplied hash is never trusted — it is the thing being checked. A tamper
    of one byte in `payload` fails `payload_matches`; a change that alters the
    envelope's meaning fails `manifest_matches` as well.
    """
    with actor_scope(actor) as session:
        return svc.verify_export(session, actor, review_id, export_id)


@router.get("/{review_id}/exports/{export_id}", operation_id="get_export")
def get_export(
    actor: ActorDep,
    review_id: uuid.UUID,
    export_id: uuid.UUID,
    with_payload: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    with actor_scope(actor) as session:
        return svc.get_export(session, actor, review_id, export_id, with_payload=with_payload)
