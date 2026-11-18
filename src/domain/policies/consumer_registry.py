"""Who receives this service's events, and who is deliberately not registered yet.

`13-…:§8.3` names what an event catalog must record and then states the gate:

> No service emits a listed document `09` event until its schema, example,
> consumer matrix and compatibility test are merged.

## This file was empty until 2026-09-04, and now is not

The four artifacts existed for **zero** of the twenty-one events, so registering
anybody would have crossed the gate on the first delivery. The relay reads this
registry to decide who gets a delivery record, and no entries produced none —
which is how `FND-007` shipped a whole transport without emitting anything.

`research_contracts` 0.9.0 released the missing artifacts: twelve payload
documents covering the eighteen wired events, an example for each, the consumer
matrix below, and `tests/test_the_twenty_one_events.py` as the compatibility
test. `13-…:§8.3` was amended in the same change (`DataCompany` #26) so that the SLO
and retention columns — which `§14.2` forbids inventing before Phase 0 — gate the
stable-v1 contract freeze instead of individual emission. Without that amendment
the gate could not be crossed by any amount of work, and the four merged pull
requests of outbox, log, delivery and inbox would have held zero rows forever.

## The consumer matrix, and where it comes from

The mapping below is **not invented here**. It is
`14-…:2204-2211`'s "Minimum registered consumer/effect" column, which names a
consumer and an effect for every one of the twenty-one. A previous session
recorded that this matrix "must be invented"; it did not, and this file is that
column transcribed with the events regrouped by consumer instead of by producer
package.

## ⛔ Why four named consumers are absent, and why that loses nothing

`14-…` also names `n8n` reconciliation, a flow projector, `8808` and `8813`.
None of them exists: the dedicated `datacompany-n8n` instance is prepared and
deliberately unmerged (`15-…:505-511` — "Until it exists, `INT-N8N-WORKFLOWS`
promotion has no runtime target"), no projector is implemented in this service
(only `projection_checkpoints`, the table it would use), and `8813` is not
started.

Registering a consumer that cannot drain would accumulate delivery rows nobody
reads. Leaving it unregistered costs **nothing**, and that is the load-bearing
fact: `relay.replay(consumer=…, from_position=0)` recreates deliveries for a
newly-registered consumer from the beginning of the durable log, "without
changing `event_id`" (`§8.1` step 6). The log is the retained thing; a delivery
row is derived from it. So a consumer that registers in six months replays its
whole history and has lost no event — which is exactly why the design keeps the
log and the deliveries in separate tables.

The order to add one is: the consumer gains a real inbox → it is added here →
the relay creates its deliveries going forward → `replay` backfills the rest.

## ⛔ This is still not a TODO to be filled in casually

`tests/unit/test_no_consumer_outruns_its_schema.py` fails if any registered
consumer names an event whose payload schema does not resolve in the installed
`research_contracts`, reading the URN from `event_catalog.CATALOGUE` rather than
deriving it from the event name. Adding an entry for an unreleased event turns a
green suite red, on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

#: The shape `ck_event_deliveries_consumer_shape` enforces, verbatim from
#: `0022._TOKEN_SHAPE`. It is repeated here rather than imported because a
#: migration module name starts with a digit and cannot be imported normally —
#: and a repeated constant that drifts is exactly the defect this closes, so
#: `tests/unit/test_a_consumer_name_is_validated_where_it_is_registered.py`
#: loads `0022` by path and fails if the two ever disagree.
CONSUMER_NAME_SHAPE: Final[str] = r"^[a-z][a-z0-9_]*$"

#: The SAME shape, anchored the way Postgres anchors it.
#:
#: ⚠️ NOT `re.compile(CONSUMER_NAME_SHAPE)`. Python's `$` also matches just
#: BEFORE a trailing newline; Postgres's POSIX `$` does not. Measured:
#: `re.match(r"^[a-z][a-z0-9_]*$", "flow_projector\n")` matches, and
#: `SELECT E'flow_projector\n' ~ '^[a-z][a-z0-9_]*$'` is `f`. A validator built
#: on `$` would therefore accept exactly one name the CHECK refuses — which is
#: this module's whole defect, reintroduced through the anchor. `\A`/`\Z` have
#: no such exception.
_CONSUMER_NAME = re.compile(r"\A[a-z][a-z0-9_]*\Z")


class InvalidConsumerNameError(ValueError):
    """A registration named a consumer the database will refuse to store.

    Raised at CONSTRUCTION, which for `REGISTERED` means at import time: the
    process that would have written the bad deliveries does not start.
    """


def is_valid_consumer_name(name: str) -> bool:
    """Whether `name` satisfies `ck_event_deliveries_consumer_shape`."""
    return bool(_CONSUMER_NAME.match(name))


@dataclass(frozen=True)
class ConsumerRegistration:
    """One registered consumer and the events it receives.

    `owner` is a team or work package, not a person: `13-…:§8.2` requires a
    poisoned delivery to "page the owning team", and a registration that cannot
    say who owns it produces an alert nobody is accountable for.

    ## ⛔ `name` is validated HERE, and that is not decoration

    `event_deliveries.consumer`, `domain_inbox.consumer` and
    `projection_checkpoints.consumer` each carry a CHECK for
    `^[a-z][a-z0-9_]*$`. Until this validation existed, a single hyphen —
    `flow-projector`, the exact spelling `14-…` uses in prose — registered
    cleanly and then failed EVERY insert at delivery time, silently voiding
    that consumer's whole stream: the registry said it was registered, the
    relay tried on every pass, and the database refused every row.

    A name is data somebody types once and a stream nothing reads is a defect
    nobody sees, so the mistake is refused where it is made. `relay._fan_out`
    still defends itself against a badly-named consumer arriving by some other
    route, because two defences against a silent stream are not one too many.
    """

    #: Matches `ck_event_deliveries_consumer_shape` — `^[a-z][a-z0-9_]*$`.
    name: str
    owner: str
    #: Exact `event_type` values. Never a wildcard — a consumer that receives
    #: everything cannot be reasoned about when a twenty-second event is added.
    events: frozenset[str] = field(default_factory=frozenset)
    #: What this consumer DOES with the event. `13-…:§8.3` requires the catalog
    #: to record "registered consumers and effect", and an effect nobody wrote
    #: down is one nobody can review for rights safety.
    effect: str = ""

    def __post_init__(self) -> None:
        if not is_valid_consumer_name(self.name):
            raise InvalidConsumerNameError(
                f"consumer name {self.name!r} does not match {CONSUMER_NAME_SHAPE} — "
                "ck_event_deliveries_consumer_shape would refuse every delivery row written for "
                "it, so the consumer would be registered and receive nothing. Lowercase letters, "
                "digits and underscores only, starting with a letter (e.g. 'flow_projector', "
                "never 'flow-projector')."
            )
        if not self.owner:
            raise InvalidConsumerNameError(
                f"consumer {self.name!r} has no owner; 13-…:§8.2 requires a poisoned delivery to "
                "page the owning team, and an unowned registration produces an alert nobody is "
                "accountable for."
            )


#: `14-…:2204-2211` rows 1-5, minus the three events with no producer. The FR
#: research studio is LIVE and drives this service end to end in production.
_FR_EVENTS: Final[frozenset[str]] = frozenset(
    {
        # Row 1 — "FR task/workbench ... update projections only".
        "review.created.v1",
        "review.activated.v1",
        "review.paused.v1",
        "review.resumed.v1",
        "review.reopened.v1",
        "review.archived.v1",
        "review.closed.v1",
        # Row 2 — "FR corpus view and workflow continuation".
        "review.import.completed.v1",
        # Row 3 — "FR resolution queue".
        "review.duplicate_cluster.proposed.v1",
        "review.duplicate_cluster.resolved.v1",
        # Row 4 — "FR blinded task state".
        "review.decision.recorded.v1",
        "review.conflict.opened.v1",
        "review.conflict.resolved.v1",
        # Row 5 — "FR evidence workbench".
        "review.research_object.approved.v1",
        "review.observation.accepted.v1",
        "review.observation.superseded.v1",
        "review.assertion.recorded.v1",
    }
)

#: `14-…:2204-2211` rows 4, 5 and 6. Research Output `8811` is LIVE at schema
#: `0003`; slice `8811-A` compiles a review's flow ledger, which is why it needs
#: the decision and conflict transitions as well as the accepted evidence.
_RESEARCH_OUTPUT_EVENTS: Final[frozenset[str]] = frozenset(
    {
        # Row 4 — "8811-A read refresh".
        "review.decision.recorded.v1",
        "review.conflict.opened.v1",
        "review.conflict.resolved.v1",
        # Row 5 — "output impact projection".
        "review.research_object.approved.v1",
        "review.observation.accepted.v1",
        "review.observation.superseded.v1",
        "review.assertion.recorded.v1",
        # Row 6 — "invalidate availability/catalogue caches; none auto-run".
        "review.evidence_snapshot.created.v1",
    }
)

REGISTERED: Final[tuple[ConsumerRegistration, ...]] = (
    ConsumerRegistration(
        name="fr",
        owner="fr-control-plane",
        events=_FR_EVENTS,
        effect=(
            "Updates projections only — task and workbench state, the corpus view, the "
            "duplicate resolution queue, blinded task state and the evidence workbench. FR "
            "never treats an event as authority: 13-…:§10.1 makes the domain application "
            "receipt the reconciliation source, and callback or event delivery alone never "
            "moves FR state to applied."
        ),
    ),
    ConsumerRegistration(
        name="research_output",
        owner="8811",
        events=_RESEARCH_OUTPUT_EVENTS,
        effect=(
            "Refreshes the 8811-A flow-ledger read on a decision or conflict transition, "
            "projects output impact on accepted evidence, and invalidates availability and "
            "catalogue caches on a new evidence snapshot. NONE of these auto-runs a compile — "
            "14-…:2211 is explicit that a snapshot event invalidates caches and starts nothing, "
            "so an event can never cause an unrequested analysis."
        ),
    ),
)

#: Named by `14-…` and deliberately NOT registered, each against what is missing.
#: Held by `tests/unit/test_no_consumer_outruns_its_schema.py` so this stays a
#: statement somebody made rather than a gap nobody noticed. Adding one here
#: without a real inbox is the defect; `relay.replay` is the supported way in.
NOT_REGISTERED: Final[dict[str, str]] = {
    "n8n": (
        "The dedicated datacompany-n8n instance is prepared and deliberately unmerged. "
        "15-…:505-511: 'Until it exists, INT-N8N-WORKFLOWS promotion has no runtime target.' "
        "IMP-DEC-026, its licensing, is also open."
    ),
    "flow_projector": (
        "Not implemented. This service has projection_checkpoints -- the table a projector "
        "would checkpoint into -- and no projector. 14-…:2208-2209 names it on the duplicate "
        "resolution and decision rows."
    ),
    "scientific_processor": (
        "8808 is named on 14-…:2211 as a snapshot-cache consumer. It has no inbox for this service's events."
    ),
    "decision_modelling": (
        "8813 is not started. 14-…:2211 names it on the snapshot row; there is no service to deliver to."
    ),
}


def consumers_for(event_type: str) -> tuple[str, ...]:
    """Consumer names that should receive `event_type`, in a stable order.

    Sorted, so the deliveries a relay creates for one event are created in the
    same order on every run and on every replica. Nothing downstream depends on
    that today; it costs nothing and removes a source of "why did these rows
    come out in a different order" that would otherwise be investigated once.
    """
    return tuple(sorted(r.name for r in REGISTERED if event_type in r.events))


def registered_names() -> frozenset[str]:
    return frozenset(r.name for r in REGISTERED)
