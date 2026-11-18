"""Every `events.py` producer writes a payload its released schema accepts.

`13-…:§8.3` — "No service emits a listed document `09` event until its schema,
example, consumer matrix and compatibility test are merged." `research_contracts`
0.9.0 just released twelve payload documents covering the eighteen event types
this service can raise (`event_catalog.EMITTABLE`). That release only proves a
document exists. It proves nothing about the twelve `emit_*` functions in
`application/services/events.py` that actually build the dicts SQLAlchemy will
persist as `DomainOutbox.payload`.

Today those two things — the released schema and the producer's dict literal —
agree only because a human read one while writing the other. That is the exact
defect this test exists to catch: a schema and its only producer that match on
the day they are released and drift a month later, silently, because nothing
runs the producer's actual output back through the document that is supposed to
govern it. `13-…:§8.2` narrows what a payload may say (identifiers, versions,
hashes, minimal facts, never a blinded decision — see `events.py`'s own module
docstring) and the released schema is where that rule is machine-checkable; a
producer that drifts from it is a producer that has silently stopped obeying
`§8.2`, and nothing before this file would have said so.

## Why a fake session and no database

`events.py`'s contract (`FND-003`) is that these functions never commit and
never flush — the whole guarantee is that the event and the domain row it
describes reach the database in ONE transaction, decided by the caller's
`actor_scope`, not by the emitter. A stub that implements only `.add()` is not
a shortcut around that contract, it IS that contract: anything more (a real
`Session`, a live engine, `uv run alembic`) would be testing SQLAlchemy and
Postgres, not the one thing in question here, which is whether the dict a
producer builds satisfies the document it claims to satisfy.

One consequence of never flushing: two `DomainOutbox` columns —
`schema_ref` and `occurred_at` — are populated only by a column default or
`server_default` that SQLAlchemy applies at INSERT time. `_write()` never sets
either, by design (see its docstring: the two guards it does raise are
"developer convenience", not the control). So the captured row's `schema_ref`
and `occurred_at` are `None` here, exactly as they would be on any unflushed
instance, and this test synthesizes both when it builds the ENVELOPE dict
(`_envelope_from_row`) rather than reading them off the row — see the comment
there for why that is a faithful stand-in and not a fudge.

## What "matches" means, twice over

Two independent checks per event, because they can fail independently:

(a) the PAYLOAD — the dict a producer built — validates against the payload
    document `event_catalog.schema_ref_for` names for that event's type; and
(b) the ROW AS A WHOLE — reassembled into the envelope shape a consumer would
    actually receive — validates against `urn:datacompany:research-event-envelope:1`,
    the one document every event in this service is pinned to
    (`models.foundation.ENVELOPE_SCHEMA_REF`, `ck_domain_outbox_envelope_pinned`).

`§8.2`'s blinding prohibition is not re-litigated here — `events.py`'s own
docstring and `review.decision.recorded.v1`/`review.conflict.opened.v1`'s
docstrings own that argument. This file only proves the documents this service
already released are documents its own code actually produces.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from research_contracts.canonical import validate_instance

from application.services import events
from domain.policies.event_catalog import EMITTABLE, schema_ref_for
from models.foundation import ENVELOPE_SCHEMA_REF, DomainOutbox

# --------------------------------------------------------------------- fake ---


class _FakeSession:
    """Captures `.add()` calls. No database, no flush, no commit — see the
    module docstring for why that absence is the point rather than a gap."""

    def __init__(self) -> None:
        self.added: list[DomainOutbox] = []

    def add(self, row: DomainOutbox) -> None:
        self.added.append(row)


def _capture(producer: Callable[..., None], *args: Any, **kwargs: Any) -> DomainOutbox:
    """Run one `emit_*` function against a fresh fake session and return the
    single row it wrote.

    Asserting exactly one row is itself a check: `events.py`'s contract is one
    `session.add()` per call (`emit_review_lifecycle`'s early `return` for the
    `draft -> configured` non-event, and `emit_acceptance`'s `return` for a
    `withdraw`, are the only documented ways to write zero — neither is
    exercised here because every case below is a real, registered emission).
    """
    session = _FakeSession()
    producer(session, *args, **kwargs)
    assert len(session.added) == 1, f"{producer.__name__} added {len(session.added)} rows, expected exactly 1"
    return session.added[0]


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _hash(seed: str) -> str:
    """A well-formed `sha256:<hex>` content hash, per `stable-identity:1`."""
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


#: Synthesized once. `occurred_at` is a `server_default=func.now()` column
#: (`models/foundation.py`) — it is never set by client code, only by Postgres
#: at INSERT time, and this test's whole premise is that it never reaches
#: Postgres. Any value in the released `timestamp` grammar
#: (`stable-identity:1#/$defs/timestamp`, RFC 3339 UTC with a trailing `Z`)
#: proves the same thing a real one would: that the envelope's *shape* — not
#: this test's clock — is what the released document accepts.
_OCCURRED_AT = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _envelope_from_row(row: DomainOutbox) -> dict[str, Any]:
    """The envelope a consumer would receive, reassembled from `row`.

    `schema_ref` is likewise synthesized rather than read off `row`: it is a
    Python-side column `default=ENVELOPE_SCHEMA_REF`
    (`models/foundation.py`), and a column default — unlike a `server_default`
    — is applied by SQLAlchemy at flush, which this test also never triggers.
    Reading `row.schema_ref` here would silently assert `None` validates,
    which is not the claim `§8.3` cares about; the claim is that the value
    every real INSERT gets (`ENVELOPE_SCHEMA_REF`, pinned equal to the
    database's own default by `ck_domain_outbox_envelope_pinned` and by
    `test_the_event_catalogue_is_the_registry.py`) is what the row conforms
    to. Every other field below (`event_id`, `producer`, `resource_scope`,
    `tenant_id`, `aggregate_type`, `aggregate_id`, `aggregate_version`,
    `payload`, `content_hash`) IS read off `row`, because `_write()` sets each
    of those directly in its `DomainOutbox(...)` call — nothing there is
    waiting on a flush.
    """
    envelope: dict[str, Any] = {
        "event_id": str(row.event_id),
        "event_type": row.event_type,
        "schema_ref": ENVELOPE_SCHEMA_REF,
        "producer": row.producer,
        "resource_scope": row.resource_scope,
        "resource_tenant_id": str(row.tenant_id),
        "aggregate_type": row.aggregate_type,
        "aggregate_id": str(row.aggregate_id),
        "aggregate_version": row.aggregate_version,
        "occurred_at": _OCCURRED_AT,
        "payload": row.payload,
    }
    if row.content_hash is not None:
        envelope["content_hash"] = row.content_hash
    return envelope


# -------------------------------------------------------------- producer stubs ---
# One `SimpleNamespace` per domain object an `emit_*` function reads from. Every
# UUID field gets a real `uuid.uuid4()` (or, where the producer passes the value
# straight through with no `str()`, an already-canonical UUID string — see the
# per-case comment); every count gets a plausible positive int; every hash gets
# a real `sha256:<hex>` digest, because a schema that pattern-matches its inputs
# is not exercised by a fixture that never has the right shape.


def _case_review_created() -> DomainOutbox:
    review = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        # emit_review_created writes this WITHOUT str() — see events.py:181 —
        # so the stub supplies it already canonical, matching what the real
        # `Review` model's `project_id` column holds.
        project_id=str(_uuid()),
        external_key="datacompany-2026-lipid-rct-0142",
    )
    return _capture(events.emit_review_created, review)


def _lifecycle_event() -> SimpleNamespace:
    return SimpleNamespace(review_id=_uuid(), tenant_id=_uuid(), revision_number=2, state_event_id=_uuid())


def _case_lifecycle(from_state: str, to_state: str) -> Callable[[], DomainOutbox]:
    def _run() -> DomainOutbox:
        return _capture(events.emit_review_lifecycle, _lifecycle_event(), from_state=from_state, to_state=to_state)

    return _run


def _case_import_completed() -> DomainOutbox:
    batch = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        import_batch_id=_uuid(),
        accepted_count=118,
        rejected_count=6,
        quarantined_count=2,
        raw_payload_digest=_hash("import-batch-raw"),
    )
    return _capture(events.emit_import_completed, batch)


def _case_duplicate_cluster_proposed() -> DomainOutbox:
    run = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        dedup_run_id=_uuid(),
        corpus_version_id=_uuid(),
        cluster_count=14,
        pair_count=37,
        input_digest=_hash("dedup-run-input"),
    )
    return _capture(events.emit_duplicate_cluster_proposed, run)


def _case_duplicate_cluster_resolved() -> DomainOutbox:
    resolution = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        resolution_id=_uuid(),
        cluster_id=_uuid(),
        resolution_type="duplicate_confirmed",
        survivor_record_id=_uuid(),
        supersedes_resolution_id=None,
    )
    return _capture(events.emit_duplicate_cluster_resolved, resolution)


def _case_decision_recorded() -> DomainOutbox:
    decision = SimpleNamespace(decision_id=_uuid())
    return _capture(
        events.emit_decision_recorded,
        decision,
        review_id=_uuid(),
        tenant_id=_uuid(),
        stage_id=_uuid(),
        round_number=1,
    )


def _case_conflict_opened() -> DomainOutbox:
    return _capture(
        events.emit_conflict_opened,
        _uuid(),
        review_id=_uuid(),
        tenant_id=_uuid(),
        stage_id=_uuid(),
        round_number=1,
    )


def _case_conflict_resolved() -> DomainOutbox:
    adjudication = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        adjudication_id=_uuid(),
        conflict_id=_uuid(),
        disposition="include",
        supersedes_adjudication_id=None,
    )
    return _capture(events.emit_conflict_resolved, adjudication)


def _case_acceptance(subject_kind: str, *, with_hash: bool) -> Callable[[], DomainOutbox]:
    def _run() -> DomainOutbox:
        acceptance = SimpleNamespace(
            review_id=_uuid(),
            tenant_id=_uuid(),
            acceptance_id=_uuid(),
            subject_content_hash=_hash(subject_kind) if with_hash else None,
            supersedes_acceptance_id=None,
        )
        return _capture(events.emit_acceptance, acceptance, subject_kind=subject_kind, decision="accept")

    return _run


def _case_observation_superseded() -> DomainOutbox:
    observation = SimpleNamespace(observation_id=_uuid())
    version = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        observation_version_id=_uuid(),
        version_number=2,
        supersedes_version_id=_uuid(),
        content_hash=_hash("observation-version-2"),
    )
    return _capture(events.emit_observation_superseded, observation, version)


def _case_assertion_recorded() -> DomainOutbox:
    assertion = SimpleNamespace(research_assertion_id=_uuid(), assertion_key="population.total_included")
    version = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        research_assertion_version_id=_uuid(),
        version_number=1,
        supersedes_version_id=None,
        content_hash=_hash("assertion-version-1"),
    )
    return _capture(events.emit_assertion_recorded, assertion, version)


def _case_evidence_snapshot_created() -> DomainOutbox:
    snapshot = SimpleNamespace(
        review_id=_uuid(),
        tenant_id=_uuid(),
        snapshot_id=_uuid(),
        version_number=1,
        intended_use="systematic_review_export",
        member_count=482,
        member_manifest_hash=_hash("snapshot-member-manifest"),
        gates_evaluated=["completeness_check", "duplicate_resolution_check"],
        supersedes_snapshot_id=None,
        content_hash=_hash("snapshot-content"),
    )
    return _capture(events.emit_evidence_snapshot_created, snapshot)


#: One case per emittable event type. `emit_review_lifecycle` dispatches six of
#: these from one function (`_ACTIVE_BY_ORIGIN` / `_LIFECYCLE_BY_TARGET` in
#: `events.py`) and `emit_acceptance` dispatches two (`_ACCEPTANCE_EVENTS`) —
#: see the module docstring on why the schema catalogue keys those groups to
#: one document rather than one-per-type. Keyed by event_type, not by function,
#: because the coverage check below (`test_every_emittable_event_type_...`)
#: needs to compare against `EMITTABLE`, which is a set of event types.
_CASES: dict[str, Callable[[], DomainOutbox]] = {
    "review.created.v1": _case_review_created,
    "review.activated.v1": _case_lifecycle("draft", "active"),
    "review.paused.v1": _case_lifecycle("active", "paused"),
    "review.resumed.v1": _case_lifecycle("paused", "active"),
    "review.reopened.v1": _case_lifecycle("closed", "active"),
    "review.archived.v1": _case_lifecycle("active", "archived"),
    "review.closed.v1": _case_lifecycle("active", "closed"),
    "review.import.completed.v1": _case_import_completed,
    "review.duplicate_cluster.proposed.v1": _case_duplicate_cluster_proposed,
    "review.duplicate_cluster.resolved.v1": _case_duplicate_cluster_resolved,
    "review.decision.recorded.v1": _case_decision_recorded,
    "review.conflict.opened.v1": _case_conflict_opened,
    "review.conflict.resolved.v1": _case_conflict_resolved,
    "review.research_object.approved.v1": _case_acceptance("research_object_version", with_hash=True),
    "review.observation.accepted.v1": _case_acceptance("observation_version", with_hash=False),
    "review.observation.superseded.v1": _case_observation_superseded,
    "review.assertion.recorded.v1": _case_assertion_recorded,
    "review.evidence_snapshot.created.v1": _case_evidence_snapshot_created,
}


# -------------------------------------------------------------------- tests ---


def test_every_emittable_event_type_is_exercised_by_a_case_above() -> None:
    """The ratchet. `a-ratchet-must-floor-on-offenders-found`: a coverage set
    that can silently shrink is worse than no coverage claim at all.

    `event_catalog.EMITTABLE` is the eighteen registered event types with a
    producer. If a nineteenth `emit_*` function is ever added — or the six
    lifecycle transitions or the two acceptance kinds this file dispatches by
    hand ever change — this must be updated in the SAME change, deliberately,
    or it fails here rather than the new producer going untested forever.
    """
    assert set(_CASES) == set(EMITTABLE)


def test_every_producer_writes_a_row_that_validates_against_its_released_schema() -> None:
    """The gate itself. `13-…:§8.3`.

    For every emittable event type: the emitter writes into a registered event
    (a), a released document governs it (b), the payload the emitter actually
    built satisfies that document (c), and the row reassembled as the envelope
    a consumer receives satisfies `research-event-envelope:1` (d). Any one of
    these failing means a producer and the schema it claims to implement have
    drifted — the defect this file exists to make loud rather than silent.
    """
    failures: list[str] = []
    for event_type, case in sorted(_CASES.items()):
        row = case()

        # (a) — the row is registered as one of the eighteen with a producer.
        if row.event_type not in EMITTABLE:
            failures.append(f"{event_type}: row.event_type {row.event_type!r} is not in EMITTABLE")
            continue

        # (b) — a released payload document governs it.
        schema_ref = schema_ref_for(row.event_type)
        if schema_ref is None:
            failures.append(f"{event_type}: schema_ref_for returned None — no released payload schema")
            continue

        # (c) — the payload the producer built satisfies that document.
        payload_errors = validate_instance(row.payload, schema_ref)
        if payload_errors:
            failures.append(f"{event_type}: payload violates {schema_ref}: {payload_errors}")

        # (d) — the row, as the envelope a consumer receives, satisfies
        # research-event-envelope:1.
        envelope_errors = validate_instance(_envelope_from_row(row), ENVELOPE_SCHEMA_REF)
        if envelope_errors:
            failures.append(f"{event_type}: envelope violates {ENVELOPE_SCHEMA_REF}: {envelope_errors}")

    assert failures == [], "producer/schema disagreement:\n" + "\n".join(failures)


def test_the_conformance_check_can_actually_fail() -> None:
    """The positive control. `a-ratchet-must-floor-on-offenders-found` again,
    from the other side: a check that has never been seen to go red proves
    nothing about the day a producer's payload actually drifts from its
    schema.

    Takes one real, currently-valid captured payload and corrupts it two
    independent ways — drop a required member, and violate a bounded string's
    `maxLength` — and requires `validate_instance` to catch both. If this ever
    passes with an EMPTY error list, the check itself has stopped looking,
    which is a worse failure than the schema drift it exists to catch.
    """
    row = _case_review_created()
    schema_ref = schema_ref_for(row.event_type)
    assert schema_ref is not None

    missing_member = dict(row.payload)
    del missing_member["external_key"]
    errors = validate_instance(missing_member, schema_ref)
    assert errors != [], "dropping a required member did not fail validation — the check cannot fail"

    oversized_member = dict(row.payload)
    oversized_member["external_key"] = "x" * 600
    errors = validate_instance(oversized_member, schema_ref)
    assert errors != [], "a 600-character string in a bounded field did not fail validation — the check cannot fail"
