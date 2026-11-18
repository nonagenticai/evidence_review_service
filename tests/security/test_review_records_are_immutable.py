"""`review_records` is immutable — `0003`'s trigger, unasserted anywhere until now.

## The gap

`0015` gives the runtime role `UPDATE` on `review_records` and explains exactly
why: `apply_survivorship` (`application/services/imports.py`) issues
``SELECT review_record_id FROM review_records WHERE review_record_id = :id FOR
UPDATE`` as the serialisation point for the two-cluster survivorship race, and
Postgres requires the `UPDATE` privilege for a row lock even though no row is
ever written through it. So this is the one table in the ledger where the GRANT
half of the guard is deliberately absent — the entire immutability guarantee
rests on the trigger `0003` created:

    CREATE TRIGGER trg_review_records_immutable
      BEFORE UPDATE OR DELETE ON review_records
      FOR EACH ROW EXECUTE FUNCTION review_records_immutable();

unconditional, bare `RAISE EXCEPTION 'review_records is immutable: corrections
create a new review_record_version' USING ERRCODE = 'restrict_violation'`.
Verified manually by the audit of PR #65, live: the trigger fires. Nothing in
this tree had asserted that in a test. `test_the_ledger_inputs_are_append_only.py`
covers the four tables `0015` newly guards and explicitly does not touch
`review_records` — its own module docstring says why `review_records` is a
different case from those four (it keeps `UPDATE`).

## Shape, deliberately copied from `test_the_ledger_inputs_are_append_only.py`

Same idiom, same reason: as the OWNER only the trigger stands (migrations
connect as the owner, and `FORCE ROW LEVEL SECURITY` does not reach DDL-level
privilege), refused `23001`. As the RUNTIME ROLE the story forks by verb, and
that fork is the point of this file:

* `UPDATE` — the role HOLDS the privilege (see above), so the trigger is
  reached and refuses it: `23001`, same as the owner.
* `DELETE` — `0015` revokes `DELETE` on `review_records` (it is in
  `DELETE_ONLY`, not `FULLY_GUARDED`), so the grant refuses it first: `42501`.
  The trigger would refuse it too, but the point of `0011`'s two-halves
  argument is that the runtime role never gets there.

The mutation arm disarms the trigger inside a transaction that always rolls
back and shows the tamper LAND — the same non-vacuity argument the ledger file
makes: a trigger that exists but does not fire passes every assertion above
against a table nothing could write for an unrelated reason, and the only way
to tell those apart is to remove the guard and watch the write succeed.
`review_record_versions` — `review_records`' own child, `ON DELETE RESTRICT`
(`0003`) — has to come off its own immutable trigger and lose its row first, or
the DELETE mutation arm would be refused by the foreign key instead and report
a working trigger as vacuous.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
#: See `test_the_ledger_inputs_are_append_only.py` for why this is not always
#: `DATABASE_URL`: `tests/conftest.py` only repoints `DATABASE_URL` at the
#: runtime role when `MIGRATION_DATABASE_URL` was unset going in.
APP_URL = os.getenv("APP_DATABASE_URL") or DB_URL
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

TENANT = tenant("tenant-review-records-immutable")
LEAD = "review-records-immutable-lead"

#: See `test_the_ledger_inputs_are_append_only.py`'s constant of the same name
#: for the measured (not assumed) SQLSTATE this trigger shape raises.
RESTRICT_VIOLATION = "23001"
INSUFFICIENT_PRIVILEGE = "42501"

TRIGGER = "trg_review_records_immutable"
CHILD_TRIGGER = "trg_review_record_versions_immutable"

#: `review_records` (`0003`) holds exactly four columns: the PK, `review_id`
#: (FK to `reviews`, `ON DELETE RESTRICT` — a real tamper here would collide
#: with referential integrity rather than exercise the guard, the same trap
#: `test_the_ledger_inputs_are_append_only.py` documents for
#: `duplicate_cluster_members`), `tenant_id` (the RLS discriminant — tampering
#: it would also trip the `WITH CHECK` policy, a DIFFERENT refusal that would
#: make this file's SQLSTATE assertions ambiguous about which guard fired) and
#: `created_at`. `created_at` carries neither constraint, and rewriting it is
#: still a real tamper: it is the provenance timestamp for when the record's
#: identity was established.
UPDATE_TAMPER = "UPDATE review_records SET created_at = created_at - interval '1 day' WHERE review_id = :r"
DELETE_TAMPER = "DELETE FROM review_records WHERE review_id = :r"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def owner() -> Iterator[Engine]:
    engine = create_engine(OWNER_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def app_role() -> Iterator[Engine]:
    """The NON-OWNER runtime role, or a skip — identical fixture to the ledger file's.

    A privilege test run as a superuser measures nothing: superusers hold every
    privilege and bypass RLS.
    """
    engine = create_engine(APP_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            who, privileged = conn.execute(
                text(
                    "SELECT current_user, "
                    "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user) "
                    "OR pg_catalog.pg_has_role(current_user, "
                    "  (SELECT relowner FROM pg_class WHERE relname = 'review_records'), 'MEMBER')"
                )
            ).one()
        if privileged:
            pytest.skip(
                f"APP_DATABASE_URL/DATABASE_URL names {who!r}, which is a superuser or owns the "
                "guarded table. GRANT-level refusals cannot be measured as that role; set "
                "APP_DATABASE_URL to the evidence_review_app credential (conftest provisions it)"
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def seeded(client: TestClient) -> dict[str, str]:
    """A review holding exactly one `review_records` row, written through the API.

    One accepted record is enough: `imports.import_payload` inserts a
    `ReviewRecord` for every accepted row, and a single non-duplicate import is
    the smallest real path that produces one — no dedup run or corpus version
    is needed to exercise a guard on the identity table itself.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "records-immutable", "external_key": f"ri-{uuid.uuid4()}", "title": "Records immutable"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()["protocol_version_id"]
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )

    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {"records": [{"title": "A single record", "year": 2022, "doi": f"10.1/{uuid.uuid4().hex}"}]}
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, f"seeding the fixture failed: {imported.text}"
    assert imported.json()["accepted_count"] == 1, imported.text
    return {"review_id": review_id}


def _row_count(engine: Engine, review_id: str) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        return int(
            conn.execute(
                text("SELECT count(*) FROM review_records WHERE review_id = :r"), {"r": review_id}
            ).scalar_one()
        )


def _refusal(engine: Engine, statement: str, review_id: str, *, role: str = "lead") -> DBAPIError | None:
    """Run one tamper in a transaction that always rolls back; hand back the error, if any.

    Caught rather than `pytest.raises`: the failure this file exists to report
    is the write being ACCEPTED, and `pytest.raises` renders that as "DID NOT
    RAISE" — three words that name neither the table nor the tamper.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(text("SELECT set_config('app.actor_role', :x, true)"), {"x": role})
            conn.execute(text(statement), {"r": review_id})
            raise _AcceptedError()
    except _AcceptedError:
        return None
    except DBAPIError as exc:
        return exc


class _AcceptedError(Exception):
    """The tamper landed. Raised to roll the transaction back before reporting it."""


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ("update", "delete"))
def test_the_owner_is_refused_by_the_trigger(owner: Engine, seeded: dict[str, str], verb: str) -> None:
    """`0011`'s first half, for `review_records`: the trigger stops the OWNER, which no REVOKE can."""
    review_id = seeded["review_id"]
    assert _row_count(owner, review_id) > 0, (
        "review_records holds no row for this review, so a refusal below would prove nothing — the "
        "fixture did not produce the state this test needs"
    )

    statement = UPDATE_TAMPER if verb == "update" else DELETE_TAMPER
    refusal = _refusal(owner, statement, review_id)

    assert refusal is not None, (
        f"the owner {verb.upper()}d review_records and the database ACCEPTED it. Its own docstring "
        "(01-…:103) says the identity row is immutable outright: corrections create a new "
        "review_record_version, never an edit here"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == RESTRICT_VIOLATION, (
        f"the {verb} on review_records was refused with SQLSTATE {sqlstate!r}, not the trigger's "
        f"{RESTRICT_VIOLATION} — something other than the immutability guard stopped it, so this "
        f"proves nothing about the guard: {refusal}"
    )
    assert "review_records is immutable" in str(refusal), str(refusal)


def test_the_runtime_role_update_is_refused_by_the_trigger(app_role: Engine, seeded: dict[str, str]) -> None:
    """The role KEEPS `UPDATE` (0015: `apply_survivorship`'s `FOR UPDATE` needs it), so the trigger is what refuses this.

    If this ever reports `42501` instead, `UPDATE` was revoked after all —
    which would also mean `apply_survivorship`'s row lock is broken, a second,
    unrelated regression this assertion would be the first thing to notice.
    """
    refusal = _refusal(app_role, UPDATE_TAMPER, seeded["review_id"])

    assert refusal is not None, (
        "the runtime role UPDATEd review_records and the database ACCEPTED it — the trigger did not "
        "fire, and UPDATE is the ONLY thing standing between the runtime role and rewriting the "
        "review's identity rows"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == RESTRICT_VIOLATION, (
        f"the runtime role's UPDATE on review_records was refused with {sqlstate!r}, not the "
        f"trigger's {RESTRICT_VIOLATION}. If it is {INSUFFICIENT_PRIVILEGE}, UPDATE was revoked from "
        f"evidence_review_app — which breaks apply_survivorship's `SELECT ... FOR UPDATE` lock, a "
        f"real product regression, not merely a weaker test: {refusal}"
    )


def test_the_runtime_role_delete_is_refused_by_the_grant(app_role: Engine, seeded: dict[str, str]) -> None:
    """`DELETE` is revoked (`0015`, `DELETE_ONLY`), so the grant refuses this before the trigger is reached."""
    refusal = _refusal(app_role, DELETE_TAMPER, seeded["review_id"])

    assert refusal is not None, (
        "the runtime role DELETEd review_records and the database ACCEPTED it; 0015's REVOKE DELETE "
        "on review_records is not in effect on this database"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == INSUFFICIENT_PRIVILEGE, (
        f"the runtime role's DELETE on review_records was refused with {sqlstate!r} rather than "
        f"{INSUFFICIENT_PRIVILEGE}. If it is {RESTRICT_VIOLATION} the trigger caught it and the "
        f"GRANT was never revoked, which is half the guard missing: {refusal}"
    )


# ---------------------------------------------------------------------------
# The mutation arm — the guard must be the thing doing the refusing
# ---------------------------------------------------------------------------


def test_disarming_the_trigger_lets_the_update_through(owner: Engine, seeded: dict[str, str]) -> None:
    """MUTATION. Disarm `trg_review_records_immutable` and the refused UPDATE must SUCCEED.

    Rolled back unconditionally, so neither the tamper nor the disarm survives
    the test — the same construction `test_the_ledger_inputs_are_append_only.py`
    uses, for the same reason: `DISABLE TRIGGER` is DDL, and DDL is
    transactional in postgres, which is what makes this safe.
    """
    review_id = seeded["review_id"]
    conn = owner.connect()
    transaction = conn.begin()
    try:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(text(f"ALTER TABLE review_records DISABLE TRIGGER {TRIGGER}"))
        landed = conn.execute(text(UPDATE_TAMPER), {"r": review_id}).rowcount
    finally:
        transaction.rollback()
        conn.close()

    assert landed > 0, (
        f"with {TRIGGER} DISABLED, the UPDATE on review_records still changed no rows. So the "
        "refusal asserted elsewhere in this file is NOT the trigger's doing — a constraint or an "
        "empty fixture is refusing it, and the immutability guard is unmeasured"
    )


def test_disarming_the_trigger_lets_the_delete_through(owner: Engine, seeded: dict[str, str]) -> None:
    """MUTATION. Same argument, for DELETE — which also needs its child's guard off and its row cleared.

    `review_record_versions.review_record_id` is `ON DELETE RESTRICT` onto
    `review_records` (`0003`), and referential integrity is an AFTER trigger —
    so with ONLY `trg_review_records_immutable` disabled, the DELETE would be
    refused by the foreign key instead (`23503`), landing 0 rows and reporting
    a working guard as vacuous. `review_record_versions` carries its own
    immutable trigger (`0003`, `trg_review_record_versions_immutable`), which
    has to come off too before its row can be cleared. Both guards, and the
    clearing DELETE, run inside the one transaction this test rolls back.
    """
    review_id = seeded["review_id"]
    conn = owner.connect()
    transaction = conn.begin()
    try:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(text(f"ALTER TABLE review_record_versions DISABLE TRIGGER {CHILD_TRIGGER}"))
        conn.execute(text("DELETE FROM review_record_versions WHERE review_id = :r"), {"r": review_id})
        conn.execute(text(f"ALTER TABLE review_records DISABLE TRIGGER {TRIGGER}"))
        landed = conn.execute(text(DELETE_TAMPER), {"r": review_id}).rowcount
    finally:
        transaction.rollback()
        conn.close()

    assert landed > 0, (
        f"with {TRIGGER} DISABLED and its child version row cleared, the DELETE on review_records "
        "still changed no rows. So the refusal asserted elsewhere in this file is NOT the trigger's "
        "doing, and the immutability guard is unmeasured"
    )


def test_nothing_was_left_disarmed(owner: Engine) -> None:
    """The mutation arm rolled back — asserted from `pg_trigger`, not assumed.

    `tgenabled = 'O'` is "fires in the origin session", the default. Anything
    else means a `DISABLE TRIGGER` above escaped its transaction, and every
    subsequent run of this suite would measure an unguarded database while
    reporting green.
    """
    names = (TRIGGER, CHILD_TRIGGER)
    with owner.begin() as conn:
        states = dict(
            conn.execute(
                text(
                    "SELECT t.tgname, t.tgenabled::text FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname = ANY(:names)"
                ),
                {"names": list(names)},
            ).all()
        )
    assert set(states) == set(names), f"expected triggers {sorted(names)} not all present: found {sorted(states)}"
    assert all(state == "O" for state in states.values()), f"a trigger was left disabled: {states}"
