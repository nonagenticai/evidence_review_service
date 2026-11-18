"""A consumer name is refused where it is TYPED, not where it is inserted.

`ck_event_deliveries_consumer_shape` is `^[a-z][a-z0-9_]*$` and applies to
`event_deliveries`, `domain_inbox` and `projection_checkpoints` alike. Nothing
validated `ConsumerRegistration.name` against it, so a single hyphen —
`flow-projector`, the exact spelling `14-…` uses in prose, and a name
`consumer_registry.NOT_REGISTERED` already carries in its underscored form —
registered cleanly and then failed EVERY delivery insert. The registry said the
consumer was registered, the relay tried on every pass, the database refused
every row, and that consumer's entire stream was void with nothing reporting it.

A registry is data somebody types once. The gap between typing it and the first
refused insert is the whole cost, and it is paid in silence, so the refusal
belongs at construction.

## Why the regex is asserted against the MIGRATION

This module carries `CONSUMER_NAME_SHAPE` as a literal because a migration
module's name starts with a digit and cannot be imported by `from
alembic.versions... import`. A hand-copied constraint that drifts is the same
class of defect the validation closes, so `test_the_shape_is_the_migrations_own`
loads `0022` by path — the idiom `tests/conftest.py` uses to read `0015`'s
REVOKE list — and fails if the two ever disagree.

No database. Every assertion here is about a value refused before any statement
is issued, which is the point.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from domain.policies.consumer_registry import (  # noqa: E402
    CONSUMER_NAME_SHAPE,
    NOT_REGISTERED,
    REGISTERED,
    ConsumerRegistration,
    InvalidConsumerNameError,
    is_valid_consumer_name,
)

#: Every one of these registered cleanly before this validation existed.
REFUSED = (
    "flow-projector",  # THE one. A hyphen, and the whole stream is void.
    "n8n-reconciler",
    "FlowProjector",  # uppercase
    "Fr",
    "1flow",  # leading digit
    "_flow",  # leading underscore
    "flow projector",  # a space
    "flow.projector",  # a dot
    "flow/projector",
    "flow_projector\n",  # a trailing newline; `re.match` alone would accept it
    "",
)

ACCEPTED = ("fr", "research_output", "flow_projector", "n8n", "c9", "a")


def _registration(name: str, owner: str = "a-team") -> ConsumerRegistration:
    return ConsumerRegistration(name=name, owner=owner, events=frozenset({"review.created.v1"}))


@pytest.mark.parametrize("name", REFUSED)
def test_a_name_the_database_would_refuse_is_refused_at_registration(name: str) -> None:
    """Constructing the registration raises. No delivery is ever attempted."""
    with pytest.raises(InvalidConsumerNameError) as caught:
        _registration(name)
    assert "ck_event_deliveries_consumer_shape" in str(caught.value), (
        "the refusal must name the constraint it mirrors; a reader has to be able to find out "
        "WHY without reading the migration"
    )
    assert not is_valid_consumer_name(name)


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_name_the_database_accepts_is_accepted(name: str) -> None:
    """The validation refuses shapes, not consumers.

    Without this half the check could be `raise` on every path and every
    negative test above would still pass.
    """
    assert is_valid_consumer_name(name)
    assert _registration(name).name == name


def test_a_registration_without_an_owner_is_refused() -> None:
    """`13-…:§8.2` pages "the owning team". An unowned poison row pages nobody."""
    with pytest.raises(InvalidConsumerNameError):
        _registration("fr", owner="")


def test_every_registered_consumer_would_survive_its_own_delivery() -> None:
    """The live registry, checked against the constraint it will meet."""
    for registration in REGISTERED:
        assert is_valid_consumer_name(registration.name), (
            f"registered consumer {registration.name!r} violates {CONSUMER_NAME_SHAPE}; every "
            "delivery row written for it would be refused and its stream would be void"
        )
        assert registration.owner, f"{registration.name!r} has no owning team"
        assert registration.events, f"{registration.name!r} is registered for no events"


def test_every_name_held_in_reserve_is_registrable() -> None:
    """`NOT_REGISTERED` is the list somebody will one day promote.

    `flow_projector` is spelled with an underscore there and `14-…` spells it
    with a hyphen in prose. A name that cannot be moved from this dict into
    `REGISTERED` without failing is a trap set for the person who moves it.
    """
    for name in NOT_REGISTERED:
        assert is_valid_consumer_name(name), (
            f"{name!r} is held in NOT_REGISTERED but could never be registered: it violates {CONSUMER_NAME_SHAPE}"
        )


def test_the_shape_is_the_migrations_own() -> None:
    """`CONSUMER_NAME_SHAPE` must equal `0022._TOKEN_SHAPE`, verbatim.

    Two copies of a constraint are one drift away from a validation that
    accepts what the database refuses — which is the defect this whole file is
    about, moved one level up.
    """
    path = _ROOT / "alembic" / "versions" / "0022_er_m022_an_event_can_finally_leave_the_service.py"
    spec = importlib.util.spec_from_file_location("_ers_migration_0022", path)
    assert spec is not None and spec.loader is not None, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert CONSUMER_NAME_SHAPE == module._TOKEN_SHAPE, (
        f"the registry validates {CONSUMER_NAME_SHAPE!r} and the CHECK enforces "
        f"{module._TOKEN_SHAPE!r}. Whichever is wider lets a registration through that the "
        "database will refuse at delivery time, silently."
    )


def test_the_relays_poison_stand_in_is_itself_a_legal_name() -> None:
    """The row that records a REFUSED name must not be refused for the same reason.

    `relay._UNSHAPED_CONSUMER` is what a poisoned delivery is attributed to when
    the consumer's own name is what the database rejected. If that constant ever
    stopped matching the shape, the poison path would be unreachable again for
    exactly the failure it was built for, and no test of the relay's happy path
    would notice.
    """
    from application.services import relay

    assert is_valid_consumer_name(relay._UNSHAPED_CONSUMER)
    assert relay._UNSHAPED_CONSUMER not in {r.name for r in REGISTERED}, (
        "the poison stand-in collides with a real registered consumer, so a failure caused by a "
        "malformed name would be attributed to a team that had nothing to do with it"
    )
