"""`/api/reviews/{id}/flow` — the derived PRISMA flow ledger (`ER-005`)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from api.dependencies import ActorDep
from application.services import flow as svc
from application.services import flow_audit as audit_svc
from application.services import reviews as review_svc
from database import actor_scope

router = APIRouter(prefix="/api/reviews", tags=["Flow"])


@router.get("/{review_id}/flow", operation_id="get_review_flow")
def get_flow(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    """Derived on every read. **Lead only, and only while they owe nothing.**

    Not served from the projection: `01-…:512` requires a stale projection to be
    *visible* while authoritative history stays queryable, and the cheapest way
    to guarantee that is to never serve the cache as the answer. The cache is
    reported alongside, with `projection_stale` when the two disagree.

    Lead-only is a blinding decision, not a permissions convenience. The
    derivation counts EVERY reviewer's decisions, so serving it to a reviewer
    would leave two bad options: derive from what that reviewer can see — a
    ledger that conserves internally while reporting numbers no history
    supports — or derive from everything, and hand reviewer B a
    `records_screened` that tells them exactly how far reviewer A has got. Both
    are the failure `ER-004.2` names for counts. PRISMA numbers are a reporting
    artifact and belong to the lead.

    **AND LEAD-ONLY WAS NOT ENOUGH, WHICH IS WHY THERE IS A SECOND GATE.** That
    argument is sound and its premise was false: `require_review_lead` grants on
    `reviews.created_by_subject_id` as well as on the realm role, so "the lead"
    is not a role here and the creator of a review may also be screening it.
    Measured on `ef028ab`, this route handed a reviewer who had decided nothing
    the settled disposition and the exclusion reason of the record they were
    about to judge — the exact leak the paragraph above names, reached through
    the mitigation the paragraph above claims. `flow.require_no_owed_decisions`
    is the fix, it is held in the service, and its docstring records what is
    suppressed and what is deliberately still exposed.
    """
    with actor_scope(actor) as session:
        review = review_svc.load_review(session, review_id)
        review_svc.require_review_lead(actor, review)
        # The owed-guard is INSIDE `compute_flow`, not called here: a gate a
        # router holds looks identical to no gate at all when the service is
        # read, which is the argument `require_review_lead_for` and
        # `rebuild_flow` both make about this very module.
        return svc.compute_flow(session, actor, review_id, persist=False)


@router.post("/{review_id}/flow:rebuild", operation_id="rebuild_review_flow")
def rebuild_flow(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    """Recompute from zero; the hash must reproduce (`01-…:510`). Lead only.

    Subject to the same owed-guard as `GET /flow`, and needs it more: this one
    PERSISTS the projection it returns, so gating only the read would have left
    a write path handing back the identical body. It inherits the gate by
    calling `compute_flow`.
    """
    with actor_scope(actor) as session:
        review = review_svc.load_review(session, review_id)
        review_svc.require_review_lead(actor, review)
        return svc.rebuild_flow(session, actor, review_id)


@router.post("/{review_id}/flow:audit", operation_id="audit_review_flow")
def audit_flow(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    """Hold the live history against every seal. Lead only. **Writes nothing.**

    Deliberately NOT the same operation as `flow:rebuild`, and deliberately not a
    change to it. `flow:rebuild` detects cache drift and corrects it, which means
    the first rebuild after an input is rewritten both reports the divergence and
    destroys the projection that proved it. This one derives, compares against the
    append-only sealed exports, and persists nothing — so it can be re-run, run by
    a scheduler, and run twice by two people who get the same answer.

    `POST` rather than `GET` only for symmetry with `flow:rebuild` and the rest of
    the `:verb` operations in this service; it is a read.

    The owed-guard is called HERE rather than inherited, and this is the one
    ledger route where that is true: `flow_audit.audit_flow` derives through
    `flow._collect` directly instead of through `flow.compute_flow`, so there is
    no shared gate for it to inherit. It reports each seal's counts against the
    live ones, which is the same disclosure `GET /flow` makes and then some.
    """
    with actor_scope(actor) as session:
        review = review_svc.load_review(session, review_id)
        review_svc.require_review_lead(actor, review)
        svc.require_no_owed_decisions(session, actor, review_id, "auditing the flow ledger")
        return audit_svc.audit_flow(session, actor, review_id)
