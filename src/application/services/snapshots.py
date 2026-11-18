"""`ER-007` — building, reading and reproducing an evidence snapshot.

`14-…:343-367`. A snapshot names the exact evidence versions one analysis or
publication run was entitled to read, so that the run can be re-derived later
and get the same answer. `ER-M021` stores it; `domain/snapshot_manifest.py`
decides what its bytes are; this module is the command.

## ⚠️ The build is SYNCHRONOUS today, and the job row is not decoration

`14-…:352` says the endpoint "creates an idempotent durable build job", and
`FND-006` shipped the substrate for exactly that. This module creates a real
`durable_jobs` row of type `evidence_snapshot`, so `GET /review-jobs/{job_id}`
and `POST /review-jobs/{job_id}:cancel` work as the trace table requires, and so
idempotency is the queue's rather than this module's. It then **executes the
build in the same transaction** and finishes the job, rather than leaving it for
a worker.

That is a deliberate limit with a named cause. A job that a worker picks up
later must be able to find its INPUT, and this job's input is the caller's
list of exact member pins. `13-…:§7.3` says where such an input belongs —
"Large inputs and outputs are immutable artifact refs" — and the artifact store
is `FND-009`, which does not exist. The alternatives were both worse:

* putting the pin list in `durable_jobs.progress` violates
  `ck_durable_jobs_progress_is_minimal`, and would violate it for a good
  reason — that column is bounded so a queue row can never become a payload
  store;
* inventing a fourth table to hold request bodies adds a schema nobody
  specified, for a worker that does not exist yet.

So the honest shape is: the job is real, the idempotency is real, the endpoints
are real, and the execution is inline until `FND-009` gives the request
somewhere to live. `README.md` says so beside the route. Nothing about the
snapshot itself changes when that flips — it is already written by a function
that takes a job and commits a terminal result.

## The caller supplies the members. This service never resolves "latest"

`14-…:353` — "the operation never infers mutable 'latest' inputs" — and
`09-…:329` refuses "unaccepted inputs". Both are enforced here rather than
trusted: every requested pin is looked up, its `content_hash` is read from the
row itself and not from the request, and a version nobody accepted is refused.

A caller that could pass a hash would be able to record a snapshot member that
never existed, which is worse than recording none: it looks like verification.
This is the same argument `0018`'s acceptance witness trigger makes.

## Gates: two run, three are recorded as not having run

`14-…:345` names five. `domain/policies/snapshot_gates.py` records which exist
and which package owes each of the others, and `gates_evaluated` on the snapshot
row says which ones actually ran for THAT snapshot. A consumer reads the ledger;
it never assumes five.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from actor import Actor
from api.problem_details import (
    NotFoundError,
    StateConflictError,
    UnprocessableStateError,
    ValidationFailedError,
)
from application.services import jobs as job_svc
from application.services import reviews as review_svc
from domain import snapshot_manifest as sm
from domain.policies import snapshot_gates
from instants import utc_z
from log_config import get_logger
from models.evidence import EvidenceSnapshot, EvidenceSnapshotMember, SnapshotArtifact

logger = get_logger(__name__)

#: `intended_use` is a `canonical_type_token` in the released manifest:
#: snake-case, and refused outright if it carries one of the estate's guarded
#: tokens. Validated HERE, at the boundary, and not left to the schema.
#:
#: ⚠️ It was left to the schema, and that was a 500. `compute_content_hash`
#: validates before it hashes and raises `SchemaValidationError`, which is a
#: bare `ValueError` subclass that `app.py` registers no handler for — so
#: `{"intended_use": "meta-analysis"}`, a request a person would obviously
#: write, returned a 500 whose body was the "unevaluated properties" pile
#: naming eleven innocent fields. The DB CHECKs `…intended_use_shape` and
#: `…not_regulatory` could never fire either: `seal` runs before `session.add`.
_INTENDED_USE_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")
_GUARDED_TOKENS = re.compile(r"regulatory|legal_|normative|consolidation")

#: The job type `FND-006` registered for this operation. Named there rather than
#: here so `job_catalog.OWED_BY` could record, before this module existed, which
#: package owed the producer. Removing `evidence_snapshot` from `OWED_BY` is
#: what makes "the conversion landed" a test rather than a claim.
JOB_TYPE = "evidence_snapshot"

#: Which tables a requested member may pin, and how to read the pinned row.
#: Exactly `ER-M021.MEMBER_ENTITY_TYPES` — an independent spelling, held equal
#: by a test. The value is `(table, id_column, version_column, hash_column)`.
_MEMBER_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "research_object_version": (
        "research_object_versions",
        "research_object_version_id",
        "research_object_id",
        "content_hash",
    ),
    "extraction_dataset_version": (
        "extraction_dataset_versions",
        "extraction_dataset_version_id",
        "extraction_dataset_id",
        "content_hash",
    ),
    "observation_version": (
        "observation_versions",
        "observation_version_id",
        "observation_id",
        "content_hash",
    ),
    "review_record_version": (
        "review_record_versions",
        "review_record_version_id",
        "review_record_id",
        "content_hash",
    ),
}

#: The grammar a pinned member's row obeys, as `urn:datacompany:pinned-version-ref:1`
#: requires — and it is a URN because `urn:datacompany:stable-identity:1#/$defs/schema_ref`
#: constrains the field to `^urn:datacompany:[a-z][a-z0-9-]*:[1-9][0-9]*$`.
#:
#: ⚠️ The first draft here used per-kind HTTPS URLs
#: (`https://contracts.nonagentic.ai/evidence-review/observation-version.v1`)
#: with a comment explaining that inventing a `urn:datacompany:*` would advertise a
#: document nobody can resolve. The comment was right about the danger and wrong
#: about the remedy: the released document refuses a URL outright, so the
#: choice was never "URN or URL", it was "which released URN".
#:
#: `urn:datacompany:immutable-version:1` is the truthful answer and not a
#: placeholder. Every table this service pins from is a version table with
#: exactly that shape — `entity_id`, `version_number`, `supersedes_version_id`,
#: `content_hash` — which is what `immutable-version` defines, and it is the
#: level of description a cross-service consumer actually needs: enough to know
#: the reference is to an immutable version and not to a mutable head. A
#: per-entity document would say more, and saying more than has been released
#: is what `research_contracts` 0.4.0 was cut to repair for the flow ledger.
MEMBER_SCHEMA_REF = "urn:datacompany:immutable-version:1"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    """The manifest's instants, through the service-wide renderer."""
    rendered = utc_z(value)
    if rendered is None:  # pragma: no cover - callers never pass None
        raise ValidationFailedError("a manifest instant cannot be absent")
    return rendered


def _command_hash(review_id: uuid.UUID, intended_use: str, requested: list[dict[str, Any]]) -> str:
    """The stable identity of "build THIS snapshot".

    Over the sorted member identities and the declared use, never over the
    request as received: two callers asking for the same snapshot with their
    members in a different order are asking for one snapshot, and a hash that
    said otherwise would build two and make the review's chain branch.
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(
        {
            "review_id": str(review_id),
            "intended_use": intended_use,
            "members": sorted([f"{m['entity_type']}:{m['version_id']}" for m in requested]),
        }
    )


def _resolve_member(
    session: Session, actor: Actor, review_id: uuid.UUID, requested: dict[str, Any], at: datetime
) -> dict[str, Any]:
    """Read one pinned version out of the database and shape it as a member.

    The `content_hash` and `version_number` come from the ROW. A request that
    supplied them would be allowed to describe a version that never existed —
    see the module docstring.
    """
    entity_type = str(requested.get("entity_type") or "").strip()
    if entity_type not in _MEMBER_SOURCES:
        raise ValidationFailedError(
            f"'{entity_type}' is not a pinnable member kind; this service can pin {', '.join(sorted(_MEMBER_SOURCES))}"
        )
    raw_version_id = str(requested.get("version_id") or "").strip()
    try:
        version_id = uuid.UUID(raw_version_id)
    except ValueError as exc:
        raise ValidationFailedError(f"member version_id {raw_version_id!r} is not a uuid") from exc

    table, id_column, entity_column, hash_column = _MEMBER_SOURCES[entity_type]
    row = (
        session.execute(
            text(
                f"SELECT {id_column} AS version_id, {entity_column} AS entity_id, "  # noqa: S608
                f"       version_number, {hash_column} AS content_hash "
                f"  FROM {table} WHERE {id_column} = :vid AND review_id = :rid"
            ),
            {"vid": version_id, "rid": review_id},
        )
        .mappings()
        .one_or_none()
    )

    if row is None:
        # NOT a 404: the review was already resolved and is visible, so this is
        # a caller naming a version that is not part of it. `422` says "your
        # request cannot be carried out", which is the truth; `404` would send
        # the caller looking for a routing problem.
        raise UnprocessableStateError(f"{entity_type} {version_id} does not belong to review {review_id}")

    _require_accepted(session, review_id, entity_type, version_id)

    return sm.pin(
        entity_type=entity_type,
        entity_id=str(row["entity_id"]),
        version_id=str(row["version_id"]),
        version_number=int(row["version_number"]),
        schema_ref=MEMBER_SCHEMA_REF,
        content_hash=str(row["content_hash"]),
        tenant_id=str(actor.tenant_id),
        resolved_at=_iso(at),
    )


def _require_accepted(session: Session, review_id: uuid.UUID, entity_type: str, version_id: uuid.UUID) -> None:
    """Refuse a member nobody accepted. `09-…:329` — "rejects … unaccepted inputs".

    ⚠️ This function exists because the module docstring claimed it did before
    it was written. The claim was caught by the integration suite's author
    rather than by a reader, which is the lucky version of
    `a-repo-doc-can-promise-what-the-code-reverts` — a snapshot that pinned an
    unaccepted observation would have been sealed, hashed, published and
    reproducible, and every one of those properties would have been true of the
    wrong evidence.

    "Accepted" is the TAIL of the supersession chain — the node with NO
    successor — not the existence of a row and not the newest `decided_at`.
    The first draft ordered by `decided_at DESC LIMIT 1`, which coincides with
    the tail only by luck: `decided_at` defaults to `now()`, which in Postgres
    is `transaction_timestamp()` and is CONSTANT within a transaction, so two
    chain nodes written together tie and the limit picks arbitrarily. `NOT
    EXISTS (successor)` is the tail by definition and cannot tie.

    It is the existence of a row. `evidence.list_acceptances` says so outright — there is no
    `is_current` column, and an `accept` later superseded by a `withdraw` is
    history rather than state. Reading any-row-with-decision-accept would let a
    withdrawn version back into a snapshot, silently, and the snapshot would
    look exactly like a correct one.

    `review_record_version` is exempt and that is deliberate: it is not in
    `ACCEPTANCE_SUBJECTS`, so no acceptance can ever exist for it. Its warrant
    is transitive — it is pinned because a sealed corpus version admitted it and
    an accepted research object cites it — and demanding a direct acceptance
    would refuse every source pin in the system.
    """
    from models.evidence import ACCEPTANCE_SUBJECTS

    if entity_type not in ACCEPTANCE_SUBJECTS:
        return

    decision = session.execute(
        text(
            """
            WITH RECURSIVE chain AS (
              SELECT acceptance_id, supersedes_acceptance_id, decision, decided_at
                FROM evidence_acceptances
               WHERE review_id = :rid AND subject_version_id = :vid
                 AND supersedes_acceptance_id IS NULL
              UNION ALL
              SELECT a.acceptance_id, a.supersedes_acceptance_id, a.decision, a.decided_at
                FROM evidence_acceptances a
                JOIN chain c ON a.supersedes_acceptance_id = c.acceptance_id
               WHERE a.review_id = :rid
            )
            SELECT c.decision
              FROM chain c
             WHERE NOT EXISTS (
                     SELECT 1 FROM evidence_acceptances s
                      WHERE s.supersedes_acceptance_id = c.acceptance_id
                   )
            """
        ),
        {"rid": review_id, "vid": version_id},
    ).scalar_one_or_none()

    if decision != "accept":
        raise UnprocessableStateError(
            f"{entity_type} {version_id} is not currently accepted "
            f"({'never accepted' if decision is None else f'latest decision is {decision}'}); "
            "a snapshot pins what the review agreed to, not what it happens to hold"
        )


def _run_gates(session: Session, actor: Actor, review_id: uuid.UUID) -> tuple[list[str], uuid.UUID]:
    """The two gates that exist. Returns `(gates that ran, locked protocol id)`.

    Both refusals are `422`: the request is well-formed and the review is not in
    a state that admits it. Neither is a `409`, because nothing about the
    snapshot conflicts — the review does.
    """
    from application.services import flow as flow_svc

    # `uncommitted_protocol`. `_locked_protocol_id` refuses an unlocked protocol
    # and is the same function `decide_acceptance` uses, deliberately: a
    # snapshot must be taken under the protocol its members were accepted under,
    # and two different notions of "committed" would eventually disagree.
    from application.services.evidence import _locked_protocol_id

    protocol_version_id = _locked_protocol_id(session, review_id)

    # `unresolved_conflicts`. Unfiltered — the actor-aware variant exists to
    # avoid telling a reviewer that a conflict exists, and a snapshot gate must
    # see every conflict regardless of who asked.
    unresolved = flow_svc._unresolved_conflict_ids(session, review_id)
    if unresolved:
        raise UnprocessableStateError(
            f"review {review_id} has {len(unresolved)} unresolved conflict(s); "
            "a snapshot taken over an open disagreement records a result nobody agreed"
        )

    return snapshot_gates.evaluated_gates(), protocol_version_id


def request_snapshot(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    intended_use: str,
    members: list[dict[str, Any]],
    idempotency_key: str | None = None,
) -> tuple[EvidenceSnapshot, uuid.UUID]:
    """Build one snapshot. Returns `(snapshot, job_id)`.

    Idempotent through the job queue: the same members and the same declared use
    produce the same `command_hash`, `jobs.enqueue` returns the EXISTING job, and
    this function then returns the snapshot that job already built. So a
    duplicate request — a retry, a replay, a second worker — yields one snapshot
    and one version, which is `14-…:367`'s fourth completion criterion.
    """
    review = review_svc.load_review(session, review_id)
    subject = review_svc.require_review_lead(actor, review)

    use = str(intended_use or "").strip()
    if not use:
        raise ValidationFailedError("intended_use is required; a snapshot must say what it is for")
    if not _INTENDED_USE_SHAPE.match(use):
        raise ValidationFailedError(
            f"intended_use {use!r} must be a lowercase snake_case token "
            "(the released manifest types it as a canonical_type_token); "
            "'meta-analysis' is spelled 'meta_analysis'"
        )
    if _GUARDED_TOKENS.search(use):
        raise ValidationFailedError(
            f"intended_use {use!r} carries a guarded token; a snapshot may not name itself "
            "as an authority the ADR has not granted"
        )
    if not members:
        raise ValidationFailedError(
            "a snapshot names at least one member; an empty snapshot pins nothing and cannot make a run reproducible"
        )

    # ⚠️ Taken BEFORE the gates, not inside `_build`.
    #
    # It was inside `_build` first, which serialised the version-number read and
    # nothing else — so `_run_gates` could observe an acceptance as current
    # while a concurrent `decide_acceptance` was committing its withdrawal. The
    # two writes touch different tables, so nothing conflicted, and the snapshot
    # committed pinning a version that had just been withdrawn. Worse than a
    # plain race: `reproduce_manifest` re-derives from the frozen member rows
    # and would report that snapshot as reproducing perfectly, forever.
    #
    # `evidence.decide_acceptance` takes the SAME lock, which is what actually
    # closes it — a lock only one of two writers holds orders nothing.
    review_svc.lock_review(session, review_id)

    at = _now()
    resolved = [_resolve_member(session, actor, review_id, m, at) for m in members]
    seen = {(m["entity_type"], m["entity_id"]) for m in resolved}
    if len(seen) != len(resolved):
        raise ValidationFailedError(
            "a snapshot names each entity once; two versions of one entity in a single "
            "snapshot would make the manifest ambiguous about which one a run may read"
        )

    command_hash = _command_hash(review_id, use, resolved)
    job = job_svc.enqueue(
        session,
        actor,
        review_id=review_id,
        job_type=JOB_TYPE,
        command_hash=command_hash,
        idempotency_key=idempotency_key or command_hash.removeprefix("sha256:")[:64],
    )

    existing = session.execute(
        select(EvidenceSnapshot).where(EvidenceSnapshot.job_id == job.job_id)
    ).scalar_one_or_none()
    if existing is not None:
        # The job already built it. Returning the same snapshot is the whole
        # point of routing through the queue — see the docstring.
        return existing, job.job_id

    gates, protocol_version_id = _run_gates(session, actor, review_id)
    snapshot = _build(
        session,
        actor,
        review_id=review_id,
        job=job,
        subject=subject,
        intended_use=use,
        members=resolved,
        gates=gates,
        protocol_version_id=protocol_version_id,
        at=at,
    )

    # The job is finished by the same transaction that wrote the snapshot. There
    # is no ordering of failures that leaves a succeeded job with no snapshot,
    # or a snapshot whose job still says it is running.
    # `claim`, NOT `claim_next`. See `jobs.claim`: the first draft here called
    # `claim_next` on the assumption that the job it had just enqueued was the
    # one it would get back, and `claim_next` returns the OLDEST runnable row in
    # the tenant. Under two concurrent snapshot requests this claimed the wrong
    # job and then failed in `finish`, which refuses a non-holder — a defect
    # invisible to every single-request test.
    job_svc.claim(session, job.job_id, worker_id="inline")
    job_svc.finish(
        session,
        job.job_id,
        worker_id="inline",
        outcome="succeeded",
        result_summary={"snapshot_id": str(snapshot.snapshot_id), "members": snapshot.member_count},
    )
    return snapshot, job.job_id


def _build(
    session: Session,
    actor: Actor,
    *,
    review_id: uuid.UUID,
    job: Any,
    subject: str,
    intended_use: str,
    members: list[dict[str, Any]],
    gates: list[str],
    protocol_version_id: uuid.UUID,
    at: datetime,
) -> EvidenceSnapshot:
    """Write the snapshot, its members and its artifact row in one transaction."""
    previous = session.execute(
        select(EvidenceSnapshot)
        .where(EvidenceSnapshot.review_id == review_id)
        .order_by(EvidenceSnapshot.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()

    version_number = 1 if previous is None else previous.version_number + 1
    supersedes = None if previous is None else previous.snapshot_id

    snapshot_id = uuid.uuid4()

    # The version chain shares ONE entity identity: `14-…:356` says changed
    # evidence creates a new snapshot with an impact link, which is a new
    # VERSION of the same thing rather than a new thing. That identity is the
    # ROOT snapshot's id, so for version 1 it is this snapshot's own.
    #
    # ⚠️ It was `review_id` in the first draft, and the reproduction check
    # caught it within minutes: `reproduce_manifest` derives the entity by
    # walking to the root, so a snapshot built under one rule and read under
    # the other hashed differently and every GET returned 409. The bug was
    # real beyond the mismatch — a review's id is the review's identity, and
    # reusing it here would have made a snapshot chain and its review
    # indistinguishable to any consumer that keys on `entity_id`.
    entity_id = snapshot_id if previous is None else _entity_of(session, previous)
    member_digest = sm.member_manifest_hash(members)
    corpus_version_id = _corpus_of(session, review_id)

    manifest = sm.canonical_manifest(
        snapshot_id=str(snapshot_id),
        entity_id=str(entity_id),
        review_id=str(review_id),
        tenant_id=str(actor.tenant_id),
        version_number=version_number,
        supersedes_snapshot_id=None if supersedes is None else str(supersedes),
        intended_use=intended_use,
        created_at=_iso(at),
        created_by=subject,
        members=members,
        member_manifest_digest=member_digest,
        protocol_pin=None,
        corpus_pin=None,
    )
    sealed, content_hash = sm.seal(manifest)
    payload = sm.canonical_bytes(sealed)

    snapshot = EvidenceSnapshot(
        snapshot_id=snapshot_id,
        tenant_id=actor.tenant_id,
        review_id=review_id,
        version_number=version_number,
        supersedes_snapshot_id=supersedes,
        schema_ref=sm.MANIFEST_SCHEMA_REF,
        intended_use=intended_use,
        protocol_version_id=protocol_version_id,
        corpus_version_id=corpus_version_id,
        member_count=len(members),
        member_manifest_hash=member_digest,
        member_ordering_rule=sm.PINNED_REF_ORDERING_RULE,
        content_hash=content_hash,
        gates_evaluated=gates,
        job_id=job.job_id,
        created_by_subject_id=subject,
        created_at=at,
    )
    session.add(snapshot)

    for member in members:
        session.add(
            EvidenceSnapshotMember(
                member_id=uuid.uuid4(),
                tenant_id=actor.tenant_id,
                snapshot_id=snapshot_id,
                owner_context=member["owner_context"],
                entity_type=member["entity_type"],
                entity_id=uuid.UUID(member["entity_id"]),
                version_id=uuid.UUID(member["version_id"]),
                version_number=member["version_number"],
                member_schema_ref=member["schema_ref"],
                content_hash=member["content_hash"],
                created_at=at,
            )
        )

    import hashlib

    session.add(
        SnapshotArtifact(
            artifact_id=uuid.uuid4(),
            tenant_id=actor.tenant_id,
            snapshot_id=snapshot_id,
            artifact_kind=sm.CANONICAL_MANIFEST_ARTIFACT,
            media_type=sm.CANONICAL_MANIFEST_MEDIA_TYPE,
            payload_digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            byte_size=len(payload),
            serializer_version=sm.SERIALIZER_VERSION,
            created_at=at,
        )
    )
    session.flush()

    from application.services import events

    events.emit_evidence_snapshot_created(session, snapshot)
    logger.info(
        "snapshot.created",
        snapshot_id=str(snapshot_id),
        review_id=str(review_id),
        members=len(members),
        gates=gates,
    )
    return snapshot


def _entity_of(session: Session, previous: EvidenceSnapshot) -> uuid.UUID:
    """The stable entity id shared by every version in a review's snapshot chain.

    Walks to the root rather than storing a column: `ER-M021` already enforces
    exactly one root per review and one successor per node, so the root IS the
    identity and a second column would be a denormalisation that can disagree
    with the chain it summarises.
    """
    row = session.execute(
        text(
            """
            WITH RECURSIVE chain AS (
              SELECT snapshot_id, supersedes_snapshot_id FROM evidence_snapshots
               WHERE snapshot_id = :sid
              UNION ALL
              SELECT s.snapshot_id, s.supersedes_snapshot_id
                FROM evidence_snapshots s JOIN chain c ON s.snapshot_id = c.supersedes_snapshot_id
            )
            SELECT snapshot_id FROM chain WHERE supersedes_snapshot_id IS NULL
            """
        ),
        {"sid": previous.snapshot_id},
    ).scalar_one_or_none()
    return row or previous.snapshot_id


def _corpus_of(session: Session, review_id: uuid.UUID) -> uuid.UUID | None:
    """The review's most recent sealed corpus version, or `None` if it has none."""
    return session.execute(
        text(
            "SELECT corpus_version_id FROM corpus_versions WHERE review_id = :rid  ORDER BY version_number DESC LIMIT 1"
        ),
        {"rid": review_id},
    ).scalar_one_or_none()


def load_snapshot(session: Session, review_id: uuid.UUID, snapshot_id: uuid.UUID) -> EvidenceSnapshot:
    snapshot = session.execute(
        select(EvidenceSnapshot).where(
            EvidenceSnapshot.snapshot_id == snapshot_id,
            EvidenceSnapshot.review_id == review_id,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise NotFoundError(f"snapshot {snapshot_id} was not found in review {review_id}")
    return snapshot


def reproduce_manifest(session: Session, snapshot: EvidenceSnapshot) -> dict[str, Any]:
    """Rebuild the manifest from the stored rows, and refuse if it does not match.

    `14-…:364`'s first completion criterion — "the snapshot reconstructs
    byte-identical canonical JSON … under the same serializer version" — is
    checked here on EVERY read rather than in a test only. A stored blob could
    only be handed back; this is re-derived from the pinned members, so a member
    row that drifted surfaces as a refusal at the moment somebody tries to rely
    on it, which is the only moment it matters.

    `resolved_at` is reconstructed from the snapshot's `created_at` rather than
    stored per member. It is `x-datacompany-hash-excluded`, so it cannot affect
    either digest; storing a per-member observation timestamp would add a column
    whose only effect is to make the row bigger.
    """
    rows = session.execute(
        select(EvidenceSnapshotMember)
        .where(EvidenceSnapshotMember.snapshot_id == snapshot.snapshot_id)
        .order_by(
            EvidenceSnapshotMember.owner_context,
            EvidenceSnapshotMember.entity_type,
            EvidenceSnapshotMember.entity_id,
            EvidenceSnapshotMember.version_id,
        )
    ).scalars()
    at = snapshot.created_at
    members = [
        sm.pin(
            entity_type=row.entity_type,
            entity_id=str(row.entity_id),
            version_id=str(row.version_id),
            version_number=row.version_number,
            schema_ref=row.member_schema_ref,
            content_hash=row.content_hash,
            tenant_id=str(row.tenant_id),
            resolved_at=_iso(at),
        )
        for row in rows
    ]

    manifest = sm.canonical_manifest(
        snapshot_id=str(snapshot.snapshot_id),
        entity_id=str(_entity_of(session, snapshot)),
        review_id=str(snapshot.review_id),
        tenant_id=str(snapshot.tenant_id),
        version_number=snapshot.version_number,
        supersedes_snapshot_id=(
            None if snapshot.supersedes_snapshot_id is None else str(snapshot.supersedes_snapshot_id)
        ),
        intended_use=snapshot.intended_use,
        created_at=_iso(at),
        created_by=snapshot.created_by_subject_id,
        members=members,
        member_manifest_digest=snapshot.member_manifest_hash,
        protocol_pin=None,
        corpus_pin=None,
    )
    sealed, recomputed = sm.seal(manifest)
    if recomputed != snapshot.content_hash:
        raise StateConflictError(
            f"snapshot {snapshot.snapshot_id} does not reproduce: stored {snapshot.content_hash}, "
            f"recomputed {recomputed}. Its members or its serializer have moved since it was sealed."
        )
    return sealed
