"""A typo in a stage name, caught in seconds, cost the whole review.

`710830e` ("Three findings from the adversarial verification of `#42`") made
`lock_protocol_version` refuse to mint a second stage set. The refusal is right
about the hazard and WRONG ABOUT ITS SCOPE, and the scope error is total:

    existing_orders = {s.stage_order for s in review_stages}   # always contains 1
    incoming_orders = set(range(1, len(stage_definitions) + 1)) # always contains 1
    collisions      = existing_orders & incoming_orders         # NEVER empty

So the intersection is non-empty for EVERY second protocol version, whatever its
stage keys are. Reproduced end to end against the real API before this change:

    create v1 (stage named 'Titel/abstrakt')      -> 201
    lock   v1                                     -> 200
    create v2 (the typo fixed)                    -> 201
    lock   v2                                     -> 409  stage_orders: [1]
    activate v2                                   -> 422
    create v3 (keys screening-alpha / screening-beta,
               sharing NOTHING with v1)           -> 201
    lock   v3                                     -> 409  stage_orders: [1]

409 body: "…Amending a review's stage set is not supported; start a new review."

And "start a new review" is not a remedy the caller can take: `external_key` is
unique on `(tenant_id, project_id, external_key)`, so the new review cannot carry
the identity the old one already claimed.

## What is preserved, and why the guard is narrowed rather than deleted

The hazard is real and is not in question here. Two stage sets in one review put
two stages at `stage_order` 1; since `#42` a record settles at its highest
`StagePosition`, and `StagePosition` breaks an order tie on `stage_key`,
ALPHABETICALLY. An alphabetical accident would then decide whether a unanimous
exclusion under the current protocol or an inclusion under a superseded one wins.
`flow.py`'s `_review_stages` independently raises `ConservationError` on a tied
order, so the ledger already fail-closes; the lock guard is the earlier and
better-placed refusal, and it stays.

What changes is WHEN it fires. A re-lock is permitted only while the review has

* zero `screening_assignments`,
* zero `screening_decisions`,
* zero `screening_conflicts`,
* zero `inclusion_exports`, and
* has never been activated,

in which case the previously minted `review_stages` rows are DELETED and the new
set minted in their place. Under that precondition the review never holds two
stage sets at once, so no tie can exist, and nothing references the discarded
rows — the only foreign keys into `review_stages` are from
`screening_assignments`, `screening_decisions` and `screening_conflicts`, all
three of which the precondition asserts empty.

## The complete fix is a different change, and it is not this one

Ordering `StagePosition` by the protocol's `version_number` first, then
`stage_order`, then `stage_key`, gives two stage sets a total order and lets the
guard be dropped entirely. `flow_derivation.py`'s own docstring names that as
"the change to make". It changes derivation semantics for EVERY review, including
every review already screened, so it is not this change. This one restores the
correction window; that one would allow amendment mid-review.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-typo-window")
LEAD = "typo-window-lead"

TYPO = [{"key": "titel-abstrakt", "name": "Titel/abstrakt"}, {"key": "full-text", "name": "Full text"}]
FIXED = [{"key": "title-abstract", "name": "Title/abstract"}, {"key": "full-text-2", "name": "Full text"}]


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _new_review(client) -> str:  # noqa: ANN001
    created = client.post(
        "/api/reviews",
        json={"project_id": "typo-window", "external_key": f"tw-{uuid.uuid4()}", "title": "Typo window"},
        headers=headers(),
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["review_id"]


def _protocol(client, review_id: str, stage_definitions: list[dict], reviewers_required: int = 2) -> str:  # noqa: ANN001
    created = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": stage_definitions,
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population"],
            "reviewers_required": reviewers_required,
        },
        headers=headers(),
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["protocol_version_id"]


def _lock(client, review_id: str, pv: str):  # noqa: ANN001, ANN201
    return client.post(f"/api/reviews/{review_id}/protocol-versions/{pv}:lock", headers=headers())


def _activate(client, review_id: str, pv: str):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": pv, "rationale": "go"},
        headers=headers(),
    )


def _stages(client, review_id: str) -> list[dict]:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/stages", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()["items"]


def _stage_protocol_versions(review_id: str) -> set[str]:
    """The `protocol_version_id` of every stage of a review, read as the owner."""
    engine = sqlalchemy.create_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            rows = conn.execute(
                sqlalchemy.text("SELECT protocol_version_id FROM review_stages WHERE review_id = :r"),
                {"r": review_id},
            ).scalars()
            found = {str(pv) for pv in rows}
        assert found, "no stage rows were visible; the tenant GUC is probably unbound"
        return found
    finally:
        engine.dispose()


def _import_and_seal(client, review_id: str, n: int = 1) -> str:  # noqa: ANN001
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {"records": [{"title": f"Study {i}", "year": 2021, "doi": f"10.1000/{uuid.uuid4()}"} for i in range(n)]}
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text
    return sealed.json()["corpus_version_id"]


# ==========================================================================
# The window: a re-lock BEFORE any screening work
# ==========================================================================


def test_a_typo_can_be_corrected_before_any_screening_work_exists(client) -> None:
    """THE DEFECT. A locked protocol with a typo was unrecoverable.

    Measured before this change: `409 state_conflict`, `stage_orders: [1]`, with
    a body prescribing "start a new review" — which `uq_reviews_project_external_key`
    makes impossible under the same `external_key`.
    """
    review_id = _new_review(client)
    v1 = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, v1).status_code in (200, 201)
    assert [s["name"] for s in _stages(client, review_id)] == ["Titel/abstrakt", "Full text"]

    v2 = _protocol(client, review_id, FIXED)
    relocked = _lock(client, review_id, v2)
    assert relocked.status_code in (200, 201), (
        f"a stage-name typo caught before any screening work was unrecoverable (HTTP {relocked.status_code}): "
        f"{relocked.text}"
    )

    # THE OLD SET IS GONE, not sitting beside the new one. This is the assertion
    # that separates the fix from the bug it would otherwise reintroduce: two
    # stage sets in one review is exactly the state `#42`'s guard exists to
    # prevent, and "the lock returned 200" is true of that state too.
    stages = _stages(client, review_id)
    assert [s["name"] for s in stages] == ["Title/abstract", "Full text"], stages
    assert sorted(s["stage_order"] for s in stages) == [1, 2], stages
    assert {s["stage_key"] for s in stages} == {"title-abstract", "full-text-2"}, stages
    # `GET /stages` does not expose `protocol_version_id`, so this is read
    # directly rather than by widening the route's contract for a test.
    assert _stage_protocol_versions(review_id) == {v2}, "a stage still points at the superseded protocol version"

    # And the review activates against the corrected protocol, which is the whole
    # point — the 409 on `:lock` was followed by a 422 on `:activate`.
    assert _activate(client, review_id, v2).status_code in (200, 201)


def test_a_third_stage_set_with_entirely_different_keys_also_relocks(client) -> None:
    """The collision test was on `stage_order`, so different KEYS did not help.

    `incoming_orders` is `{1..N}` and `existing_orders` always contains 1, so a
    protocol sharing no key at all with the locked one collided just the same.
    Measured before this change: 409, `stage_orders: [1]`.
    """
    review_id = _new_review(client)
    v1 = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, v1).status_code in (200, 201)

    v2 = _protocol(
        client,
        review_id,
        [{"key": "screening-alpha", "name": "Alpha"}, {"key": "screening-beta", "name": "Beta"}],
    )
    relocked = _lock(client, review_id, v2)
    assert relocked.status_code in (200, 201), relocked.text
    assert {s["stage_key"] for s in _stages(client, review_id)} == {"screening-alpha", "screening-beta"}


def test_a_relock_to_a_shorter_stage_set_leaves_no_orphan_stage(client) -> None:
    """Two stages down to one. The trailing stage must not survive.

    The narrow reading of the fix — "delete the rows whose order collides" —
    leaves the old stage 2 behind and lands the review in precisely the
    unorderable state the guard defends against, with a 200 on the way in.
    """
    review_id = _new_review(client)
    v1 = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, v1).status_code in (200, 201)

    v2 = _protocol(client, review_id, [{"key": "only-stage", "name": "Only"}])
    assert _lock(client, review_id, v2).status_code in (200, 201)

    stages = _stages(client, review_id)
    assert [s["stage_key"] for s in stages] == ["only-stage"], f"an orphan stage survived the re-lock: {stages}"


# ==========================================================================
# The refusal that stays: work exists, or the review was activated
# ==========================================================================


def test_a_relock_after_activation_is_still_refused_with_the_original_reasoning(client) -> None:
    """ACTIVATION CLOSES THE WINDOW even with zero assignments and zero decisions.

    `:activate` names ONE protocol version as the criteria every decision in the
    review is judged against, and it is written into the append-only
    `review_state_history`. Re-minting the stage set afterwards would silently
    change what that recorded activation activated.
    """
    review_id = _new_review(client)
    v1 = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, v1).status_code in (200, 201)
    assert _activate(client, review_id, v1).status_code in (200, 201)

    v2 = _protocol(client, review_id, FIXED)
    refused = _lock(client, review_id, v2)
    assert refused.status_code == 409, f"a second stage set was minted into an ACTIVE review: {refused.text}"

    body = refused.json()
    assert body.get("stage_orders") == [1, 2], body
    # The original reasoning, not a new message. It is what tells the reader why
    # this is refused rather than retried.
    assert "alphabetical accident" in body["detail"], body
    assert "ledger orders every decision" in body["detail"], body

    # AND NOTHING WAS MINTED. A 409 that has already written the rows is the trap
    # `#42`'s report measured on `:activate`.
    stages = _stages(client, review_id)
    assert sorted(s["stage_order"] for s in stages) == [1, 2], stages
    assert {s["stage_key"] for s in stages} == {"titel-abstrakt", "full-text"}, stages


def test_a_relock_is_refused_once_an_assignment_exists_even_unactivated(client) -> None:
    """THE ASSIGNMENT LEG, PROVED ON ITS OWN.

    Through the API an assignment implies an activation (`create_assignments`
    refuses without an active protocol), so a test driving only the API cannot
    tell the assignment clause from the activation clause — it would pass with
    the assignment check deleted. The assignment is therefore written directly,
    as the OWNER with the tenant GUC bound, against real `stage_id`,
    `review_record_id` and `corpus_version_id` values so no foreign key is
    fabricated. The insert's row count is asserted because `FORCE ROW LEVEL
    SECURITY` applies to the owner too and an unbound GUC writes nothing while
    reporting success.
    """
    review_id = _new_review(client)
    v1 = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, v1).status_code in (200, 201)
    corpus_version_id = _import_and_seal(client, review_id, 1)
    stage = _stages(client, review_id)[0]

    engine = sqlalchemy.create_engine(os.environ["MIGRATION_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        record_id = conn.execute(
            sqlalchemy.text("SELECT review_record_id FROM review_records WHERE review_id = :r LIMIT 1"),
            {"r": review_id},
        ).scalar()
        assert record_id is not None, "the fixture sealed no record; the tenant GUC is probably unbound"
        written = conn.execute(
            sqlalchemy.text(
                "INSERT INTO screening_assignments "
                "(assignment_id, review_id, tenant_id, stage_id, round_number, review_record_id, "
                " corpus_version_id, reviewer_subject_id, assigned_by_subject_id) "
                "VALUES (:aid, :review, :tenant, :stage, 1, :record, :corpus, 'r1', :lead)"
            ),
            {
                "aid": str(uuid.uuid4()),
                "review": review_id,
                "tenant": TENANT,
                "stage": stage["stage_id"],
                "record": record_id,
                "corpus": corpus_version_id,
                "lead": LEAD,
            },
        ).rowcount
    engine.dispose()
    assert written == 1, "the assignment insert affected no rows, so this test would prove nothing"

    # The review was NEVER activated, so only the assignment can be refusing this.
    v2 = _protocol(client, review_id, FIXED)
    refused = _lock(client, review_id, v2)
    assert refused.status_code == 409, (
        f"a stage set was re-minted under an existing assignment (HTTP {refused.status_code}): {refused.text}"
    )
    assert "alphabetical accident" in refused.json()["detail"], refused.json()
    assert {s["stage_key"] for s in _stages(client, review_id)} == {"titel-abstrakt", "full-text"}


def test_the_original_lock_of_a_second_version_is_still_the_only_stage_set(client) -> None:
    """NEGATIVE CONTROL for the whole change.

    "The re-lock is permitted" must never mean "the review may hold two stage
    sets". Whatever path is taken, `review_stages` for one review must always be
    orderable: one row per position, no position twice.
    """
    review_id = _new_review(client)
    for definitions in (TYPO, FIXED, [{"key": "third", "name": "Third"}]):
        pv = _protocol(client, review_id, definitions)
        assert _lock(client, review_id, pv).status_code in (200, 201)
        orders = [s["stage_order"] for s in _stages(client, review_id)]
        assert sorted(orders) == list(range(1, len(definitions) + 1)), orders
        assert len(orders) == len(set(orders)), f"two stages share a position: {orders}"

    # And the derivation agrees — it is the independent backstop, and a review
    # this path corrupted would 500 here rather than answer.
    _import_and_seal(client, review_id, 1)
    flow = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert flow.status_code == 200, f"the ledger cannot order this review: {flow.text}"


# ==========================================================================
# The database guard, asserted as the RUNTIME role
# ==========================================================================


def test_the_runtime_role_may_delete_a_stage_only_inside_the_window(client) -> None:
    """`0013` re-grants DELETE, and the trigger is what makes that narrow.

    `0011` revoked `UPDATE, DELETE` on `review_stages` from the runtime role and
    installed an unconditional refusal. The re-lock is a DELETE, so the privilege
    has to come back — and the whole guarantee then rests on the trigger, exactly
    as `review_protocol_versions_locked_immutable` already works. Asserted as the
    role the service actually connects as, because as the owner `FORCE ROW LEVEL
    SECURITY` is bypassed and this would certify nothing.
    """
    # A review inside the window: nothing has been assigned, decided or activated.
    open_review = _new_review(client)
    assert _lock(client, open_review, _protocol(client, open_review, TYPO)).status_code in (200, 201)
    open_stage = _stages(client, open_review)[0]["stage_id"]

    # A review outside it.
    closed_review = _new_review(client)
    pv = _protocol(client, closed_review, TYPO)
    assert _lock(client, closed_review, pv).status_code in (200, 201)
    assert _activate(client, closed_review, pv).status_code in (200, 201)
    closed_stage = _stages(client, closed_review)[0]["stage_id"]

    engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])

    def bind(conn) -> None:  # noqa: ANN001
        """Re-bind the tenant GUC, and prove the target row is reachable.

        `set_config(..., true)` is TRANSACTION-local, so every `rollback()` below
        discards it. A statement issued afterwards matches NO rows under the
        tenant policy, the trigger never fires, and `pytest.raises` reports
        `DID NOT RAISE` — or worse, a permitted-looking `rowcount` of 0. This is
        the same vacuity `0011`'s own test carried undetected: while `DELETE` was
        revoked outright, an `InsufficientPrivilege` was raised before any row was
        looked at, so its DELETE leg never depended on seeing the row at all.
        """
        conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(sqlalchemy.text("SELECT set_config('app.actor_role', 'lead', true)"))

    with engine.connect() as conn:
        bind(conn)
        # BOTH PRECONDITIONS, ASSERTED — the same reason `0011`'s test asserts
        # them: a refusal test whose statement matches no rows raises nothing and
        # is indistinguishable from a guard that works.
        installed = conn.execute(
            sqlalchemy.text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'review_stages'::regclass AND NOT tgisinternal"
            )
        ).scalar()
        assert installed, "the append-only trigger is not installed on this database"
        for stage_id in (open_stage, closed_stage):
            visible = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM review_stages WHERE stage_id = :s"), {"s": stage_id}
            ).scalar()
            assert visible == 1, f"this connection cannot see {stage_id}; the statements below would prove nothing"

        # UPDATE stays revoked outright. `0011`'s argument is untouched by this
        # change: re-ordering a stage in place is never a correction, and the
        # runtime role is refused before the trigger is even reached.
        with pytest.raises(sqlalchemy.exc.DatabaseError) as caught:
            conn.execute(
                sqlalchemy.text("UPDATE review_stages SET stage_order = 99 WHERE stage_id = :s"), {"s": open_stage}
            )
            conn.commit()
        conn.rollback()
        assert "permission" in str(caught.value).lower(), caught.value

        # DELETE on a review that has been activated: the privilege is now there,
        # so this reaches the trigger and the trigger is what refuses it.
        bind(conn)
        assert (
            conn.execute(
                sqlalchemy.text("SELECT count(*) FROM review_stages WHERE stage_id = :s"), {"s": closed_stage}
            ).scalar()
            == 1
        ), "the row is invisible again; the DELETE below would match nothing"
        with pytest.raises(sqlalchemy.exc.DatabaseError) as caught:
            conn.execute(sqlalchemy.text("DELETE FROM review_stages WHERE stage_id = :s"), {"s": closed_stage})
            conn.commit()
        conn.rollback()
        assert "append" in str(caught.value).lower(), caught.value

        # DELETE inside the window: permitted, and it is the runtime role doing it.
        bind(conn)
        deleted = conn.execute(
            sqlalchemy.text("DELETE FROM review_stages WHERE stage_id = :s"), {"s": open_stage}
        ).rowcount
        conn.commit()
        assert deleted == 1, "the runtime role could not delete a stage inside the correction window"
    engine.dispose()

    # The activated review is untouched; the open one lost exactly the one row.
    assert len(_stages(client, closed_review)) == 2, "a stage of the ACTIVATED review was deleted"
    assert [s["stage_key"] for s in _stages(client, open_review)] == ["full-text"]


def test_the_trigger_refuses_a_delete_it_cannot_read_the_preconditions_for(client) -> None:
    """NON-VACUITY. An unbound tenant GUC must REFUSE, not permit.

    Every precondition the trigger reads — `screening_assignments`,
    `screening_decisions`, `screening_conflicts`, `inclusion_exports`,
    `review_state_history` — sits behind `FORCE ROW LEVEL SECURITY` keyed on
    `app.current_tenant`. With that GUC unbound each `EXISTS` returns false, and a
    trigger that concluded "no work exists" from that would permit every delete on
    every review — the failure mode `0012`'s pre-check hit and the one this shape
    inherits.

    Reached as the OWNER with the table's own policy suspended, because that is
    the only way the trigger can fire on a row the connection's tenant does not
    match; the runtime role cannot suspend a policy, which is the point of it
    being a non-owner.
    """
    review_id = _new_review(client)
    pv = _protocol(client, review_id, TYPO)
    assert _lock(client, review_id, pv).status_code in (200, 201)
    assert _activate(client, review_id, pv).status_code in (200, 201)
    stage_id = _stages(client, review_id)[0]["stage_id"]

    engine = sqlalchemy.create_engine(os.environ["MIGRATION_DATABASE_URL"])
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE review_stages NO FORCE ROW LEVEL SECURITY"))
        try:
            visible = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM review_stages WHERE stage_id = :s"), {"s": stage_id}
            ).scalar()
            assert visible == 1, "the row is invisible even with the policy suspended; this proves nothing"
            with pytest.raises(sqlalchemy.exc.DatabaseError) as caught:
                conn.execute(sqlalchemy.text("DELETE FROM review_stages WHERE stage_id = :s"), {"s": stage_id})
            assert "append" in str(caught.value).lower(), caught.value
        finally:
            conn.rollback()
            conn.execute(sqlalchemy.text("ALTER TABLE review_stages FORCE ROW LEVEL SECURITY"))
            conn.commit()
    engine.dispose()

    assert len(_stages(client, review_id)) == 2
