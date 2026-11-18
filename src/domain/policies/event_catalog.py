"""The event catalogue `13-…:§8.3` calls for, as data rather than as prose.

`09-…:§3.7` names twenty-one events this service produces. Six work packages in,
on `origin/main` at `915ae08`, **not one of them had ever been written**: there
was no outbox, no writer and no row. `ER-M019` created the table; this module is
the register of what may go in it.

## The nine columns, and the two held to a different gate

`13-…:§8.3` was **amended 2026-09-04** (`DataCompany` #26). As originally written it
required an operational SLO for every event before that event could be emitted,
while `13-…:§14.2` states that code "support but do not invent targets" and that
Phase 0 supplies the numbers. Phase 0 has not run. The two rules were jointly
unsatisfiable, and this service is where that showed: an outbox, a durable log, a
delivery table and an inbox had shipped across four merged pull requests and
`research_domain_events`, `event_deliveries` and `durable_jobs` all stood at
**zero rows**, guaranteed to stay there.

Emission now gates on nine columns. `retention` and `slo` are still **required and
populated** — they hold an explicit deferral carrying its reason, never an
invented value — but they gate the first stable-v1 contract freeze rather than the
emission of an individual event.

## Why three columns are uniform across the whole set

`data_classes`, `redaction` and `retention` are one decision recorded once
(`13-…:§8.3.2`), not twenty-one judgements. They follow from `§8.2`: a payload
that contains only "IDs, versions, hashes and minimal facts" can hold no other
data class, and there is no content in it to redact. An event that stops obeying
`§8.2` leaves this set and needs its own row.

## Why the schema URN is recorded and not derived

`tests/unit/test_no_consumer_outruns_its_schema.py` used to DERIVE an event's
payload URN from its name by convention. That works only while every event has
its own document, and it silently stopped being true when
`research_contracts` 0.9.0 released **twelve** payload documents covering the
**eighteen** wired events: `emit_review_lifecycle` writes six event types from
one dict dispatch and `emit_acceptance` writes two, so those types share a
document. One producer function writes them, so they cannot diverge without it
changing, and separate documents would advertise version lifecycles that no code
path can exercise.

A derived URN would have resolved to nothing for eight of the eighteen and read
as "unreleased" for every one of them. `§8.3` asks the catalogue to NAME the
schema URN; naming it here makes the mapping a recorded fact that a reviewer can
check, and lets several event types point at one document honestly.

## ⛔ Committing is not emitting; delivering is

`13-…:§8.1` is a six-step path. This module and the outbox are step 1. What makes
an event LEAVE is a delivery row, and what creates one is a registered consumer in
`consumer_registry`. That file, not this one, is the gate on emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The producing bounded context. Mirrors `PRODUCER` in `models/foundation.py` and
#: the `ck_domain_outbox_producer` CHECK; recorded here because `§8.3` requires the
#: catalogue to name a producer, and a catalogue that made the reader look it up
#: elsewhere would not be one.
PRODUCER: Final[str] = "evidence-review"

#: `research_contracts/OWNERS.yaml` `schema_version_policy`, in one line. Every
#: event in this catalogue is governed by it, so it is stated once rather than
#: copied into twenty-one rows where twenty-one copies could drift apart.
COMPATIBILITY_RULE: Final[str] = (
    "semver on the schema $id: adding a name or an optional member is minor and keeps the "
    "$id; any change to what a released rule means mints a new $id major, and event versions "
    "ship additively so '.v1' and a future '.v2' are two registered types, not one changed one"
)

#: `13-…:§8.3.2`. One decision for the whole set, derived from the `§8.2` payload
#: rule rather than chosen per event.
DATA_CLASSES: Final[tuple[str, ...]] = ("tenant-identifiers", "content-hashes")
REDACTION: Final[str] = "none-required — the payload carries no content (13-…:§8.2)"

#: Held to the stable-v1 contract freeze, not to emission. Deleting a domain event
#: is a lawful-erasure question and `IMP-DEC-005` owns it, so this is an explicit
#: deferral and not a retention policy.
RETENTION: Final[str] = "unbounded-until-IMP-DEC-005"

#: `None`, and it must stay `None` until Phase 0 supplies numbers. `13-…:§14.2`
#: forbids inventing a target, and a plausible-looking number here would be worse
#: than the gap: an SLO nobody measured is one an operator would page against.
SLO: Final[None] = None


@dataclass(frozen=True)
class EventDescriptor:
    """One row of the `§8.3` catalogue.

    Consumers and their effects are deliberately NOT members here. They live in
    `consumer_registry`, which the relay actually reads, and a second copy in this
    module would be a mapping nothing keeps in step with the first. `§8.3` asks the
    catalogue to record "registered consumers and effect"; it does not ask for the
    record to be in one file, and the registry is where a reviewer looks anyway
    because it is what changes behaviour.
    """

    #: Which work package owes and owns this event. A team or package, never a
    #: person: `13-…:§8.2` requires a poisoned delivery to page an owning team.
    owner: str
    #: The aggregate the envelope names for this event. NOT always the review —
    #: `review.decision.recorded.v1` is about a `screening_decision`, which is why
    #: the payload carries `review_id` and is not duplicating the envelope.
    aggregate_type: str
    #: True when `aggregate_version` is a real per-aggregate counter, False when the
    #: producer writes a literal 1 because the aggregate is written once. On a
    #: write-once aggregate a supersession is a NEW aggregate chained by a payload
    #: field, so a consumer cannot get supersession order from `aggregate_version`
    #: alone. That cost is recorded rather than hidden; buying it back needs a
    #: counter column on the aggregate's own migration.
    ordering_is_counted: bool
    #: The released payload document, or `None` for an event with no producer.
    schema_ref: str | None


#: Every event `09-…:§3.7` names for this service, in the document's order.
#: Mirrors `REGISTERED_EVENTS` in `alembic/versions/0019_…`, which is the closed
#: CHECK the database enforces. Neither is derived from the other on purpose:
#: two independent spellings held equal by a test is what catches an edit to one
#: of them, and a single shared constant would make the CHECK and the caller
#: agree by construction even when both are wrong.
REGISTERED_EVENTS: Final[tuple[str, ...]] = (
    "review.created.v1",
    "review.activated.v1",
    "review.paused.v1",
    "review.resumed.v1",
    "review.reopened.v1",
    "review.archived.v1",
    "review.import.completed.v1",
    "review.duplicate_cluster.proposed.v1",
    "review.duplicate_cluster.resolved.v1",
    "review.decision.recorded.v1",
    "review.conflict.opened.v1",
    "review.conflict.resolved.v1",
    "review.research_object.approved.v1",
    "review.proposal.decided.v1",
    "review.observation.accepted.v1",
    "review.observation.superseded.v1",
    "review.assertion.recorded.v1",
    "review.appraisal.recorded.v1",
    "review.evidence_snapshot.created.v1",
    "review.stage.completed.v1",
    "review.closed.v1",
)

#: The three registered events no operation in this service can raise yet, each
#: against the reason. Measured on `915ae08`, not assumed.
#:
#: ⚠️ CORRECTED 2026-09-04. This said "four" and the prose above said "seventeen"
#: emittable, from `44e6cd7` when that was true. `ER-007` (#71, `b7ea9bc`) built
#: `review.evidence_snapshot.created.v1` -- there is a writer at
#: `application/services/snapshots.py` -- and correctly dropped it from this dict,
#: so `EMITTABLE` became eighteen. The three prose claims were not updated with it.
#: Nothing was lost: the set tests below already held `NOT_YET` and `EMITTABLE`
#: against the writers, so only the narration was wrong. But this module exists to
#: state the gap, and a register that miscounts its own gap sends the next reader
#: hunting a fourth unbuilt event that does not exist.
#: `test_the_prose_states_the_counts_the_code_computes` now pins every one of
#: those five sentences to the computed sets, so the next edit here fails loudly.
NOT_YET: Final[dict[str, str]] = {
    "review.proposal.decided.v1": (
        "ER-006.3. There is no ProposalBundle contract and no proposal_decisions table; "
        "src/application/services/evidence.py records the same gap at its extraction path."
    ),
    "review.appraisal.recorded.v1": (
        "ER-009 / ER-M009. No appraisal_records, appraisal_judgements or appraisal_evidence "
        "table exists, and no writer."
    ),
    "review.stage.completed.v1": (
        "Stage completeness is only READ, as an export precondition in "
        "src/application/services/flow.py. No operation marks a stage complete."
    ),
}

#: The eighteen this service can raise today. Derived, so it cannot disagree
#: with the two sets above.
EMITTABLE: Final[tuple[str, ...]] = tuple(name for name in REGISTERED_EVENTS if name not in NOT_YET)

#: The `§8.3` catalogue proper. Every column that varies per event is here; the
#: four that do not (`PRODUCER`, `COMPATIBILITY_RULE`, `DATA_CLASSES`, `REDACTION`)
#: and the two deferred ones (`RETENTION`, `SLO`) are module constants above,
#: because twenty-one identical copies of one value is twenty-one places for it to
#: drift and one place for a reader to be misled.
#:
#: `resource_scope` is not a column either: `services/events.py` hardcodes
#: `"tenant"` for every event this service raises, and a per-event column implying
#: the choice varies would be a lie about a value only one line can change.
CATALOGUE: Final[dict[str, EventDescriptor]] = {
    "review.created.v1": EventDescriptor(
        owner="ER-001",
        aggregate_type="review",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-created:1",
    ),
    # Six event types, one document, one producer function. See the module docstring.
    **{
        event_type: EventDescriptor(
            owner="ER-001",
            aggregate_type="review",
            ordering_is_counted=True,
            schema_ref="urn:datacompany:review-lifecycle-transition:1",
        )
        for event_type in (
            "review.activated.v1",
            "review.paused.v1",
            "review.resumed.v1",
            "review.reopened.v1",
            "review.archived.v1",
            "review.closed.v1",
        )
    },
    "review.import.completed.v1": EventDescriptor(
        owner="ER-002",
        aggregate_type="import_batch",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-import-completed:1",
    ),
    "review.duplicate_cluster.proposed.v1": EventDescriptor(
        owner="ER-003",
        aggregate_type="deduplication_run",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-duplicate-cluster-proposed:1",
    ),
    "review.duplicate_cluster.resolved.v1": EventDescriptor(
        owner="ER-003",
        aggregate_type="duplicate_resolution",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-duplicate-cluster-resolved:1",
    ),
    "review.decision.recorded.v1": EventDescriptor(
        owner="ER-004",
        aggregate_type="screening_decision",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-decision-recorded:1",
    ),
    "review.conflict.opened.v1": EventDescriptor(
        owner="ER-004",
        aggregate_type="screening_conflict",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-conflict-opened:1",
    ),
    "review.conflict.resolved.v1": EventDescriptor(
        owner="ER-004",
        aggregate_type="screening_adjudication",
        ordering_is_counted=False,
        schema_ref="urn:datacompany:review-conflict-resolved:1",
    ),
    # Two event types, one document: `emit_acceptance` writes both, keyed on
    # `subject_kind`, which is the member that says which one this is.
    **{
        event_type: EventDescriptor(
            owner="ER-006",
            aggregate_type="evidence_acceptance",
            ordering_is_counted=False,
            schema_ref="urn:datacompany:review-evidence-acceptance-recorded:1",
        )
        for event_type in (
            "review.research_object.approved.v1",
            "review.observation.accepted.v1",
        )
    },
    "review.observation.superseded.v1": EventDescriptor(
        owner="ER-006",
        aggregate_type="observation",
        ordering_is_counted=True,
        schema_ref="urn:datacompany:review-observation-superseded:1",
    ),
    "review.assertion.recorded.v1": EventDescriptor(
        owner="ER-006",
        aggregate_type="research_assertion",
        ordering_is_counted=True,
        schema_ref="urn:datacompany:review-assertion-recorded:1",
    ),
    "review.evidence_snapshot.created.v1": EventDescriptor(
        owner="ER-007",
        aggregate_type="evidence_snapshot",
        ordering_is_counted=True,
        schema_ref="urn:datacompany:review-evidence-snapshot-created:1",
    ),
    # The three with no producer. `schema_ref` is None and must stay None: a
    # released document for an event nothing can raise is a contract with no
    # writer, which reads as progress through the `§8.3` gate and is not.
    #
    # `owner` is EXPLICIT here, not derived from `NOT_YET`'s prose reason: this
    # module's own docstring ("Why the schema URN is recorded and not derived")
    # makes exactly this argument about `schema_ref`, and deriving `owner` by
    # string-splitting a human-readable sentence was the identical mistake one
    # screen further down. It produced garbage: splitting `stage.completed`'s
    # reason on the first "." landed inside the filename "flow.py" and yielded a
    # 91-character sentence fragment as a paging target, because `13-…:§8.2`
    # requires a poisoned delivery to page an owning team, not a substring of
    # prose that happened to be built for a different reader.
    "review.proposal.decided.v1": EventDescriptor(
        owner="ER-006.3",
        aggregate_type="review",
        ordering_is_counted=False,
        schema_ref=None,
    ),
    "review.appraisal.recorded.v1": EventDescriptor(
        owner="ER-009 / ER-M009",
        aggregate_type="review",
        ordering_is_counted=False,
        schema_ref=None,
    ),
    "review.stage.completed.v1": EventDescriptor(
        owner="ER-001",
        aggregate_type="review",
        ordering_is_counted=False,
        schema_ref=None,
    ),
}


def schema_ref_for(event_type: str) -> str | None:
    """The released payload document for `event_type`, or `None` if it has none.

    `None` means one of two things and the caller must not conflate them: the
    event has no producer (it is in `NOT_YET`), or `event_type` is not registered
    at all. `is_registered` separates them.
    """
    descriptor = CATALOGUE.get(event_type)
    return descriptor.schema_ref if descriptor else None


def is_registered(event_type: str) -> bool:
    return event_type in CATALOGUE
