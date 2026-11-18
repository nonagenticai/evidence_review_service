"""Three findings from the adversarial verification of `#42` (`e9559c3`).

`#42` made `stage_order` load-bearing: the ledger settles a record at the highest
`StagePosition` it has decisions at. Three things were not true when it did.

## 1. Two stages of one review can share a `stage_order`

`review_stages` is unique on `(review_id, stage_key)` only, and
`lock_protocol_version` numbers stages `enumerate(..., start=1)` per protocol
version. So locking a second protocol version whose stage keys differ mints
stages ordered 1, 2 alongside the existing 1, 2 — measured, 201 all the way, and
the `:activate` that follows can 409 without undoing it.

`StagePosition` then breaks the tie on `stage_key`, alphabetically. That is
deterministic, and `flow_derivation`'s own docstring says the tie-break "carries
NO meaning" and that ordering by the protocol's `version_number` is "the change
to make" if two protocol versions are ever screened against in one review. They
now can be. Until that change exists, an alphabetical accident decides which
stage settles a record — and the measured effect was a unanimous exclusion under
the current protocol being discarded in favour of an inclusion under the old one,
sealed at 201 with an empty `omissions` array.

Refused at both ends, the same shape as every other conservation rule here: the
lock refuses to mint the colliding stage, and the derivation refuses to order a
review that already holds one.

## 2. The export's conflict gate read the protocol's `reviewers_required`

`#42` moved the LEDGER onto the stage's number and said "the two cannot
disagree". `_unresolved_conflicts` was not moved. A stage declaring 2 inside a
protocol declaring 3 opens its conflict on 2 and then needs 3 agreeing votes to
clear it — so `/flow` reports the review finished while `/exports` refuses over a
disagreement that has been settled.

## 3. `review_stages` was said to be append-only. It was not.

`flow.py` states it as a fact and reasons from it. Measured: no triggers at all —
the only history table with none — and the runtime role held `UPDATE` and
`DELETE`. Since `#42` a `canonical_counts_hash` sealed into an append-only export
depended on a column anything holding the app credential could rewrite.
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

TENANT = tenant("tenant-ledger-order")
LEAD = "ledger-order-lead"


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


def _protocol(client, review_id: str, stage_definitions: list[dict], reviewers_required: int = 2):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": stage_definitions,
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population"],
            "reviewers_required": reviewers_required,
        },
        headers=headers(),
    ).json()["protocol_version_id"]


def _review(client, stage_definitions: list[dict], reviewers_required: int = 2) -> str:  # noqa: ANN001
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "ledger-order", "external_key": f"lo-{uuid.uuid4()}", "title": "Ledger order"},
        headers=headers(),
    ).json()["review_id"]
    pv = _protocol(client, review_id, stage_definitions, reviewers_required)
    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{pv}:lock", headers=headers())
    assert locked.status_code in (200, 201), locked.text
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": pv, "rationale": "go"},
        headers=headers(),
    )
    assert activated.status_code in (200, 201), activated.text
    return review_id


def _import_and_seal(client, review_id: str, n: int = 1) -> None:  # noqa: ANN001
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {"records": [{"title": f"Study {i}", "year": 2021, "doi": f"10.1000/{uuid.uuid4()}"} for i in range(n)]}
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text


def _stages(client, review_id: str) -> list[dict]:  # noqa: ANN001
    return client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]


def _assign(client, review_id: str, stage_id: str, who: list[str], round_number: int = 1):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": who, "round_number": round_number},
        headers=headers(),
    )


def _decide(  # noqa: ANN201
    client,  # noqa: ANN001
    review_id: str,
    stage_id: str,
    record_id: str,
    who: str,
    disposition: str,
    *,
    round_number: int = 1,
    reason: str | None = None,
    supersedes: str | None = None,
):
    return client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "round_number": round_number,
            "review_record_id": record_id,
            "disposition": disposition,
            "exclusion_reason_code": reason,
            "rationale": "probe",
            "supersedes_decision_id": supersedes,
        },
        headers=headers(who, "reviewer"),
    )


def _first_record(client, review_id: str, stage_id: str, who: str) -> str:  # noqa: ANN001
    got = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(who, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()["items"][0]["review_record_id"]


# --------------------------------------------------------------------------
# 1. Two stages sharing a stage_order
# --------------------------------------------------------------------------


def test_locking_a_second_protocol_version_cannot_collide_with_the_existing_stage_order(client) -> None:
    """THE WRITE. The state must not be creatable in the first place.

    Measured before this change: 201 on create, 200 on lock, and the review then
    held stages ordered 1, 2, 1, 2. The `:activate` that follows returned 409,
    which is the trap — the caller sees a refusal and the corrupting rows are
    already committed.
    """
    review_id = _review(client, [{"key": "b-title", "name": "B title"}, {"key": "b-full", "name": "B full"}])
    pv2 = _protocol(client, review_id, [{"key": "a-title", "name": "A title"}, {"key": "a-full", "name": "A full"}])

    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{pv2}:lock", headers=headers())
    assert locked.status_code == 409, (
        f"locking a second protocol version minted stages that collide on stage_order (HTTP {locked.status_code}). "
        "An alphabetical accident then decides which stage settles a record."
    )

    orders = [s["stage_order"] for s in _stages(client, review_id)]
    assert sorted(orders) == [1, 2], f"the review holds stages that cannot be totally ordered: {orders}"


def test_the_ledger_refuses_a_review_whose_stages_cannot_be_ordered(client) -> None:
    """THE BACKSTOP. For rows written before the lock refused them.

    The stage is inserted directly, as the owner with the tenant GUC bound —
    `FORCE ROW LEVEL SECURITY` applies to the owner too and an unbound GUC writes
    nothing silently. The insert's row count is asserted for that reason.
    """
    review_id = _review(client, [{"key": "ta", "name": "TA"}, {"key": "ft", "name": "FT"}])
    _import_and_seal(client, review_id, 1)
    assert client.get(f"/api/reviews/{review_id}/flow", headers=headers()).status_code == 200

    existing = _stages(client, review_id)[0]
    engine = sqlalchemy.create_engine(os.environ["MIGRATION_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        # The real protocol version: `protocol_version_id` carries a foreign key,
        # so a fabricated uuid would be refused by the FK and the test would
        # report a corrupt review it never managed to create.
        pv = conn.execute(
            sqlalchemy.text("SELECT protocol_version_id FROM review_stages WHERE stage_id = :s"),
            {"s": existing["stage_id"]},
        ).scalar()
        assert pv is not None, "could not read the stage back; the tenant GUC is probably unbound"
        written = conn.execute(
            sqlalchemy.text(
                "INSERT INTO review_stages "
                "(stage_id, review_id, tenant_id, protocol_version_id, stage_key, stage_order, name, "
                " reviewers_required) "
                "VALUES (:sid, :review, :tenant, :pv, 'a-shadow', :order, 'A shadow', 2)"
            ),
            {
                "sid": str(uuid.uuid4()),
                "review": review_id,
                "tenant": TENANT,
                "pv": pv,
                "order": existing["stage_order"],
            },
        ).rowcount
    engine.dispose()
    assert written == 1, "the corrupting insert affected no rows, so this test would prove nothing"

    refused = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert refused.status_code == 500, (
        f"the ledger ordered a review whose stages have no total order (HTTP {refused.status_code}). "
        "An alphabetical tie-break then decides which stage settles a record."
    )

    # AND THE OPERATOR CAN READ IT. `ConservationError` subclasses
    # `AssertionError`, so nothing mapped it and this was a bare
    # `Internal Server Error` with the message — which names both tied stages and
    # the position they collide at — discarded inside the process. A refusal
    # nobody can read is not a refusal they can act on.
    problem = refused.json()
    assert problem["code"] == "conservation_violation", problem
    assert "a-shadow" in problem["detail"] and existing["stage_key"] in problem["detail"], problem

    # Every route that goes through `_stage_positions` must answer the same way,
    # not just the one this test happened to pick. `/conflicts` and `:close`
    # became reachable only today — `list_conflicts` borrows
    # `_unresolved_conflicts`, and `transition(closed)` gates on it.
    for name, response in (
        ("/exports", client.post(f"/api/reviews/{review_id}/exports", headers=headers())),
        ("/flow:rebuild", client.post(f"/api/reviews/{review_id}/flow:rebuild", headers=headers())),
    ):
        assert response.status_code == 500, f"{name} answered {response.status_code}: {response.text}"
        assert response.json().get("code") == "conservation_violation", (
            f"{name} returned a bare 500 with no problem body: {response.text[:200]}"
        )

    # `/conflicts` and `:close` are NOT in that list, and measuring rather than
    # assuming is why. Both reach `_stage_positions` only through
    # `_unresolved_conflicts`, which returns early when the review holds no
    # conflicts at all — so on this fixture they answer 200 and are right to.
    # They 500 on a review that has a disagreement, which is the shape the
    # adversarial report measured; asserting them here would have made this test
    # about the fixture rather than about the routes.
    assert client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).status_code == 200


# --------------------------------------------------------------------------
# 2. The export's conflict gate
# --------------------------------------------------------------------------


def test_a_settled_disagreement_does_not_block_the_export_on_the_protocols_number(client) -> None:
    """The stage declares 2 inside a protocol declaring 3.

    The conflict opens on the stage's number and must clear on the stage's
    number. Before this, `/flow` reported the review finished while `/exports`
    refused over a disagreement that had been settled by consensus.
    """
    review_id = _review(
        client,
        [{"key": "ta", "name": "TA", "reviewers_required": 2}],
        reviewers_required=3,
    )
    _import_and_seal(client, review_id, 1)
    stage_id = _stages(client, review_id)[0]["stage_id"]
    assert _assign(client, review_id, stage_id, ["r1", "r2"]).status_code in (200, 201)
    record_id = _first_record(client, review_id, stage_id, "r1")

    assert _decide(client, review_id, stage_id, record_id, "r1", "include").status_code == 201
    disagreed = _decide(client, review_id, stage_id, record_id, "r2", "exclude", reason="wrong_design")
    assert disagreed.status_code == 201, disagreed.text

    conflicts = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()
    assert conflicts["total"] >= 1, f"the fixture opened no conflict, so this test is about nothing: {conflicts}"

    # r2 comes round to r1's view. That is consensus, and it is what the stage's
    # own number of two reviewers agreeing means.
    corrected = _decide(
        client,
        review_id,
        stage_id,
        record_id,
        "r2",
        "include",
        supersedes=disagreed.json()["decision_id"],
    )
    assert corrected.status_code == 201, corrected.text

    counts = client.get(f"/api/reviews/{review_id}/flow", headers=headers()).json()["counts"]
    assert counts["not_yet_screened"] == 0 and counts["included"] == 1, counts

    exported = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert exported.status_code == 201, (
        "the ledger says the review is finished and the export says a disagreement is outstanding. "
        "The conflict opened on the stage's two reviewers and was cleared against the protocol's three: "
        + exported.text
    )


def test_an_open_disagreement_still_blocks_the_export(client) -> None:
    """NEGATIVE CONTROL. Reading the stage's number must not disarm the gate.

    Without this, "clear the conflict on the stage's number" is indistinguishable
    from "clear the conflict always", and the check `#42`'s docstring says must
    not be deleted would have been deleted by accident.
    """
    review_id = _review(client, [{"key": "ta", "name": "TA", "reviewers_required": 2}], reviewers_required=3)
    _import_and_seal(client, review_id, 1)
    stage_id = _stages(client, review_id)[0]["stage_id"]
    _assign(client, review_id, stage_id, ["r1", "r2"])
    record_id = _first_record(client, review_id, stage_id, "r1")

    assert _decide(client, review_id, stage_id, record_id, "r1", "include").status_code == 201
    assert _decide(client, review_id, stage_id, record_id, "r2", "exclude", reason="wrong_design").status_code == 201

    exported = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert exported.status_code == 422, (
        f"an unresolved, unadjudicated disagreement sealed (HTTP {exported.status_code}): {exported.text}"
    )


# --------------------------------------------------------------------------
# 3. review_stages is append-only
# --------------------------------------------------------------------------


def test_the_runtime_role_cannot_rewrite_a_stage(client) -> None:
    """`flow.py` reasons from "review_stages is append-only". Make it true.

    Since `#42` the `canonical_counts_hash` sealed into an append-only export
    depends on `stage_order`. Measured before this change: `review_stages` was
    the only history table in the schema with no trigger at all, and the runtime
    role held `UPDATE` and `DELETE`. Two rows rewritten as `evidence_review_app`
    inverted a sealed export's counts while `:verify` still answered verified.

    Asserted against the RUNTIME role — the one the service actually connects as
    — because a check run as the owner would pass against no enforcement.
    """
    review_id = _review(client, [{"key": "ta", "name": "TA"}, {"key": "ft", "name": "FT"}])
    stage_id = _stages(client, review_id)[0]["stage_id"]

    engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(sqlalchemy.text("SELECT set_config('app.actor_role', 'lead', true)"))

        # BOTH PRECONDITIONS, ASSERTED. A refusal test whose statement matches no
        # rows raises nothing and reads exactly like a guard that works: this
        # failed in CI as `DID NOT RAISE` and nothing in it could say whether the
        # trigger was missing or the row was invisible.
        installed = conn.execute(
            sqlalchemy.text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'review_stages'::regclass AND NOT tgisinternal"
            )
        ).scalar()
        assert installed, "migration 0011's append-only trigger is not installed on this database"
        visible = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM review_stages WHERE stage_id = :s"), {"s": stage_id}
        ).scalar()
        assert visible == 1, (
            "this connection cannot see the stage, so the statements below would match no rows and "
            "raise nothing whatever the guard did"
        )

        for statement in (
            "UPDATE review_stages SET stage_order = 99 WHERE stage_id = :s",
            "DELETE FROM review_stages WHERE stage_id = :s",
        ):
            # RE-BOUND EVERY ITERATION, and this is not tidying.
            #
            # `set_config(..., true)` is TRANSACTION-local, and the `rollback()`
            # at the foot of the previous iteration ends the transaction it was
            # bound in. So the second statement ran with `app.current_tenant`
            # unbound, the tenant policy hid the row, and it matched NOTHING —
            # the exact vacuity the assertions above this loop exist to rule out,
            # reintroduced between them and the statement they guard.
            #
            # It was invisible while `0011`'s REVOKE covered both verbs: an
            # `InsufficientPrivilege` is raised before any row is looked at, so
            # the DELETE leg raised for a reason that had nothing to do with the
            # row it named. `0013` re-grants DELETE — scoped by the now-conditional
            # trigger — and the leg immediately failed `DID NOT RAISE`, which is
            # what surfaced this. Re-binding makes the DELETE reach the trigger,
            # which is what this test claims to be measuring.
            conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(sqlalchemy.text("SELECT set_config('app.actor_role', 'lead', true)"))
            matched = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM review_stages WHERE stage_id = :s"), {"s": stage_id}
            ).scalar()
            assert matched == 1, f"{statement.split()[0]} would match no rows, so it would prove nothing"

            with pytest.raises(sqlalchemy.exc.DatabaseError) as caught:
                conn.execute(sqlalchemy.text(statement), {"s": stage_id})
                conn.commit()
            conn.rollback()
            assert "append" in str(caught.value).lower() or "permission" in str(caught.value).lower(), (
                f"{statement.split()[0]} was refused for an unrelated reason: {caught.value}"
            )
    engine.dispose()

    orders = sorted(s["stage_order"] for s in _stages(client, review_id))
    assert orders == [1, 2], f"a stage was rewritten despite the refusals: {orders}"
