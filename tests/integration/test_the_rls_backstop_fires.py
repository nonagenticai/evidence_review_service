"""If the suite ever runs as a superuser, it must go RED — asserted, not assumed.

`tests/conftest.py` demotes the CI superuser to a non-owner `evidence_review_app`
before anything runs, because a superuser BYPASSES row-level security entirely:
a suite run as one does not merely test less, it reports green while the policies
do nothing at all. Every blinding and cross-review assertion in `tests/security/`
rests on that demotion having happened.

The demotion can be skipped. Two paths do it — `MIGRATION_DATABASE_URL` already
set, and a `DATABASE_URL` role that cannot create a role — and both are legitimate
in local development. So the question that matters is not "does conftest work" but
**"if it silently did not, would anything notice?"**

Measured 2026-08-10, running `tests/security/` + `test_rls_foundation.py` twice
against one database:

    runtime role provisioned (the CI shape)  ->  80 passed
    provisioning skipped, running as superuser -> 7 FAILED, 73 passed

So the backstop fires. That was a one-off measurement, and a one-off measurement
of a safety net is exactly the kind of thing that stops being true without anyone
noticing — the seven failures live in two files that get edited. This pins it.

## What this file does NOT do

It does not re-run the suite. It asserts the two properties the backstop is made
of, directly against the database: the connection the suite actually uses is not
privileged, and the policies it relies on are `FORCE`d so that even the table
owner is subject to them.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: Tables whose policies the blinding and scoping suites depend on. Not every
#: table — a list that has to be updated on every migration becomes a nuisance
#: that somebody deletes. These are the ones a false green would matter most on.
LOAD_BEARING = (
    "screening_decisions",
    "screening_conflicts",
    "screening_unblinding_events",
    "reviews",
    "review_record_versions",
    # `domain_outbox` joins the list because it is the one table `13-…:§5.2`
    # would have permitted to have NO policy at all — operational tables are
    # excluded from tenant RLS there so a cross-tenant relay can discover work,
    # and `research_output_service` follows that literally. `0019` declines it
    # and records why. A deviation defended by a docstring alone is a deviation
    # nobody measures, so the policy it keeps instead is asserted here with the
    # ones a false green would matter most on.
    "domain_outbox",
)


def _runtime_engine():  # noqa: ANN202
    return create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})


def test_the_suite_does_not_run_as_a_privileged_role() -> None:
    """The premise of every security test in this repository.

    `rolsuper` bypasses RLS outright. `rolbypassrls` does the same by name. If
    either is true of the connection the suite uses, `tests/security/` is
    describing policies that are not being applied.
    """
    engine = _runtime_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
    finally:
        engine.dispose()

    user, is_super, bypasses = row
    assert not is_super, (
        f"the suite is connected as {user!r}, a SUPERUSER. Row-level security is bypassed, so every "
        "assertion in tests/security/ is being made against policies that are not running. "
        "conftest._provision_runtime_role did not demote the connection — check MIGRATION_DATABASE_URL "
        "and the stderr line it prints when it skips."
    )
    assert not bypasses, f"the suite is connected as {user!r}, which holds BYPASSRLS"


def test_the_suite_is_not_the_owner_of_the_tables_it_tests() -> None:
    """`FORCE ROW LEVEL SECURITY` is what makes ownership not an escape.

    Without `FORCE`, the table owner is exempt from its own policies, so a suite
    connected as the owner would pass every blinding test against no enforcement.
    Both halves are asserted: not the owner, and forced anyway.
    """
    engine = _runtime_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.relname, pg_get_userbyid(c.relowner) = current_user AS i_own_it, "
                    "       c.relrowsecurity, c.relforcerowsecurity "
                    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
                ),
                {"names": list(LOAD_BEARING)},
            ).all()
    finally:
        engine.dispose()

    found = {r[0] for r in rows}
    missing = set(LOAD_BEARING) - found
    assert not missing, (
        f"these tables were not found at all: {sorted(missing)}. The assertions below would then be "
        "made over a shorter list than intended, which is the vacuous case."
    )

    for name, i_own_it, enabled, forced in rows:
        assert not i_own_it, (
            f"the suite owns {name!r}. Without FORCE the owner is exempt from its own policies, and "
            "even with it, owning the table means the suite can turn the policy off."
        )
        assert enabled, f"row-level security is NOT ENABLED on {name!r}"
        assert forced, (
            f"row-level security is not FORCEd on {name!r}, so the owner is exempt from it and the "
            "policy is only as strong as who happens to connect"
        )


def test_the_runtime_role_cannot_turn_the_policy_off() -> None:
    """The active check. The two above are readings; this one tries it.

    A role that can `DISABLE ROW LEVEL SECURITY` can defeat every policy in this
    schema at will, so "not the owner" has to be true in the way that matters,
    not merely in `pg_class`.

    ## ALWAYS ROLLED BACK, and that is not tidiness

    The first version of this used `engine.begin()`, which COMMITS on success.
    Run against a privileged role — which is precisely the condition this file
    exists to detect — the `ALTER` succeeded and was committed, and it left
    `screening_decisions` with row-level security **disabled** for every
    subsequent run against that database. Measured, on my own machine, minutes
    after writing it: `rls=false forced=true`, which enforces nothing, because
    `FORCE` without `ENABLE` is inert.

    A test that silently weakens the thing it is checking is worse than no test.
    The transaction is now explicit and rolled back in `finally`, so the
    statement's outcome is observed and its effect never survives — whether it
    raised or not.
    """
    from sqlalchemy.exc import ProgrammingError

    engine = _runtime_engine()
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                with pytest.raises(ProgrammingError, match="must be owner of table"):
                    conn.execute(text("ALTER TABLE screening_decisions DISABLE ROW LEVEL SECURITY"))
            finally:
                trans.rollback()
    finally:
        engine.dispose()

    # And prove the rollback worked, rather than trusting it. If this ever reads
    # false, the statement above escaped its transaction and the database this
    # suite runs against is now unprotected.
    engine = _runtime_engine()
    try:
        with engine.connect() as conn:
            enabled = conn.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'screening_decisions'")
            ).scalar()
    finally:
        engine.dispose()
    assert enabled, (
        "row-level security on screening_decisions is DISABLED after this test ran. The ALTER above "
        "escaped its transaction, and every later test in this database is now measuring nothing."
    )
