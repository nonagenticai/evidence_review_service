"""Two leads assigning the same round at the same instant — `ER-004.1`.

`create_assignments` refuses to re-point a round that already holds assignments
from a different corpus (`tests/api/test_a_round_is_bound_to_one_corpus.py`).
That refusal is a check-then-act: read the round's corpora, then insert. Between
those two statements a second caller naming a DIFFERENT corpus can read the same
empty round, pass the same check, and insert — and because
`uq_assignment_once` excludes `corpus_version_id`, nothing at the database
notices. The round then holds two corpora and both callers were told `201`,
which is the exact state the guard exists to make unreachable.

So the guard takes `pg_advisory_xact_lock` on `(review, stage, round)` first.
Advisory rather than a row lock because there is no row at that grain, and
because `0011` revoked `UPDATE, DELETE ON review_stages` from the runtime role —
`SELECT ... FOR UPDATE` needs one of those. Transaction-scoped rather than
session-scoped because the service runs behind PgBouncer in transaction pooling,
where a session lock would leak onto the next borrower of the connection.

## The interleaving is constructed, not sampled

The winner holds an open transaction; the loser is started and parks on the lock;
`thread.is_alive()` after a two-second join is the assertion. With the lock
removed the loser does not park — it completes a three-record insert in
milliseconds — so the test is red in the direction that matters and does not
depend on how long anything takes to be green.

Measured with the `pg_advisory_xact_lock` line commented out:

    AssertionError: the second lead was not serialised: it finished while the
    first lead's transaction was still open ... round holds
    {'<v1>': 3, '<v2>': 3}
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.tenants import tenant

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-round-race")
LEAD = "round-race-lead"
FIRST_REVIEWER = "round-race-reviewer-a"
SECOND_REVIEWER = "round-race-reviewer-b"


def headers() -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": LEAD, "X-Actor-Role": "lead"}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def two_corpora(client):  # noqa: ANN001, ANN201
    """An activated review with two sealed corpus versions and no assignments yet.

    A second corpus version needs only a changed manifest, so one more imported
    record is the cheapest way to reach one — the merge machinery is irrelevant
    to this race.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "round-race", "external_key": f"rr-{uuid.uuid4()}", "title": "Round race"},
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
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )

    def _import(n: int) -> None:
        imported = client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps(
                {
                    "records": [
                        {
                            "title": f"Race paper {n}.{i}",
                            "year": 2020,
                            "doi": f"10.1056/NEJMoa20{n}.{uuid.uuid4().hex[:8]}",
                        }
                        for i in range(3)
                    ]
                }
            ),
            headers={**headers(), "Content-Type": "application/octet-stream"},
        )
        assert imported.status_code in (200, 201), imported.text

    _import(1)
    v1 = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]
    _import(2)
    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text
    v2 = resealed.json()["corpus_version_id"]
    assert v2 != v1

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    return {
        "review_id": uuid.UUID(review_id),
        "stage_id": uuid.UUID(stage_id),
        "v1": uuid.UUID(v1),
        "v2": uuid.UUID(v2),
    }


def _corpora_of_round(review_id: uuid.UUID, round_number: int) -> dict[str, int]:
    """Read as the runtime role with the tenant bound — an unbound read matches nothing."""
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            rows = conn.execute(
                text(
                    "SELECT corpus_version_id, count(*) FROM screening_assignments "
                    "WHERE review_id = :r AND round_number = :n GROUP BY 1"
                ),
                {"r": str(review_id), "n": round_number},
            ).all()
    finally:
        engine.dispose()
    return {str(row[0]): row[1] for row in rows}


def test_a_concurrent_second_corpus_cannot_slip_past_the_binding(two_corpora):  # noqa: ANN001, ANN201
    """The second lead must serialise behind the first, then be refused.

    Refused and not merely serialised: waiting and then writing v2 into a round
    holding v1 would be the same split round arrived at more slowly.
    """
    from actor import Actor, ActorRole, bind_actor
    from api.problem_details import StateConflictError
    from application.services.screening import create_assignments

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    loser_result: dict[str, object] = {}
    loser_started = threading.Event()

    winner_session = Session(engine)
    winner_session.begin()
    bind_actor(winner_session, actor)
    won = create_assignments(
        winner_session,
        actor,
        two_corpora["review_id"],
        stage_id=two_corpora["stage_id"],
        round_number=1,
        reviewer_subject_ids=[FIRST_REVIEWER],
        corpus_version_id=two_corpora["v1"],
    )
    assert won["assignments_created"] == 3, won

    def loser() -> None:
        session = Session(engine)
        try:
            session.begin()
            bind_actor(session, actor)
            loser_started.set()
            # Parks on `pg_advisory_xact_lock` until the winner's transaction
            # ends. Without that lock it reads an empty round, passes the guard
            # and inserts v2 alongside the winner's uncommitted v1.
            loser_result["body"] = create_assignments(
                session,
                actor,
                two_corpora["review_id"],
                stage_id=two_corpora["stage_id"],
                round_number=1,
                reviewer_subject_ids=[SECOND_REVIEWER],
                corpus_version_id=two_corpora["v2"],
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the refusal IS the finding
            loser_result["error"] = f"{type(exc).__name__}: {exc}"
            session.rollback()
        finally:
            session.close()

    thread = threading.Thread(target=loser, daemon=True)
    thread.start()
    loser_started.wait(timeout=10)
    thread.join(timeout=2)
    parked = thread.is_alive()

    winner_session.commit()
    winner_session.close()
    thread.join(timeout=30)
    engine.dispose()

    held = _corpora_of_round(two_corpora["review_id"], 1)
    assert parked, (
        "the second lead was not serialised: it finished while the first lead's transaction was "
        f"still open, so the check-then-act had no lock behind it. Round holds {held}"
    )
    assert "error" in loser_result, f"the second lead was not refused: {loser_result}"
    assert loser_result["error"].startswith(StateConflictError.__name__), loser_result  # type: ignore[union-attr]
    assert held == {str(two_corpora["v1"]): 3}, (
        f"round 1 ended up holding {held}; the binding did not survive two concurrent leads"
    )
