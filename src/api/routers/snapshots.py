"""`/api/reviews/{id}/evidence-snapshots` — the immutable analysis input set (`ER-007`).

`14-…:352-360`. A snapshot names the exact evidence versions one run was
entitled to read. `POST` builds one from pins the caller supplies; `GET` returns
it, **reproduced from its stored members rather than replayed from a cache**.

## Why the read reproduces instead of serving what was stored

`14-…:364`'s first completion criterion is that the snapshot "reconstructs
byte-identical canonical JSON … under the same serializer version".
`snapshots.reproduce_manifest` rebuilds the manifest from the member rows and
refuses if the recomputed hash differs from the sealed one, so that criterion is
checked on every read and not only in a test.

Serving a stored blob would satisfy the letter and lose the point: a stored copy
answers "what did we say" where a consumer needs "what is true". The append-only
triggers make drift unlikely; this makes it *visible*, which is a different
property and the one somebody publishing against a snapshot actually needs.

## Both routes are LEAD-ONLY, including the read

`14-…:352` requires "the authenticated snapshot-preparer permission" for
creation. No separate read permission is specified, and this service has no
`snapshot-preparer` role — `require_review_lead` is the nearest real authority
and it is the same gate `create_export` uses.

The read is lead-only for the reason `exports.py` records at length about its
own reads: a snapshot's MEMBER LIST is a complete statement of what survived
screening, which is the same inference `ER-004.2` names for counts, arriving as
a list of ids instead of a number. Blinding is only ever as strong as its
weakest door.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, status
from sqlalchemy import select

from api.dependencies import ActorDep
from application.services import reviews as review_svc
from application.services import snapshots as svc
from database import actor_scope
from instants import utc_z
from models.evidence import EvidenceSnapshot, SnapshotArtifact

router = APIRouter(prefix="/api/reviews", tags=["Evidence snapshots"])


def _render(snapshot: EvidenceSnapshot, artifacts: list[SnapshotArtifact]) -> dict[str, Any]:
    """Metadata and artifact refs. `14-…:358` — never the evidence itself."""
    from domain.policies import snapshot_gates

    gates = list(snapshot.gates_evaluated or [])
    return {
        "snapshot_id": str(snapshot.snapshot_id),
        "review_id": str(snapshot.review_id),
        "version_number": snapshot.version_number,
        "supersedes_snapshot_id": (
            None if snapshot.supersedes_snapshot_id is None else str(snapshot.supersedes_snapshot_id)
        ),
        "schema_ref": snapshot.schema_ref,
        "intended_use": snapshot.intended_use,
        "member_count": snapshot.member_count,
        "member_manifest_hash": snapshot.member_manifest_hash,
        "member_ordering_rule": snapshot.member_ordering_rule,
        "content_hash": snapshot.content_hash,
        # The ledger, and the verdict derived from it. Both, deliberately: the
        # boolean is what a consumer branches on, and the list is what tells
        # them WHY it is false without having to know this service's roadmap.
        "gates_evaluated": gates,
        "fully_gated": snapshot_gates.is_fully_gated(gates),
        "gates_not_run": sorted(set(snapshot_gates.SNAPSHOT_GATES) - set(gates)),
        "job_id": str(snapshot.job_id),
        "created_by": snapshot.created_by_subject_id,
        # `utc_z`, not `.isoformat()`. `review_jobs.py` documents having MEASURED
        # this exact defect and fixed it there; the fix was not carried across,
        # so a POST returned `…+00:00` (the fresh Python UTC value) and a GET
        # returned `…+02:00` for the same instant (the row read back in the
        # session's TimeZone) — while that same GET body carried
        # `manifest.created_at` as `…Z`. One instant, three spellings, two of
        # them in one response.
        "created_at": utc_z(snapshot.created_at),
        "artifacts": [
            {
                "artifact_kind": a.artifact_kind,
                "media_type": a.media_type,
                "payload_digest": a.payload_digest,
                "byte_size": a.byte_size,
                "serializer_version": a.serializer_version,
            }
            for a in artifacts
        ],
    }


def _artifacts(session: Any, snapshot: EvidenceSnapshot) -> list[SnapshotArtifact]:
    return list(
        session.execute(
            select(SnapshotArtifact)
            .where(SnapshotArtifact.snapshot_id == snapshot.snapshot_id)
            .order_by(SnapshotArtifact.artifact_kind)
        ).scalars()
    )


@router.post(
    "/{review_id}/evidence-snapshots",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_evidence_snapshot",
)
def create_evidence_snapshot(
    actor: ActorDep,
    review_id: uuid.UUID,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Build a snapshot from EXACT pins the caller names.

    `201` rather than `202`, and that is a deliberate difference from
    `14-…:352`'s "creates an idempotent durable build job". A durable job IS
    created and is readable at `GET /api/review-jobs/{job_id}`, but it is
    executed inline — `application/services/snapshots.py` records why, and what
    (`FND-009`) has to exist before it can be claimed by a worker. Answering
    `202` with a finished snapshot in the body would describe the operation as
    pending when it is done, which is the one thing a status code must not do.
    """
    members = payload.get("members")
    if not isinstance(members, list):
        from api.problem_details import ValidationFailedError

        raise ValidationFailedError(
            "members must be a list of {entity_type, version_id} pins; a snapshot never infers its own inputs"
        )
    with actor_scope(actor) as session:
        snapshot, job_id = svc.request_snapshot(
            session,
            actor,
            review_id,
            intended_use=str(payload.get("intended_use") or ""),
            members=members,
            idempotency_key=payload.get("idempotency_key"),
        )
        body = _render(snapshot, _artifacts(session, snapshot))
        body["job_id"] = str(job_id)
        return body


@router.get("/{review_id}/evidence-snapshots", operation_id="list_evidence_snapshots")
def list_evidence_snapshots(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.require_review_lead(actor, review_svc.load_review(session, review_id))
        rows = list(
            session.execute(
                select(EvidenceSnapshot)
                .where(EvidenceSnapshot.review_id == review_id)
                .order_by(EvidenceSnapshot.version_number)
            ).scalars()
        )
        return {"items": [_render(row, _artifacts(session, row)) for row in rows]}


@router.get("/{review_id}/evidence-snapshots/{snapshot_id}", operation_id="get_evidence_snapshot")
def get_evidence_snapshot(actor: ActorDep, review_id: uuid.UUID, snapshot_id: uuid.UUID) -> dict[str, Any]:
    """The snapshot, plus the manifest reproduced from its members.

    The manifest is returned inline rather than behind an artifact download
    because there is no artifact store to download from — `13-…:§11` /
    `FND-009`. The `artifacts` array still carries the digest and byte size, so
    a consumer can verify the bytes it received are the bytes that were sealed.
    """
    with actor_scope(actor) as session:
        review_svc.require_review_lead(actor, review_svc.load_review(session, review_id))
        snapshot = svc.load_snapshot(session, review_id, snapshot_id)
        body = _render(snapshot, _artifacts(session, snapshot))
        body["manifest"] = svc.reproduce_manifest(session, snapshot)
        return body
