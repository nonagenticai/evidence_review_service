"""The row-level-security pattern every later table inherits, proven not assumed.

Reviewer blinding (ER-004.2) is built on this. If tenant isolation is wrong here,
it is wrong in `screening_decisions` too, and there the failure is a disclosure
rather than a bug. So the pattern is falsified once, at the foundation:

* a bound tenant sees its own rows and only its own;
* an UNBOUND tenant sees nothing — fail-closed, because the alternative is that a
  forgotten `bind_actor` silently returns every tenant's rows;
* the runtime role cannot disable the policy at all.

The last one is why there are two database roles. `FORCE ROW LEVEL SECURITY`
makes policies apply to the owner, but an owner can always `ALTER TABLE …
DISABLE`. Only a non-owner is structurally unable to.

Skipped unless a database is reachable. It is NOT marked `integration` — this
repository has no deselected lane, because a lane CI never runs is advertised
coverage rather than real coverage.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
#: The migration owner. Falls back to DATABASE_URL for a single-role setup.
OWNER_DB_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
TENANT_A = tenant("tenant-A")
TENANT_B = tenant("tenant-B")
APP_ROLE = "evidence_review_app"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)


@pytest.fixture()
def engine():  # noqa: ANN201
    # Connects as the OWNER and drops to the app role with `SET LOCAL ROLE` for
    # each assertion — that is what `_as_app` below does, and it is why the
    # fixture can seed rows the policy would otherwise refuse.
    #
    # This used to read `DATABASE_URL`, which was the owner at the time. Once
    # `DATABASE_URL` became the non-owner runtime role — as it is in production —
    # the seeding `INSERT` started failing the policy's `WITH CHECK`. The fix is
    # to name the owner explicitly rather than to depend on which role happens to
    # be configured.
    eng = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})
    # The runtime role is provisioned out of band (it exists on worker4, not in
    # CI's ephemeral database), and the migration therefore grants to it only
    # when present. The test needs it to exist, so it creates it here rather
    # than skipping — a blinding test that quietly skips is worse than no test,
    # because the suite still reports green.
    with eng.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).scalar()
        if not exists:
            conn.execute(text(f"CREATE ROLE {APP_ROLE} NOLOGIN"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON operation_idempotency TO {APP_ROLE}"))
    with eng.begin() as conn:
        for tenant_id, subject in ((TENANT_A, "reviewer-a"), (TENANT_B, "reviewer-b")):
            # The tenant GUC is bound BEFORE each insert, because `FORCE ROW
            # LEVEL SECURITY` applies the policy's `WITH CHECK` to the owner too
            # and an unbound insert is refused. It used to work unbound only
            # because the owner in the old local setup was a superuser, which
            # bypasses RLS outright — so this seeding, and the isolation it was
            # meant to demonstrate, were both being exercised against a database
            # where the policy did nothing at all.
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": tenant_id})
            conn.execute(text("DELETE FROM operation_idempotency WHERE resource_tenant_id = :t"), {"t": tenant_id})
            conn.execute(
                text(
                    "INSERT INTO operation_idempotency (resource_scope, resource_tenant_id, "
                    "principal_subject_id, operation_id, idempotency_key, request_hash, "
                    "response_status) VALUES ('review', :t, :s, 'create', 'k1', 'h', 201)"
                ),
                {"t": tenant_id, "s": subject},
            )
    yield eng
    eng.dispose()


@pytest.fixture()
def app_engine(engine):  # noqa: ANN201, ANN001
    """A real connection AS the runtime role.

    Not `SET LOCAL ROLE` on the owner's connection: that needs the owner to be a
    member of the app role, which it is not on worker4 and should not be. It
    used to work here only because the local owner was a superuser — the same
    superuser that was also silently bypassing every policy this file exists to
    test. Connecting as the role itself is both closer to production and the
    only version that cannot be accidentally privileged.
    """
    eng = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    yield eng
    eng.dispose()


def _as_app(conn, bound_tenant: str | None):  # noqa: ANN001, ANN202
    # Returns uuid.UUID values: `resource_tenant_id` is a uuid column as of
    # 0014, and psycopg types it regardless of any SQLAlchemy setting.
    if bound_tenant is not None:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": bound_tenant})
    return conn.execute(text("SELECT resource_tenant_id FROM operation_idempotency")).scalars().all()


@pytest.mark.parametrize(
    ("bound_tenant", "expected"),
    [(TENANT_A, [TENANT_A]), (TENANT_B, [TENANT_B])],
    ids=["tenant-A", "tenant-B"],
)
def test_a_bound_tenant_sees_only_its_own_rows(app_engine, bound_tenant: str, expected: list[str]) -> None:
    with app_engine.begin() as conn:
        assert [str(v) for v in _as_app(conn, bound_tenant)] == expected


def test_an_unbound_tenant_sees_nothing(app_engine) -> None:
    """Fail-closed.

    If this returned every row, a route that forgot to bind an actor would read
    across tenants and look perfectly healthy while doing it.
    """
    with app_engine.begin() as conn:
        assert _as_app(conn, None) == []


def test_the_runtime_role_cannot_disable_the_policy(app_engine) -> None:
    """The whole reason migrations and the service use different roles."""
    from sqlalchemy.exc import ProgrammingError

    with app_engine.begin() as conn:
        with pytest.raises(ProgrammingError, match="must be owner of table"):
            conn.execute(text("ALTER TABLE operation_idempotency DISABLE ROW LEVEL SECURITY"))


def test_the_policy_reads_the_guc_that_actor_binds(engine) -> None:
    """The migration and `src/actor.py` must name the same GUC.

    A policy reading a GUC nobody sets matches nothing, which looks exactly like
    "this tenant has no data" — the quietest possible way for isolation to be
    both broken and invisible.
    """
    from actor import TENANT_GUC

    with engine.begin() as conn:
        definition = conn.execute(
            text("SELECT qual FROM pg_policies WHERE tablename = 'operation_idempotency'")
        ).scalar_one()
    assert TENANT_GUC in definition, f"policy reads {definition!r}, actor.py binds {TENANT_GUC!r}"
