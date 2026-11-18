"""The four tables the ledger derives from refuse UPDATE and DELETE — `0015`.

`flow._collect` calls them *"the five immutable inputs"*. Until `0015` four of
them were immutable by convention only. Measured on `0014`::

    SELECT tgname FROM pg_trigger
     WHERE tgrelid IN ('import_batches'::regclass, 'import_rows'::regclass,
                       'duplicate_clusters'::regclass,
                       'duplicate_cluster_members'::regclass)
       AND NOT tgisinternal;
    (0 rows)

    grantee=evidence_review_app on all four: SELECT, INSERT, UPDATE, DELETE

and `imports.import_payload`'s own docstring already said so in as many words:
*"Append-only by construction, and NOT by trigger … somebody with the database
credential CAN delete these rows."*

## Both halves, separately, because they fail differently

`0011` explains why the trigger and the REVOKE are not substitutes, and this file
asserts them apart:

* as the **owner** — which is how a migration and any `psql` session connects —
  only the trigger stands, and the refusal is SQLSTATE `23001`
  (`restrict_violation`; see the constant below, it is NOT `2F004`);
* as the **runtime role**, the grant refuses first and the refusal is `42501`
  (`insufficient_privilege`), so a service bug reads as a permission error rather
  than as a business rule.

Asserted on SQLSTATE and never on a message substring, for the reason
`test_blinding_layers` gives at the same assertion: a unique violation or a check
constraint would refuse the write too, and a test that accepts any refusal proves
only that the row could not be written *for some reason*.

## The mutation arm is the part that makes this non-vacuous

A trigger that exists but does not fire is precisely the failure mode `0015` is
about, and every assertion above passes just as well against a table nothing can
write for an unrelated reason — a check constraint, a foreign key, an
un-provisioned fixture. So each guard is DISARMED inside a transaction that is
then rolled back, and the write it refuses is shown to SUCCEED. If the mutation
arm cannot make the write land, the refusal above was not the trigger's doing and
this file is measuring nothing.

The disarm/restore pair is `ALTER TABLE … DISABLE TRIGGER` inside a transaction
that always rolls back, so the enabled state cannot leak even if an assertion
fails mid-test — DDL is transactional in postgres. `test_nothing_was_left_disarmed`
is the belt over that brace and asserts the final state directly from
`pg_trigger`.

## The one permitted write, and why it has to be here

`import_batches` is NOT flatly append-only, and the reason is a real call path
rather than an oversight: `imports.import_payload` completes the batch's tallies
in the same transaction that inserts it. `0015`'s trigger permits exactly that
window and nothing else, so this file asserts the window is OPEN (the API import
below returns 201, which it could not if the write were refused) and that it is
SHUT afterwards.
"""

from __future__ import annotations

import json
import os
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
#: The runtime role's own URL, and it is NOT always `DATABASE_URL`.
#:
#: `tests/conftest.py` repoints `DATABASE_URL` at `evidence_review_app` only when
#: `MIGRATION_DATABASE_URL` is unset — which is CI's shape, not the local dev
#: shape. A local `.env` that sets both leaves `DATABASE_URL` naming the OWNER,
#: and the grant half of this file would then assert `42501` against a superuser
#: and fail for an environment reason. `APP_DATABASE_URL` is what the local
#: setup calls the runtime role; where neither applies, the grant tests SKIP
#: rather than report a red — the `app_role` fixture below is what decides.
APP_URL = os.getenv("APP_DATABASE_URL") or DB_URL
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

TENANT = tenant("tenant-ledger-inputs-append-only")
LEAD = "append-only-lead"

#: `restrict_violation` is **23001**, in the integrity-constraint class, and this
#: constant was `"2F004"` on the first run of this file — copied from the comment
#: in `test_blinding_layers.py:349`, which says *"a trigger's `restrict_violation`
#: is `2F004`"*. It is not. `2F004` is `prohibited_sql_statement_attempted`, a
#: PL/pgSQL-routine error, and every one of the four guards reported `23001`
#: instead. That comment is wrong where it stands; nothing there depends on it
#: (it asserts `42501`), so it is a wrong reason for a right assertion rather
#: than a live defect — but anybody copying it, as this file did, writes a test
#: that can only fail.
RESTRICT_VIOLATION = "23001"
INSUFFICIENT_PRIVILEGE = "42501"

#: `table -> (the UPDATE that tampers with it, the DELETE that removes it)`.
#:
#: Every statement is a REAL tamper rather than a token write, and that is not
#: decoration. `UPDATE import_rows SET row_index = row_index + 1` would be
#: refused just as loudly while proving nothing about the attack: flipping an
#: accepted row to quarantined is what moves `identified_total`, and dropping a
#: cluster member is what moves `duplicates_removed`. Both numbers are sealed
#: into an export's manifest.
#:
#: `import_rows`'s UPDATE also satisfies `ck_import_row_outcome_consistent`
#: (a non-accepted row must carry a reason and no record id). Without that the
#: statement is refused by the CHECK — `23514`, not `23001` — and the mutation
#: arm below would report the guard as vacuous when it is not. Measured the hard
#: way.
TAMPERS: dict[str, tuple[str, str]] = {
    "import_rows": (
        "UPDATE import_rows SET status = 'quarantined', quarantine_reason = 'forged', "
        "review_record_id = NULL WHERE review_id = :r",
        "DELETE FROM import_rows WHERE review_id = :r",
    ),
    "duplicate_clusters": (
        "UPDATE duplicate_clusters SET cluster_key = 'forged' WHERE review_id = :r",
        "DELETE FROM duplicate_clusters WHERE review_id = :r",
    ),
    # `review_id` and NOT `review_record_version_id`, which is the more obvious
    # tamper and the wrong statement for this file:
    # `duplicate_cluster_members_review_record_version_id_fkey` refuses a random
    # uuid, and an FK is an AFTER trigger — so the guard test would still see
    # `23001` while the MUTATION arm, with the guard disarmed, would get `23503`
    # and report the guard vacuous when it is not. `review_id` carries no foreign
    # key, and moving it is a real tamper: the member leaves the cluster's review
    # and `duplicates_removed` drops.
    "duplicate_cluster_members": (
        "UPDATE duplicate_cluster_members SET review_id = gen_random_uuid() WHERE review_id = :r",
        "DELETE FROM duplicate_cluster_members WHERE review_id = :r",
    ),
    "import_batches": (
        "UPDATE import_batches SET raw_payload_digest = 'sha256:forged' WHERE review_id = :r",
        "DELETE FROM import_batches WHERE review_id = :r",
    ),
}

TRIGGERS = {table: f"trg_{table}_append_only" for table in TAMPERS}

#: What has to go first for a DELETE to be POSSIBLE at all once the guard is
#: disarmed, and it is only ever needed by the mutation arm.
#:
#: Both parents are referenced `ON DELETE RESTRICT`, and referential integrity is
#: an AFTER trigger — so with the append-only guard removed the delete is refused
#: by `23503` instead, `landed` is never assigned, and the mutation arm reports a
#: working guard as vacuous. That is the same trap as
#: `duplicate_cluster_members`' foreign key above, one level up.
#:
#: It changes nothing about the guard test, which never reaches the FK: a BEFORE
#: trigger fires before referential integrity, so the refusal there is `23001`
#: whether or not children exist.
#: Each entry is `(the table whose guard must also come off, the statement that
#: clears the reference)`. Enumerated from `pg_constraint`, not guessed::
#:
#:     import_rows               -> import_batches                 ON DELETE RESTRICT
#:     review_record_versions    -> import_rows                    ON DELETE RESTRICT
#:     duplicate_cluster_members -> duplicate_clusters             ON DELETE RESTRICT
#:
#: `review_record_versions` is cleared rather than deleted: it is the anchor for
#: the corpus and every decision, and deleting it here would need three more
#: guards off to prove one point about `import_rows`.
DELETE_PREREQUISITES: dict[str, tuple[tuple[str, str], ...]] = {
    "import_batches": (
        (
            "review_record_versions",
            "UPDATE review_record_versions SET source_import_row_id = NULL WHERE review_id = :r",
        ),
        ("import_rows", "DELETE FROM import_rows WHERE review_id = :r"),
    ),
    "import_rows": (
        (
            "review_record_versions",
            "UPDATE review_record_versions SET source_import_row_id = NULL WHERE review_id = :r",
        ),
    ),
    "duplicate_clusters": (
        ("duplicate_cluster_members", "DELETE FROM duplicate_cluster_members WHERE review_id = :r"),
    ),
}

#: The guard on a prerequisite table is not always one of `0015`'s four.
PREREQUISITE_TRIGGERS = {
    **TRIGGERS,
    "review_record_versions": "trg_review_record_versions_immutable",
}


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    import sys

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
    """The NON-OWNER runtime role, or a skip.

    A privilege test run as a superuser measures nothing — superusers hold every
    privilege and bypass RLS — and reporting that as a red teaches the next
    reader to distrust this file. The same argument `tests/conftest.py` makes at
    length about provisioning the role.
    """
    engine = create_engine(APP_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            who, privileged = conn.execute(
                text(
                    "SELECT current_user, "
                    "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user) "
                    "OR pg_catalog.pg_has_role(current_user, "
                    "  (SELECT relowner FROM pg_class WHERE relname = 'import_rows'), 'MEMBER')"
                )
            ).one()
        if privileged:
            pytest.skip(
                f"APP_DATABASE_URL/DATABASE_URL names {who!r}, which is a superuser or owns the "
                "guarded tables. GRANT-level refusals cannot be measured as that role; set "
                "APP_DATABASE_URL to the evidence_review_app credential (conftest provisions it)"
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def seeded(client: TestClient) -> dict[str, str]:
    """A review holding a row in each of the four tables, written through the API.

    Written through the API rather than by INSERT on purpose: the fixture is also
    the positive control for the permitted write. `POST /imports` inserts the
    batch, inserts its rows, and then UPDATEs the batch's tallies inside the same
    transaction — the one window `0015`'s conditional trigger leaves open. A 201
    here is the assertion that the window is open; every refusal below is the
    assertion that nothing else is.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "append-only", "external_key": f"ao-{uuid.uuid4()}", "title": "Append only"},
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

    doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:8]}"
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "The trial, from PubMed", "year": 2021, "doi": doi},
                    {"title": "The trial, from Embase", "year": 2021, "doi": doi},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    # THE PERMITTED WRITE. `import_payload` UPDATEs `accepted_count` on the batch
    # it has just inserted; a flatly append-only trigger makes this a 500.
    assert imported.status_code == 201, (
        "importing a payload was refused, which is what an unconditional append-only trigger on "
        f"import_batches produces — 0015's tally window is not open: {imported.text}"
    )
    # THE TALLY ITSELF. `accepted_count` is the column the permitted UPDATE
    # writes; a 201 carrying 0 would mean the batch was inserted, the rows were
    # written, and the tally write was silently lost.
    assert imported.json()["accepted_count"] == 2, imported.text

    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers()).json()
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert clusters, "the two same-DOI records did not cluster, so there is no duplicate row to guard"
    return {"review_id": review_id}


def _rows(engine: Engine, table: str, review_id: str) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        # `table` is a key of `TAMPERS`, never caller input.
        return int(
            conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE review_id = :r"),  # nosec B608
                {"r": review_id},
            ).scalar_one()
        )


def _refusal(engine: Engine, statement: str, review_id: str, *, role: str = "lead") -> DBAPIError | None:
    """Run one tamper in a transaction that always rolls back; hand back the error.

    Caught rather than `pytest.raises`, for the reason `test_blinding_layers`
    gives: the failure this file exists to report is the write being ACCEPTED,
    and `pytest.raises` renders that as "DID NOT RAISE", three words that name
    neither the table nor the tamper.
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


@pytest.mark.parametrize("table", sorted(TAMPERS))
@pytest.mark.parametrize("verb", ("update", "delete"))
def test_the_owner_is_refused_by_the_trigger(owner: Engine, seeded: dict[str, str], table: str, verb: str) -> None:
    """`0011`'s first half: the trigger stops the OWNER, which no REVOKE can.

    Migrations connect as the owner and `FORCE ROW LEVEL SECURITY` does not apply
    to DDL-level privilege, so this is the only guarantee that holds against
    anybody holding the owner credential — which is everybody with a `psql`.
    """
    review_id = seeded["review_id"]
    assert _rows(owner, table, review_id) > 0, (
        f"{table} holds no row for this review, so a refusal below would prove nothing — "
        "the fixture did not produce the state this test needs"
    )

    statement = TAMPERS[table][0 if verb == "update" else 1]
    refusal = _refusal(owner, statement, review_id)

    assert refusal is not None, (
        f"the owner {verb.upper()}d {table} and the database ACCEPTED it. The ledger derives "
        "identified_total and duplicates_removed from these rows and seals both into an export's "
        "manifest, so this write rewrites sealed evidence with no trace"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == RESTRICT_VIOLATION, (
        f"the {verb} on {table} was refused with SQLSTATE {sqlstate!r}, not the trigger's "
        f"{RESTRICT_VIOLATION} — something other than the append-only guard stopped it, so this "
        f"proves nothing about the guard: {refusal}"
    )
    assert "append-only" in str(refusal), str(refusal)


@pytest.mark.parametrize(
    ("table", "verb"),
    [
        ("import_rows", "update"),
        ("import_rows", "delete"),
        ("duplicate_clusters", "update"),
        ("duplicate_clusters", "delete"),
        ("duplicate_cluster_members", "update"),
        ("duplicate_cluster_members", "delete"),
        # `import_batches` keeps UPDATE — the tally window — so only its DELETE
        # is a grant question. Its UPDATE is covered by the trigger test above
        # and by `test_the_runtime_role_keeps_only_the_tally_window` below.
        ("import_batches", "delete"),
    ],
)
def test_the_runtime_role_is_refused_by_the_grant(
    app_role: Engine, seeded: dict[str, str], table: str, verb: str
) -> None:
    """`0011`'s second half: the privilege is gone, so the trigger is never reached.

    The distinction is not cosmetic. A service bug that issues one of these
    statements should surface as `insufficient_privilege` — a deployment fact —
    rather than as a `restrict_violation` that reads like a business rule the
    caller broke.
    """
    statement = TAMPERS[table][0 if verb == "update" else 1]
    refusal = _refusal(app_role, statement, seeded["review_id"])

    assert refusal is not None, (
        f"the runtime role {verb.upper()}d {table} and the database ACCEPTED it; 0015's REVOKE is "
        "not in effect on this database"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == INSUFFICIENT_PRIVILEGE, (
        f"the runtime role's {verb} on {table} was refused with {sqlstate!r} rather than "
        f"{INSUFFICIENT_PRIVILEGE}. If it is {RESTRICT_VIOLATION} the trigger caught it and the "
        f"GRANT was never revoked, which is half the guard missing: {refusal}"
    )


def test_the_runtime_role_keeps_only_the_tally_window(app_role: Engine, seeded: dict[str, str]) -> None:
    """`import_batches.UPDATE` is still granted, and the trigger is what refuses it.

    `0015` trades the REVOKE on this one privilege for the import path, exactly
    as `0013` traded `review_stages.DELETE` for the correction window, and says
    so. This test pins the trade: the privilege is present (so the refusal is
    `23001`, not `42501`) and the write is refused anyway.
    """
    refusal = _refusal(
        app_role,
        "UPDATE import_batches SET accepted_count = 99 WHERE review_id = :r",
        seeded["review_id"],
    )
    assert refusal is not None, (
        "the runtime role rewrote a committed batch's tallies; the conditional trigger is the ONLY "
        "thing guarding this table's UPDATE and it did not fire"
    )
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == RESTRICT_VIOLATION, (
        f"expected the trigger ({RESTRICT_VIOLATION}) to refuse this, got {sqlstate!r}. If it is "
        f"{INSUFFICIENT_PRIVILEGE} then UPDATE was revoked on import_batches after all, and "
        f"`imports.import_payload` cannot complete a batch's tallies: {refusal}"
    )


# ---------------------------------------------------------------------------
# The mutation arm — the guard must be the thing doing the refusing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(TAMPERS))
@pytest.mark.parametrize("verb", ("update", "delete"))
def test_disarming_the_trigger_lets_the_tamper_through(
    owner: Engine, seeded: dict[str, str], table: str, verb: str
) -> None:
    """MUTATION. Disarm the guard and the refused write must SUCCEED.

    Without this the file passes against a table nothing can write for reasons
    that have nothing to do with `0015` — a check constraint, a foreign key, a
    fixture that produced no rows. A guard that exists but does not fire is the
    exact failure mode this item is about, and the only way to tell the two apart
    is to remove the guard and watch the write land.

    The whole thing runs inside one transaction that always rolls back, so
    neither the tamper nor the disarm survives the test: DDL is transactional in
    postgres, which is what makes `DISABLE TRIGGER` safe to use here at all.
    """
    review_id = seeded["review_id"]
    statement = TAMPERS[table][0 if verb == "update" else 1]
    trigger = TRIGGERS[table]

    landed = 0
    conn = owner.connect()
    transaction = conn.begin()
    try:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))  # nosec B608
        if verb == "delete":
            for child, clear in DELETE_PREREQUISITES.get(table, ()):
                if child != table:
                    conn.execute(
                        text(f"ALTER TABLE {child} DISABLE TRIGGER {PREREQUISITE_TRIGGERS[child]}")  # nosec B608
                    )
                conn.execute(text(clear), {"r": review_id})
        landed = conn.execute(text(statement), {"r": review_id}).rowcount
    finally:
        transaction.rollback()
        conn.close()

    assert landed > 0, (
        f"with {trigger} DISABLED, the {verb.upper()} on {table} still changed no rows. So the "
        "refusal asserted elsewhere in this file is NOT the trigger's doing — a constraint, a "
        "foreign key or an empty fixture is refusing it, and the append-only guard is unmeasured"
    )


def test_nothing_was_left_disarmed(owner: Engine) -> None:
    """The mutation arm rolled back — asserted from `pg_trigger`, not assumed.

    `tgenabled = 'O'` is "fires in the origin session", the default. Anything
    else means a `DISABLE TRIGGER` above escaped its transaction, and every
    subsequent run of this suite would measure an unguarded database while
    reporting green.
    """
    with owner.begin() as conn:
        states = dict(
            conn.execute(
                text(
                    "SELECT c.relname, t.tgenabled::text FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND t.tgname = ANY(:names)"
                ),
                {"names": list(TRIGGERS.values())},
            ).all()
        )
    assert set(states) == set(TRIGGERS), (
        f"0015's triggers are not all present: found {sorted(states)}, expected {sorted(TRIGGERS)}"
    )
    assert all(state == "O" for state in states.values()), f"a trigger was left disabled: {states}"
