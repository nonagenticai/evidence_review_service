"""Accepted evidence is append-only, and its basis cannot be forged — `ER-M017`/`ER-M018`.

`16-…:§13.1` is the requirement: *"Screening, acceptance, appraisal state, and
lifecycle are append-only decisions or bitemporal assertions over exact versions;
they are not mutable columns on those versions."* `ER-006` adds eleven tables
that carry exactly those decisions, so this file asserts they refuse to be
rewritten — and, separately, that the refusals are not vacuous.

## Three guarantees, asserted apart because they fail differently

1. **Append-only.** Every one of the eleven refuses `UPDATE` and `DELETE` with
   SQLSTATE `23001` (`restrict_violation`). Asserted on SQLSTATE and never on a
   message substring, for the reason `test_the_ledger_inputs_are_append_only`
   gives: a unique violation or a check constraint would refuse the write too,
   and a test that accepts any refusal proves only that the row could not be
   written *for some reason*.

2. **The `§3.7` either/or.** An observation version must resolve to an exact
   `EvidenceLocation` **or** an explicit expert assertion — never neither, never
   both. That is a CHECK (`23514`), so the class distinguishes it from the
   trigger above without reading any text.

3. **The `DM-AT-25` fail-closed arm.** The evidence-located arm is declared and
   switched off, because `EvidenceLocation` is owned by Ingestion/Documents
   (`§7.2`) and `8810` has no client that can verify a reference into it. A
   well-formed located observation must still be refused. If this test ever goes
   green by accident, `8810` has started accepting evidence it cannot check.

## The mutation arm is what makes this file non-vacuous

A trigger that exists but never fires passes every assertion above, and so does
a table nothing can write for an unrelated reason. So each append-only guard is
DISARMED inside a transaction that is then rolled back, and the write it refused
is shown to SUCCEED. If the mutation arm cannot land the write, the refusal was
not the trigger's doing and this file is measuring nothing.

DDL is transactional in postgres, so the disarm cannot leak even if an assertion
fails mid-test. `test_nothing_was_left_disarmed` is the belt over that brace.
"""

from __future__ import annotations

import hashlib
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

#: `restrict_violation`, the class every append-only trigger in this service
#: raises. NOT `2F004` — see the constant of the same name in
#: `test_the_ledger_inputs_are_append_only`, which records that mistake.
RESTRICT_VIOLATION = "23001"
#: `check_violation`. The `§3.7` basis rules are CHECKs, not triggers, and the
#: difference is the point: a CHECK cannot be disabled by a session that can
#: write the table.
CHECK_VIOLATION = "23514"

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64

#: `table -> (a REAL tamper, the delete)`. Every UPDATE changes something that
#: would matter if it succeeded — a rationale, a hash, a decision — rather than
#: touching a column nobody reads. A token write would be refused just as loudly
#: while proving nothing about the attack.
TAMPERS: dict[str, tuple[str, str]] = {
    "research_objects": (
        "UPDATE research_objects SET object_key = 'forged' WHERE review_id = :r",
        "DELETE FROM research_objects WHERE review_id = :r",
    ),
    "research_object_versions": (
        "UPDATE research_object_versions SET content = '{\"forged\": true}' WHERE review_id = :r",
        "DELETE FROM research_object_versions WHERE review_id = :r",
    ),
    "research_object_records": (
        "UPDATE research_object_records SET link_role = 'companion' WHERE review_id = :r",
        "DELETE FROM research_object_records WHERE review_id = :r",
    ),
    "extraction_datasets": (
        "UPDATE extraction_datasets SET dataset_key = 'forged' WHERE review_id = :r",
        "DELETE FROM extraction_datasets WHERE review_id = :r",
    ),
    "extraction_dataset_versions": (
        f"UPDATE extraction_dataset_versions SET content_hash = '{HASH_B}' WHERE review_id = :r",
        "DELETE FROM extraction_dataset_versions WHERE review_id = :r",
    ),
    "observations": (
        "UPDATE observations SET variable_key = 'forged' WHERE review_id = :r",
        "DELETE FROM observations WHERE review_id = :r",
    ),
    # The one that matters most: rewriting a value AFTER a lead accepted it is
    # the whole attack this aggregate exists to make impossible.
    "observation_versions": (
        "UPDATE observation_versions SET value = '{\"n\": 999}' WHERE review_id = :r",
        "DELETE FROM observation_versions WHERE review_id = :r",
    ),
    "research_assertions": (
        "UPDATE research_assertions SET assertion_key = 'forged' WHERE review_id = :r",
        "DELETE FROM research_assertions WHERE review_id = :r",
    ),
    "research_assertion_versions": (
        "UPDATE research_assertion_versions SET statement = 'forged' WHERE review_id = :r",
        "DELETE FROM research_assertion_versions WHERE review_id = :r",
    ),
    # Flipping a contradiction into a support is how a claim loses its
    # disconfirming evidence without anything appearing to be deleted.
    "research_assertion_support": (
        "UPDATE research_assertion_support SET stance = 'supports' WHERE review_id = :r",
        "DELETE FROM research_assertion_support WHERE review_id = :r",
    ),
    "evidence_acceptances": (
        "UPDATE evidence_acceptances SET rationale = 'forged' WHERE review_id = :r",
        "DELETE FROM evidence_acceptances WHERE review_id = :r",
    ),
}


@pytest.fixture(scope="module")
def engine():  # noqa: ANN201
    """The OWNER connection.

    Deliberately the owner and not the runtime role: under `FORCE ROW LEVEL
    SECURITY` the owner is still subject to the policies, but it is the strongest
    credential a migration or a `psql` session has — so a refusal proved here is
    a refusal that holds against everything weaker. The runtime role's REVOKE is
    a second, independent guard and `test_the_grant_guard_covers_every_revoke`
    is where the estate asserts it.
    """
    eng = create_engine(OWNER_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def seeded(engine):  # noqa: ANN201, ANN001
    """One row in every one of the eleven tables, written through SQL.

    Written directly rather than through the API because this file is about what
    the DATABASE refuses. Driving the fixture through HTTP would make a failure
    here ambiguous between "the guard is missing" and "the route changed shape".
    """
    review_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ids = {
        name: uuid.uuid4()
        for name in (
            "protocol",
            "object",
            "object_version",
            "record",
            "record_version",
            "corpus",
            "object_record",
            "dataset",
            "dataset_version",
            "observation",
            "observation_version",
            "assertion",
            "assertion_version",
            "support",
            "acceptance",
        )
    }
    params = {"r": review_id, "t": tenant_id, **{k: v for k, v in ids.items()}}

    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL app.current_tenant = '{tenant_id}'"))
        conn.execute(
            text(
                "INSERT INTO reviews (review_id, tenant_id, project_id, external_key, title,"
                " created_by_subject_id) VALUES (:r, :t, 'p', :ext, 'append-only probe', 'seed')"
            ),
            {**params, "ext": str(review_id)},
        )
        conn.execute(
            text(
                "INSERT INTO review_protocol_versions (protocol_version_id, review_id, tenant_id,"
                " version_number, schema_ref, content_hash, inclusion_criteria, exclusion_criteria,"
                " stage_definitions, exclusion_reason_vocabulary, locked_at, locked_by_subject_id,"
                " created_by_subject_id)"
                f" VALUES (:protocol, :r, :t, 1, 'urn:x:1', '{HASH_A}', '[]', '[]', '[]', '[]',"
                " now(), 'seed', 'seed')"
            ),
            params,
        )
        conn.execute(
            text("INSERT INTO review_records (review_record_id, review_id, tenant_id) VALUES (:record, :r, :t)"),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO review_record_versions (review_record_version_id, review_record_id,"
                " review_id, tenant_id, version_number, schema_ref, content_hash, record_type,"
                f" created_by_subject_id) VALUES (:record_version, :record, :r, :t, 1, 'urn:x:1', '{HASH_A}',"
                " 'report', 'seed')"
            ),
            params,
        )
        # The corpus seal is verified by a trigger, so the fixture has to seal
        # honestly: `sha256:` + sha256 of the member version ids joined by
        # newlines in ascending text order. Copied from the trigger rather than
        # invented — a placeholder hash here is refused, which is the seal doing
        # its job and has nothing to do with what this file tests.
        manifest = "sha256:" + hashlib.sha256(str(ids["record_version"]).encode()).hexdigest()
        conn.execute(
            text(
                "INSERT INTO corpus_versions (corpus_version_id, review_id, tenant_id, version_number,"
                " member_count, member_ordering_rule, member_manifest_hash, created_by_subject_id)"
                " VALUES (:corpus, :r, :t, 1, 1, 'record_version_id_asc', :manifest, 'seed')"
            ),
            {**params, "manifest": manifest},
        )
        conn.execute(
            text(
                "INSERT INTO corpus_version_members (corpus_version_id, review_id, tenant_id,"
                " review_record_id, review_record_version_id, member_ordinal)"
                " VALUES (:corpus, :r, :t, :record, :record_version, 1)"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO research_objects (research_object_id, review_id, tenant_id, object_key,"
                " created_by_subject_id) VALUES (:object, :r, :t, 'STUDY-1', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO research_object_versions (research_object_version_id, research_object_id,"
                " review_id, tenant_id, version_number, protocol_version_id, schema_ref, content_hash,"
                " content, revision_kind, rationale, created_by_subject_id)"
                f" VALUES (:object_version, :object, :r, :t, 1, :protocol, 'urn:x:1', '{HASH_A}', '{{}}',"
                " 'initial', 'seed', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO research_object_records (research_object_record_id, research_object_id,"
                " review_id, tenant_id, review_record_id, review_record_version_id, corpus_version_id,"
                " link_role, rationale, linked_by_subject_id)"
                " VALUES (:object_record, :object, :r, :t, :record, :record_version, :corpus,"
                " 'primary', 'seed', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO extraction_datasets (extraction_dataset_id, review_id, tenant_id,"
                " research_object_id, dataset_key, created_by_subject_id)"
                " VALUES (:dataset, :r, :t, :object, 'PICO', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO extraction_dataset_versions (extraction_dataset_version_id,"
                " extraction_dataset_id, review_id, tenant_id, version_number, protocol_version_id,"
                " schema_ref, content_hash, extraction_method, rationale, created_by_subject_id)"
                f" VALUES (:dataset_version, :dataset, :r, :t, 1, :protocol, 'urn:x:1', '{HASH_A}',"
                " 'manual', 'seed', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO observations (observation_id, review_id, tenant_id, extraction_dataset_id,"
                " variable_key, created_by_subject_id)"
                " VALUES (:observation, :r, :t, :dataset, 'sample_size', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO observation_versions (observation_version_id, observation_id, review_id,"
                " tenant_id, version_number, protocol_version_id, extraction_dataset_version_id,"
                " schema_ref, content_hash, value, basis_kind, expert_subject_id, expert_role,"
                " expert_rationale, expert_decision_ref, created_by_subject_id)"
                f" VALUES (:observation_version, :observation, :r, :t, 1, :protocol, :dataset_version,"
                f" 'urn:x:1', '{HASH_A}', '{{\"n\": 120}}', 'expert_assertion', 'expert-1', 'reviewer',"
                " 'stated in the abstract', :dataset_version, 'seed')"
            ),
            params,
        )
        # ⚠️ `assertion_key` was `'CLAIM-1'` until `0027` added a CHECK: uppercase
        # AND hyphenated, so `urn:datacompany:review-assertion-recorded:1`'s pattern
        # forbids it on two counts and `AssertionBody` would have refused it 422.
        # This seed writes STRAIGHT to the table, which is precisely the bypass
        # `0027` exists to close — the API is a door, not a wall. The value is
        # corrected rather than the constraint relaxed: a fixture that stores what
        # the system can never legitimately produce is exercising a state that
        # cannot occur.
        conn.execute(
            text(
                "INSERT INTO research_assertions (research_assertion_id, review_id, tenant_id,"
                " assertion_key, created_by_subject_id) VALUES (:assertion, :r, :t, 'claim_1', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO research_assertion_versions (research_assertion_version_id,"
                " research_assertion_id, review_id, tenant_id, version_number, protocol_version_id,"
                " schema_ref, content_hash, statement, content, created_by_subject_id)"
                f" VALUES (:assertion_version, :assertion, :r, :t, 1, :protocol, 'urn:x:1', '{HASH_A}',"
                " 'the drug works', '{}', 'seed')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO research_assertion_support (support_id, research_assertion_version_id,"
                " observation_version_id, review_id, tenant_id, stance)"
                " VALUES (:support, :assertion_version, :observation_version, :r, :t, 'contradicts')"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO evidence_acceptances (acceptance_id, review_id, tenant_id, subject_kind,"
                " observation_version_id, subject_content_hash, protocol_version_id, decision,"
                " rationale, decided_by_subject_id)"
                f" VALUES (:acceptance, :r, :t, 'observation_version', :observation_version, '{HASH_A}',"
                " :protocol, 'accept', 'checked against the paper', 'lead-1')"
            ),
            params,
        )
    return {"review_id": review_id, "tenant_id": tenant_id, **ids}


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@pytest.mark.parametrize("table", sorted(TAMPERS))
def test_every_accepted_evidence_table_refuses_an_update(engine, seeded, table) -> None:  # noqa: ANN001
    update, _ = TAMPERS[table]
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL app.current_tenant = '{seeded['tenant_id']}'"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text(update), {"r": seeded["review_id"]})
    assert _sqlstate(caught.value) == RESTRICT_VIOLATION, (
        f"{table} refused the UPDATE with {_sqlstate(caught.value)}, not the append-only "
        f"trigger's {RESTRICT_VIOLATION}. A refusal from some other constraint would satisfy a "
        "test that only checked 'the write failed'."
    )


@pytest.mark.parametrize("table", sorted(TAMPERS))
def test_every_accepted_evidence_table_refuses_a_delete(engine, seeded, table) -> None:  # noqa: ANN001
    _, delete = TAMPERS[table]
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL app.current_tenant = '{seeded['tenant_id']}'"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text(delete), {"r": seeded["review_id"]})
    assert _sqlstate(caught.value) == RESTRICT_VIOLATION


@pytest.mark.parametrize("table", sorted(TAMPERS))
def test_the_refusal_is_the_trigger_and_not_something_else(engine, seeded, table) -> None:  # noqa: ANN001
    """Disarm the guard, and the write it refused must land.

    This is the arm that makes the two tests above mean anything. Without it they
    pass identically against a table that refuses every write for an unrelated
    reason — a check constraint, a foreign key, a fixture that never inserted a
    row. The transaction always rolls back, and DDL is transactional in postgres,
    so the disabled state cannot outlive this function even on a failure.
    """
    update, _ = TAMPERS[table]
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(f"SET LOCAL app.current_tenant = '{seeded['tenant_id']}'"))
        connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_append_only"))
        result = connection.execute(text(update), {"r": seeded["review_id"]})
        assert result.rowcount >= 1, (
            f"with its trigger disabled, the tamper on {table} still changed no row. The "
            "refusal asserted above was therefore NOT this trigger, and this file proves "
            "nothing about it."
        )
    finally:
        transaction.rollback()
        connection.close()


def test_nothing_was_left_disarmed(engine) -> None:  # noqa: ANN001
    """Every append-only trigger is enabled, read straight from `pg_trigger`.

    The belt over the rollback's brace. `tgenabled = 'O'` is "fires on origin",
    the default; `'D'` is disabled.
    """
    with engine.connect() as conn:
        disabled = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
                    " WHERE NOT t.tgisinternal AND t.tgenabled = 'D'"
                    " AND t.tgname LIKE 'trg_%_append_only'"
                )
            )
            .scalars()
            .all()
        )
    assert not disabled, f"left disabled: {sorted(disabled)}"


# --------------------------------------------------------------------------
# `16-…:§3.7` — the basis of an observation
# --------------------------------------------------------------------------


def _insert_observation_version(conn, seeded, **overrides) -> None:  # noqa: ANN001, ANN003
    columns = {
        "observation_version_id": uuid.uuid4(),
        "observation_id": seeded["observation"],
        "review_id": seeded["review_id"],
        "tenant_id": seeded["tenant_id"],
        "version_number": 2,
        "supersedes_version_id": seeded["observation_version"],
        "protocol_version_id": seeded["protocol"],
        "extraction_dataset_version_id": seeded["dataset_version"],
        "schema_ref": "urn:x:1",
        "content_hash": HASH_B,
        "value": '{"n": 121}',
        "unit": None,
        "basis_kind": "expert_assertion",
        "document_version_ref": None,
        "evidence_location": None,
        "expert_subject_id": "expert-1",
        "expert_role": "reviewer",
        "expert_rationale": "stated in the abstract",
        "expert_decision_ref": seeded["dataset_version"],
        "created_by_subject_id": "seed",
    }
    columns.update(overrides)
    names = ", ".join(columns)
    binds = ", ".join(f":{name}" for name in columns)
    conn.execute(text(f"INSERT INTO observation_versions ({names}) VALUES ({binds})"), columns)


def _refusal(engine, seeded, **overrides):  # noqa: ANN001, ANN003, ANN201
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(f"SET LOCAL app.current_tenant = '{seeded['tenant_id']}'"))
        with pytest.raises(DBAPIError) as caught:
            _insert_observation_version(connection, seeded, **overrides)
        return caught.value
    finally:
        transaction.rollback()
        connection.close()


def test_an_observation_with_no_basis_at_all_cannot_be_written(engine, seeded) -> None:  # noqa: ANN001
    """`§3.7` — neither an evidence location nor an expert assertion.

    This is the row that would look like a recorded fact and stand behind
    nothing. It is refused by a CHECK rather than by validation, so no route,
    script or migration can produce one.
    """
    caught = _refusal(
        engine,
        seeded,
        expert_subject_id=None,
        expert_role=None,
        expert_rationale=None,
        expert_decision_ref=None,
    )
    assert _sqlstate(caught) == CHECK_VIOLATION


def test_an_observation_claiming_both_bases_cannot_be_written(engine, seeded) -> None:  # noqa: ANN001
    caught = _refusal(
        engine,
        seeded,
        document_version_ref='{"doc": 1}',
        evidence_location='{"page": 3}',
    )
    assert _sqlstate(caught) == CHECK_VIOLATION


def test_an_expert_assertion_with_a_blank_rationale_cannot_be_written(engine, seeded) -> None:  # noqa: ANN001
    """`§3.7` arm 2 requires a rationale, and whitespace is not one.

    `btrim(...) <> ''` rather than `IS NOT NULL`, because a required field
    satisfied by a space is a required field in name only.
    """
    caught = _refusal(engine, seeded, expert_rationale="   ")
    assert _sqlstate(caught) == CHECK_VIOLATION


def test_a_well_formed_evidence_located_observation_is_still_refused(engine, seeded) -> None:  # noqa: ANN001
    """`DM-AT-25` fail-closed — and this test going green would be the defect.

    `EvidenceLocation` is owned by Ingestion/Documents (`§7.2`). `8810` cannot
    ask whether a `PinnedVersionRef` into it is real, so it cannot tell a genuine
    citation from an invented one, and an unverifiable pinned reference must fail
    closed. The row below is otherwise perfectly well formed — that is the point:
    it is refused for what `8810` cannot check, not for how it is shaped.

    When the Ingestion/Documents verifier lands, the migration that enables this
    arm drops `ck_observation_located_arm_unverifiable`, and THIS test is the one
    that must be rewritten to assert the new behaviour. Do not delete it.
    """
    caught = _refusal(
        engine,
        seeded,
        basis_kind="evidence_located",
        document_version_ref='{"document_version_id": "d", "content_hash": "' + HASH_A + '"}',
        evidence_location='{"page": 3, "selector": "table-2"}',
        expert_subject_id=None,
        expert_role=None,
        expert_rationale=None,
        expert_decision_ref=None,
    )
    assert _sqlstate(caught) == CHECK_VIOLATION


def test_an_acceptance_cannot_witness_a_hash_its_subject_never_had(engine, seeded) -> None:  # noqa: ANN001
    """The witness must actually witness.

    Without the trigger, `subject_content_hash` is a free-text field the writer
    fills in, and an acceptance could claim a hash the version never carried —
    which is worse than recording nothing, because it reads as verification.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(f"SET LOCAL app.current_tenant = '{seeded['tenant_id']}'"))
        with pytest.raises(DBAPIError) as caught:
            connection.execute(
                text(
                    "INSERT INTO evidence_acceptances (acceptance_id, review_id, tenant_id,"
                    " subject_kind, observation_version_id, subject_content_hash,"
                    " protocol_version_id, decision, rationale, decided_by_subject_id)"
                    " VALUES (:a, :r, :t, 'observation_version', :ov, :wrong, :p, 'accept',"
                    " 'looks right', 'lead-1')"
                ),
                {
                    "a": uuid.uuid4(),
                    "r": seeded["review_id"],
                    "t": seeded["tenant_id"],
                    "ov": seeded["observation_version"],
                    "wrong": HASH_B,
                    "p": seeded["protocol"],
                },
            )
        assert _sqlstate(caught.value) == RESTRICT_VIOLATION
    finally:
        transaction.rollback()
        connection.close()
