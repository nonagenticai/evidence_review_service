"""`ER-M019` — the outbox refuses what the writer also refuses.

An event that can be edited after it was committed beside the state it describes
reopens the exact gap the outbox closes, so `domain_outbox` is append-only in the
database and not by convention. Three further CHECKs make the writer's rules
enforceable by something a caller cannot route around.

## The four guarantees, asserted apart because they fail differently

1. **Append-only.** `UPDATE` and `DELETE` are refused with SQLSTATE `23001`
   (`restrict_violation`). Asserted on SQLSTATE and never on message text, for
   the reason `test_the_ledger_inputs_are_append_only` gives: a check or a unique
   violation would refuse the write too, and a test that accepts any refusal
   proves only that the row could not be written *for some reason*.

2. **The registry is closed.** `event_type` is pinned to the twenty-one names
   `09-…:§3.7` lists (`23514`). `13-…:§8.3` asks for a registry; a registry a
   caller extends at runtime by passing a new string is not one.

3. **A payload cannot carry prose.** `er_payload_is_minimal` walks the JSON
   recursively and refuses any string over 512 characters at any depth. This is
   the guarantee `13-…:§8.2` and `14-…` state — identifiers, versions, hashes
   and minimal facts, never abstracts or extracted narrative values — made
   enforceable. It matters more here than anywhere else in the schema, because
   `13-…:§5.2` would allow this table to be excluded from tenant RLS so a relay
   can discover work across tenants, and `0019` deliberately declines that (its
   docstring records why, and how `FND-007` gets its access additively instead).
   The bound is therefore defence in depth rather than a stand-in for the
   policy — it is what bounds the damage on the day the relay policy IS added.

4. **The envelope is pinned.** A row may not declare a grammar other than the
   released `urn:datacompany:research-event-envelope:1`.

## Both directions, or the bound is measuring nothing

A 513-character string must be refused AND a 512-character one accepted. A test
that only proved the refusal would leave the over-narrow failure invisible —
a bound that rejected a legitimate URN would look identical in this file.

## The mutation arm is what makes the append-only half non-vacuous

The trigger is DISARMED inside a transaction that is then rolled back, and the
`UPDATE` it refused is shown to succeed. If the mutation arm cannot land the
write, the refusal was not the trigger's doing. DDL is transactional in
postgres, so the disarm cannot leak even if an assertion fails mid-test.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

DB_URL = os.getenv("DATABASE_URL", "")
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL

pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

ENVELOPE = "urn:datacompany:research-event-envelope:1"

INSERT = text(
    "INSERT INTO domain_outbox "
    "(event_id, event_type, schema_ref, producer, resource_scope, tenant_id, "
    " aggregate_type, aggregate_id, aggregate_version, payload) "
    "VALUES (:event_id, :event_type, :schema_ref, 'evidence-review', 'tenant', :tenant_id, "
    "        'review', :aggregate_id, 1, CAST(:payload AS jsonb))"
)


#: Every row this file writes belongs to one tenant, and the table is under
#: `FORCE ROW LEVEL SECURITY` like every other table in the schema — FORCE means
#: the owner is not exempt either, so the GUC has to be bound before an INSERT
#: or the policy's `WITH CHECK` refuses it. Binding it is not a workaround: it is
#: the same `set_config(..., is_local => true)` the application does.
TENANT = uuid.UUID("0195f48b-d000-7000-8000-0000000000e5")


def _owner_engine():  # noqa: ANN202
    return create_engine(OWNER_URL, future=True)


def _bind(conn) -> None:  # noqa: ANN001
    conn.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(TENANT)},
    )


def _insert(conn, **overrides) -> uuid.UUID:  # noqa: ANN001, ANN003
    event_id = uuid.uuid4()
    _bind(conn)
    params = {
        "event_id": event_id,
        "event_type": "review.created.v1",
        "schema_ref": ENVELOPE,
        "tenant_id": TENANT,
        "aggregate_id": uuid.uuid4(),
        "payload": '{"review_id": "0195f48b-d000-7000-8000-00000000decd"}',
    }
    params.update(overrides)
    conn.execute(INSERT, params)
    return event_id


def _sqlstate(error: DBAPIError) -> str:
    return getattr(error.orig, "sqlstate", "") or getattr(getattr(error.orig, "diag", None), "sqlstate", "") or ""


# ============================================================ 1. append-only ===


@pytest.mark.parametrize(
    ("statement", "what"),
    [
        ("UPDATE domain_outbox SET event_type = 'review.closed.v1' WHERE event_id = :event_id", "UPDATE"),
        ("DELETE FROM domain_outbox WHERE event_id = :event_id", "DELETE"),
    ],
)
def test_a_committed_event_refuses_to_be_rewritten(statement: str, what: str) -> None:
    engine = _owner_engine()
    try:
        with engine.begin() as conn:
            event_id = _insert(conn)
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _bind(conn)
            conn.execute(text(statement), {"event_id": event_id})
        assert _sqlstate(caught.value) == "23001", (
            f"{what} was refused, but not by the append-only trigger: {_sqlstate(caught.value)}"
        )
    finally:
        engine.dispose()


def test_the_refusal_is_the_triggers_doing() -> None:
    """Non-vacuity: disarm the trigger and the same UPDATE must succeed.

    Always rolled back. A committed disarm would leave the table rewritable for
    every later test in the session, and the failure would surface somewhere
    else entirely.
    """
    engine = _owner_engine()
    try:
        with engine.begin() as conn:
            event_id = _insert(conn)
        conn = engine.connect()
        transaction = conn.begin()
        try:
            _bind(conn)
            conn.execute(text("ALTER TABLE domain_outbox DISABLE TRIGGER trg_domain_outbox_append_only"))
            updated = conn.execute(
                text("UPDATE domain_outbox SET event_type = 'review.closed.v1' WHERE event_id = :event_id"),
                {"event_id": event_id},
            ).rowcount
            assert updated == 1, "the UPDATE failed even with the trigger disabled; the refusal above proved nothing"
        finally:
            transaction.rollback()
            conn.close()
    finally:
        engine.dispose()


def test_nothing_was_left_disarmed() -> None:
    engine = _owner_engine()
    try:
        with engine.connect() as conn:
            enabled = conn.execute(
                text(
                    "SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_domain_outbox_append_only' "
                    "  AND NOT tgisinternal"
                )
            ).scalar_one()
        assert enabled == "O", f"the append-only trigger is not enabled: tgenabled={enabled!r}"
    finally:
        engine.dispose()


# ========================================================= 2. closed registry ===


def test_an_unregistered_event_name_is_refused() -> None:
    engine = _owner_engine()
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, event_type="review.invented.v1")
        assert _sqlstate(caught.value) == "23514"
    finally:
        engine.dispose()


def test_a_regulatory_event_name_is_refused() -> None:
    """The denylist survives into the one record nobody can retract.

    `ADR-DATACOMPANY-REGULATORY-MODEL` is `Proposed` and unsigned; §2.2 still rejects
    every canonical regulatory table. The name is refused by the closed registry
    AND, independently, by the denylist CHECK — so removing one leaves the other.
    """
    engine = _owner_engine()
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, event_type="regulatory.provision.published.v1")
        assert _sqlstate(caught.value) == "23514"
    finally:
        engine.dispose()


def test_an_unreleased_envelope_is_refused() -> None:
    engine = _owner_engine()
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, schema_ref="https://contracts.nonagentic.ai/events/review.created.schema.json")
        assert _sqlstate(caught.value) == "23514"
    finally:
        engine.dispose()


# ======================================================== 3. payload minimality ===


def test_an_abstract_cannot_be_stored_in_a_payload() -> None:
    engine = _owner_engine()
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, payload='{"abstract": "' + "x" * 513 + '"}')
        assert _sqlstate(caught.value) == "23514"
    finally:
        engine.dispose()


def test_the_bound_cannot_be_escaped_by_nesting() -> None:
    """The recursion is the point. A bound applied only at the top level bounds
    where somebody puts the abstract, not whether they may."""
    engine = _owner_engine()
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, payload='{"record": {"fields": [{"value": "' + "y" * 513 + '"}]}}')
        assert _sqlstate(caught.value) == "23514"
    finally:
        engine.dispose()


def test_a_legitimate_payload_is_not_refused() -> None:
    """The other direction. Without this the bound could be 1 and every test
    above would still pass."""
    engine = _owner_engine()
    try:
        with engine.begin() as conn:
            _insert(
                conn,
                payload=(
                    '{"review_id": "0195f48b-d000-7000-8000-00000000decd", '
                    '"schema_ref": "' + ENVELOPE + '", '
                    '"content_hash": "sha256:' + "0" * 64 + '", '
                    '"exclusion_reason": "wrong_population", '
                    '"counts": {"included": 2, "excluded": 2}, '
                    '"boundary": "' + "z" * 512 + '"}'
                ),
            )
    finally:
        engine.dispose()


# ================================================== 4. the runtime role's grants ===


def test_the_runtime_role_cannot_rewrite_an_event_either() -> None:
    """The trigger and the grant are two different refusals and both must hold.

    `conftest.py` records why: its blanket GRANT restores whatever a migration
    revoked, so a package that forgets to join `_APPEND_ONLY_MIGRATIONS` keeps
    `UPDATE`/`DELETE` on the runtime role and its append-only tests pass for the
    WRONG reason — `23001` from the trigger rather than `42501` from the grant.
    """
    if OWNER_URL == DB_URL:
        pytest.skip("no separate runtime role in this environment; conftest did not demote the connection")
    owner = _owner_engine()
    runtime = create_engine(DB_URL, future=True)
    try:
        with owner.begin() as conn:
            event_id = _insert(conn)
        with runtime.begin() as conn, pytest.raises(DBAPIError) as caught:
            _bind(conn)
            conn.execute(
                text("UPDATE domain_outbox SET event_type = 'review.closed.v1' WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
        assert _sqlstate(caught.value) == "42501", (
            "the runtime role was refused, but by the trigger rather than by a missing grant: "
            f"{_sqlstate(caught.value)}"
        )
    finally:
        runtime.dispose()
        owner.dispose()


# ========================================= 5. the policy 13-…:§5.2 would allow away ===

#: Both tests below assert a POLICY, so they must run as the non-owner runtime
#: role. `conftest.py` demotes `DATABASE_URL` to it; where it could not — a
#: locally provisioned database whose connected role is the owner, and in that
#: container the owner is also a superuser — there is nothing to measure, and a
#: superuser bypasses RLS whether or not it is FORCEd. Skipping is the honest
#: outcome; passing here as the owner would be the vacuous one.
_NO_RUNTIME_ROLE = OWNER_URL == DB_URL
_OTHER_TENANT = uuid.UUID("0195f48b-d000-7000-8000-0000000000e6")


@pytest.mark.skipif(_NO_RUNTIME_ROLE, reason="conftest did not demote the connection; RLS cannot be measured")
def test_another_tenant_cannot_read_an_event() -> None:
    """The deviation, measured.

    `13-…:§5.2` permits an operational table to carry no tenant policy at all,
    and `research_output_service`'s outbox carries none. `0019` declines that,
    for a reason its docstring records: the exclusion would be the first hole in
    an invariant this service tests for on every run, opened for a relay that
    does not exist yet.

    A deviation nobody measures is not a decision, so this is the measurement.
    The row is written under one tenant and read back under another, through the
    same GUC the application binds.
    """
    owner = _owner_engine()
    runtime = create_engine(DB_URL, future=True)
    try:
        with owner.begin() as conn:
            event_id = _insert(conn)
        with runtime.begin() as conn:
            _bind(conn)
            mine = conn.execute(
                text("SELECT count(*) FROM domain_outbox WHERE event_id = :event_id"),
                {"event_id": event_id},
            ).scalar_one()
        with runtime.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.current_tenant', :tenant, true)"),
                {"tenant": str(_OTHER_TENANT)},
            )
            theirs = conn.execute(
                text("SELECT count(*) FROM domain_outbox WHERE event_id = :event_id"),
                {"event_id": event_id},
            ).scalar_one()
    finally:
        runtime.dispose()
        owner.dispose()

    assert mine == 1, "the writing tenant cannot see its own event; the policy is too tight, not too loose"
    assert theirs == 0, "another tenant can read this tenant's events"


@pytest.mark.skipif(_NO_RUNTIME_ROLE, reason="conftest did not demote the connection; RLS cannot be measured")
def test_an_event_cannot_be_written_into_another_tenant() -> None:
    """`WITH CHECK`, not only `USING`. A policy that filtered reads but allowed a
    foreign-tenant INSERT would let one tenant plant a permanent, unerasable row
    in another's stream."""
    runtime = create_engine(DB_URL, future=True)
    try:
        with runtime.begin() as conn, pytest.raises(DBAPIError) as caught:
            _insert(conn, tenant_id=_OTHER_TENANT)
        assert _sqlstate(caught.value) == "42501"
    finally:
        runtime.dispose()
