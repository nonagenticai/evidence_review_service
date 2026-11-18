"""The two actor invariants, asserted before anything depends on them.

These are written at the skeleton stage on purpose. Both are properties that are
cheap to hold from the first commit and expensive to retrofit once routers exist:
a service principal that can record a decision, or a session-scoped GUC behind a
transaction pooler, are both defects that show up as *data* rather than as a
crash.
"""

from __future__ import annotations

import pytest

from actor import (
    ACTOR_GUC,
    ROLE_GUC,
    TENANT_GUC,
    Actor,
    ActorRole,
    NotAHumanPrincipalError,
    bind_actor,
    require_human,
)
from tests.tenants import tenant


class _RecordingSession:
    """Captures the SQL text and bound parameters ``bind_actor`` issues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(statement), params or {}))
        return None


# --------------------------------------------------------------------------
# Screening automation ranks and proposes. It does not decide.
# 12-datacompany-solution-profile-specification.md:121-124
# --------------------------------------------------------------------------


def test_a_service_principal_is_not_a_human() -> None:
    """A tenant without a subject is a service, and cannot record a judgement."""
    service = Actor(tenant_id="0195f48b-d000-7000-8000-000000000003", subject_id=None)
    assert not service.is_human
    with pytest.raises(NotAHumanPrincipalError):
        require_human(service)


@pytest.mark.parametrize("subject", ["", "   ", "\t", "\n"])
def test_a_blank_subject_is_not_a_human(subject: str) -> None:
    """Whitespace is not attribution.

    An empty-string subject is what an unauthenticated caller and a
    misconfigured token both produce, and `NOT NULL` alone would accept it.
    """
    actor = Actor(tenant_id=tenant("unit"), subject_id=subject)
    assert not actor.is_human
    with pytest.raises(NotAHumanPrincipalError):
        require_human(actor)


def test_a_human_subject_is_returned() -> None:
    """Falsifiable control: the guard must not reject everything."""
    actor = Actor(tenant_id=tenant("unit"), subject_id="george")
    assert require_human(actor) == "george"


def test_lead_is_a_visibility_role_not_a_bypass() -> None:
    """A lead is still a principal that must be human to decide."""
    lead = Actor(tenant_id=tenant("unit"), subject_id=None, role=ActorRole.LEAD)
    with pytest.raises(NotAHumanPrincipalError):
        require_human(lead)


# --------------------------------------------------------------------------
# Every GUC is transaction-scoped, because PgBouncer reuses server connections.
# --------------------------------------------------------------------------


def test_every_guc_is_transaction_scoped() -> None:
    """`set_config(..., true)` — never `SET`, never `set_config(..., false)`.

    PgBouncer in transaction pooling hands the same server connection to the next
    client. A session-scoped actor GUC would leak one reviewer's identity into
    another reviewer's transaction, and because the RLS policies read it, the
    result would be a blinding breach that is intermittent, invisible, and
    indistinguishable from correct behaviour under low load.
    """
    session = _RecordingSession()
    bind_actor(session, Actor(tenant_id=tenant("tenant-a"), subject_id="reviewer-a", role=ActorRole.REVIEWER))

    assert len(session.calls) == 3, session.calls
    for sql, _ in session.calls:
        normalised = " ".join(sql.split()).lower()
        assert "set_config(" in normalised, sql
        assert normalised.rstrip(")").endswith("true"), f"not transaction-scoped: {sql}"
        assert not normalised.startswith("set "), f"session-scoped SET: {sql}"


def test_all_three_gucs_are_bound_with_parameters_not_interpolation() -> None:
    """Values arrive from a token; they must never be formatted into the SQL."""
    session = _RecordingSession()
    hostile = "x'; DROP TABLE screening_decisions; --"
    bind_actor(session, Actor(tenant_id=hostile, subject_id=hostile))

    bound_names = {name for _, params in session.calls for name in params}
    assert bound_names == {"tenant", "subject", "role"}
    for sql, params in session.calls:
        assert hostile not in sql, "the value reached the SQL text"
        if params:
            assert hostile in params.values() or "reviewer" in params.values()


def test_the_guc_names_are_the_ones_the_policies_will_read() -> None:
    """Pinned. A policy referencing a GUC nobody sets matches nothing, silently.

    The migration that creates the row-level-security policies repeats these
    names; this pin is the other half of that agreement, so renaming one here
    without renaming the policy fails a test instead of quietly disabling
    blinding.
    """
    assert TENANT_GUC == "app.current_tenant"
    assert ACTOR_GUC == "app.actor_subject_id"
    assert ROLE_GUC == "app.actor_role"

    session = _RecordingSession()
    bind_actor(session, Actor(tenant_id=tenant("unit"), subject_id="s"))
    issued = " ".join(sql for sql, _ in session.calls)
    for guc in (TENANT_GUC, ACTOR_GUC, ROLE_GUC):
        assert guc in issued, f"{guc} is declared but never bound"
