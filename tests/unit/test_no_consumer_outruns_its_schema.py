"""A registered consumer must not outrun the release its event depends on.

`13-…:§8.3`, verbatim:

> No service emits a listed document `09` event until its schema, example,
> consumer matrix and compatibility test are merged.

Registering a consumer is what makes the relay create a delivery, and a
delivery is what makes an event LEAVE this service. So this gate is enforced
where the registration happens, not where the relay runs — by the time a
delivery row exists it is already too late to ask whether the contract was
released.

## Why this file exists at all when the registry is empty

This file was written while `consumer_registry.REGISTERED` had no entries, so
every assertion below was trivially satisfied. That was exactly the moment to
write them: a tripwire added when the list is empty is cheap and obviously
correct, and a tripwire added after the first entry has to be argued against
whoever added it.

`a-ratchet-must-floor-on-offenders-found` warns that a guard whose subject set
is empty passes forever while the thing it guards rots. So
`test_the_gate_can_actually_refuse` builds a registration in memory and runs
the same check over it, proving the check can fail without waiting for somebody
to trip it for real. `REGISTERED` now holds `fr` and `research_output`
(`research_contracts` 0.9.0), so that control can no longer rely on an empty
registry to make the point — it now uses a real `event_catalog.NOT_YET` event,
which has no producer and so no released schema, and is a stronger case than
the synthetic one this file used to build.

## Why the schema URN is looked up, not derived

This file used to DERIVE an event's payload schema URN from its name by
convention. That stopped being correct when `research_contracts` 0.9.0 shipped
twelve payload documents covering the eighteen wired events, not eighteen:
`emit_review_lifecycle` writes six event types from one dict dispatch and
`emit_acceptance` writes two, so those event types share a document. A derived
URN resolved to nothing for eight of the eighteen. See `_unreleased` below and
`event_catalog`'s "Why the schema URN is recorded and not derived".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from domain.policies import consumer_registry, event_catalog  # noqa: E402
from domain.policies.consumer_registry import ConsumerRegistration  # noqa: E402

#: Matches `ck_event_deliveries_consumer_shape` in `ER-M022`.
_CONSUMER_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")


def _unreleased(event_type: str) -> bool:
    """True if `event_type`'s payload schema is not released in `research_contracts`.

    This used to derive the URN from the event name by convention
    (`urn:datacompany:<kebab-name>:<major>`). That broke silently the day
    `research_contracts` 0.9.0 shipped twelve payload documents for the
    eighteen wired events: `emit_review_lifecycle` writes six event types from
    one dict dispatch and `emit_acceptance` writes two, so those types share
    one document, and a name-derived URN resolved to nothing for eight of the
    eighteen. `13-…:§8.3` asks the catalogue to NAME the schema URN, so this
    looks it up through `event_catalog.schema_ref_for` — a recorded fact, not a
    convention standing in for one. Do not reintroduce derivation here: it
    stops matching the moment another producer function shares a document.

    An event with no producer (`event_catalog.NOT_YET`) has `schema_ref is
    None` and is always unreleased — there is nothing for any URN to resolve
    to.
    """
    from research_contracts.canonical.registry import canonical_schemas

    schema_ref = event_catalog.schema_ref_for(event_type)
    return schema_ref is None or schema_ref not in canonical_schemas()


def _violations(registrations: tuple[ConsumerRegistration, ...]) -> dict[str, list[str]]:
    """`{consumer: [events whose payload schema is not released]}`."""
    found: dict[str, list[str]] = {}
    for registration in registrations:
        unreleased = sorted(e for e in registration.events if _unreleased(e))
        if unreleased:
            found[registration.name] = unreleased
    return found


def test_the_gate_can_actually_refuse() -> None:
    """The positive control. A gate over an empty set proves nothing by itself.

    Builds a registration for a real registered event whose payload schema is
    genuinely unreleased — one of the three in `event_catalog.NOT_YET`, which
    has no producer and so `schema_ref is None`. Now that `research_contracts`
    0.9.0 has released real schemas and `REGISTERED` has real entries, a
    synthetic always-unreleased URN would no longer prove the check can fail;
    this uses an actual gap instead, which is the stronger case.
    """
    sample = sorted(event_catalog.NOT_YET)[0]
    assert event_catalog.schema_ref_for(sample) is None, (
        f"{sample} now has a schema_ref — pick a different NOT_YET event for this control."
    )
    pretend = (
        ConsumerRegistration(
            name="a_consumer_that_jumped_the_gun",
            owner="nobody",
            events=frozenset({sample}),
            effect="would receive an event whose contract is unreleased",
        ),
    )
    assert _violations(pretend) == {"a_consumer_that_jumped_the_gun": [sample]}


def test_no_registered_consumer_names_an_unreleased_event() -> None:
    """The gate itself. `13-…:§8.3`.

    If this fails, a consumer was registered before its event's schema,
    example, consumer matrix and compatibility test were merged in
    `research_contracts`. The relay would then create a delivery for it, the
    event would leave this service, and the gate would have been crossed by a
    two-line edit to a policy module.

    The order is: release the four artifacts, THEN register the consumer.
    """
    violations = _violations(consumer_registry.REGISTERED)
    assert violations == {}, (
        f"{violations} — each named event has no released payload schema. Release its "
        "schema, example, consumer matrix and compatibility test in research_contracts "
        "before registering a consumer for it (13-…:§8.3)."
    )


def test_every_registered_consumer_is_shaped_like_its_database_column() -> None:
    """A registration the database will refuse is worse than none.

    `ck_event_deliveries_consumer_shape` is `^[a-z][a-z0-9_]*$`. A consumer
    named otherwise would pass every check in this file and then fail at the
    first delivery INSERT, at relay time, in a background pass nobody is
    watching.
    """
    bad = [r.name for r in consumer_registry.REGISTERED if not _CONSUMER_SHAPE.match(r.name)]
    assert bad == [], f"consumer names the database will refuse: {bad}"


def test_every_registered_consumer_names_an_owner_and_an_effect() -> None:
    """`13-…:§8.3` requires the catalog to record consumers AND effect.

    And `§8.2` requires a poisoned delivery to "page the owning team", which a
    registration with no owner cannot do — the alert would fire with nobody
    accountable for it.
    """
    incomplete = [r.name for r in consumer_registry.REGISTERED if not r.owner.strip() or not r.effect.strip()]
    assert incomplete == [], f"registered without an owner or a stated effect: {incomplete}"


def test_every_registered_event_is_one_this_service_actually_produces() -> None:
    """A consumer subscribed to an event nobody emits waits forever.

    Silently: there is no error for "registered for something that never
    happens", only a queue that is always empty and a team that assumes the
    pipeline works.
    """
    known = set(event_catalog.REGISTERED_EVENTS)
    unknown = sorted({e for r in consumer_registry.REGISTERED for e in r.events if e not in known})
    assert unknown == [], f"registered for events this service does not produce: {unknown}"


def test_not_registered_and_registered_are_disjoint() -> None:
    """`consumer_registry.NOT_REGISTERED` names consumers `14-…` calls for but
    that deliberately have no entry in `REGISTERED` — `n8n`, `flow_projector`,
    `scientific_processor`, `decision_modelling` — each against the reason it
    has no inbox yet. A name in both lists would mean this file simultaneously
    claims a consumer receives deliveries and deliberately does not, which can
    only happen by editing one list and forgetting the other.
    """
    overlap = consumer_registry.registered_names() & consumer_registry.NOT_REGISTERED.keys()
    assert overlap == set(), f"named in both REGISTERED and NOT_REGISTERED: {sorted(overlap)}"


def test_every_emittable_event_has_a_released_schema_ref() -> None:
    """`event_catalog.EMITTABLE` is the eighteen events with a producer. Each one
    must name a schema in the catalogue — a `None` here would mean an event can
    leave this service with no recorded payload contract at all, which is
    exactly what `13-…:§8.3` exists to prevent.
    """
    missing = [e for e in event_catalog.EMITTABLE if event_catalog.schema_ref_for(e) is None]
    assert missing == [], f"emittable events with no recorded schema_ref: {missing}"


def test_every_not_yet_event_has_no_schema_ref() -> None:
    """The inverse of the check above. `event_catalog.NOT_YET` events have no
    producer, so a populated `schema_ref` for one would be a released contract
    with no writer — `event_catalog.CATALOGUE`'s own comment calls that "a
    contract with no writer, which reads as progress through the §8.3 gate and
    is not."
    """
    wrongly_populated = [e for e in event_catalog.NOT_YET if event_catalog.schema_ref_for(e) is not None]
    assert wrongly_populated == [], f"NOT_YET events with a schema_ref set anyway: {wrongly_populated}"
