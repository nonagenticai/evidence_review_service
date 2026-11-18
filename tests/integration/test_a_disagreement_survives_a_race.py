"""Two reviewers disagreeing at the same instant — `ER-004.4`.

`_maybe_open_conflict` reads every current decision on the record, compares the
dispositions, and inserts a conflict when they differ. That is a check-then-act,
and the sessions running it are READ COMMITTED — the Postgres default, not
overridden anywhere in this service. So two reviewers deciding the same record
concurrently each see ONE decision, each fall short of `reviewers_required`, and
each open nothing. `uq_conflict_once` cannot save it: a unique key stops two
INSERTs of the same row, and here neither transaction reaches an INSERT.

Measured on `edc79c7`, `include` against `exclude`:

    AssertionError: two concurrent disagreeing decisions opened no conflict.
    Neither transaction ever saw the other's row, so the count never reached
    reviewers_required and the check-then-act in `_maybe_open_conflict` had
    nothing behind it. /conflicts={'items': [], 'count': 0, 'open': 0,
    'has_more': False, 'total': 0, 'total_open': 0}, second reviewer parked=False
    assert 0 == 1

The same disagreement taken sequentially opens one conflict, so the concurrency
is the whole of the difference.

## What this test does NOT claim

That the record is stuck forever. It is not: `GET /decisions` shows the lead both
dispositions, and either reviewer superseding their own decision re-enters this
function against a snapshot holding both rows and opens the conflict. What is
missing is anything that happens WITHOUT a human doing that — until one does,
the record is `not_yet_screened` with no `conflict_id` for a lead to resolve and
`POST /exports` refuses the review.

## The interleaving is constructed, not sampled

A barrier plus two commits would reproduce this only sometimes, and a test that
is red only sometimes is not a regression test. So the first reviewer's
transaction is held OPEN while the second runs: under the fix the second parks
on `pg_advisory_xact_lock` and its next statement takes a fresh READ COMMITTED
snapshot containing the first decision; with the lock removed it does not park —
`parked=False` above — and completes against a snapshot that cannot contain it.

:func:`test_two_reviewers_agreeing_at_the_same_instant_open_no_conflict` is the
negative control, and it passed before the fix as well as after: serialising the
check must not invent a disagreement between two reviewers who agree.
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
from sqlalchemy import create_engine
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

TENANT = tenant("tenant-conflict-race")
LEAD = "conflict-race-lead"
FIRST = "conflict-race-r1"
SECOND = "conflict-race-r2"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def undecided_record(client):  # noqa: ANN001, ANN201
    """An activated two-reviewer review with ONE record and no decisions yet."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "conflict-race", "external_key": f"cr-{uuid.uuid4()}", "title": "Conflict race"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
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
        content=json.dumps({"records": [{"title": "A disputed study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [FIRST, SECOND]},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(FIRST, "reviewer"),
    ).json()["items"][0]["review_record_id"]
    return {
        "review_id": uuid.UUID(review_id),
        "stage_id": uuid.UUID(stage_id),
        "record_id": uuid.UUID(record_id),
        "review_id_str": review_id,
    }


def _decide_concurrently(
    undecided_record: dict,
    *,
    first: tuple[str, str | None],
    second: tuple[str, str | None],
) -> dict[str, object]:
    """Run two reviewers' decisions with the FIRST one's transaction still open.

    The interleaving is constructed rather than sampled. The first reviewer's
    session records its decision and does NOT commit; the second reviewer is
    started in a thread and, under a correct implementation, parks. Whether it
    parked is read from `thread.is_alive()` after a bounded join — the same
    construction `test_a_round_binding_survives_a_race` uses, and for the same
    reason: with the serialisation removed the loser does not park, it finishes
    in milliseconds, so the test is red in the direction that matters and does
    not depend on how long anything takes to be green.
    """
    from actor import Actor, ActorRole, bind_actor
    from application.services.screening import record_decision

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    outcome: dict[str, object] = {}
    second_started = threading.Event()

    def _decide(session: Session, subject: str, disposition: str, reason: str | None) -> dict:
        actor = Actor(tenant_id=TENANT, subject_id=subject, role=ActorRole.REVIEWER)
        bind_actor(session, actor)
        return record_decision(
            session,
            actor,
            undecided_record["review_id"],
            stage_id=undecided_record["stage_id"],
            round_number=1,
            review_record_id=undecided_record["record_id"],
            disposition=disposition,
            exclusion_reason_code=reason,
            rationale="screened",
        )

    winner_session = Session(engine)
    winner_session.begin()
    outcome["first"] = _decide(winner_session, FIRST, *first)

    def loser() -> None:
        session = Session(engine)
        try:
            session.begin()
            second_started.set()
            outcome["second"] = _decide(session, SECOND, *second)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - a refusal would itself be the finding
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            session.rollback()
        finally:
            session.close()

    thread = threading.Thread(target=loser, daemon=True)
    thread.start()
    second_started.wait(timeout=10)
    thread.join(timeout=2)
    outcome["parked"] = thread.is_alive()

    winner_session.commit()
    winner_session.close()
    thread.join(timeout=30)
    engine.dispose()
    return outcome


def _conflicts(client, review_id: str) -> dict:  # noqa: ANN001, ANN201
    return client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()


def test_two_reviewers_disagreeing_at_the_same_instant_still_open_one_conflict(client, undecided_record):  # noqa: ANN001, ANN201
    """`include` and `exclude`, concurrently, must produce exactly one conflict."""
    outcome = _decide_concurrently(
        undecided_record,
        first=("include", None),
        second=("exclude", "wrong_design"),
    )
    assert "error" not in outcome, f"the second reviewer's decision was refused: {outcome}"

    conflicts = _conflicts(client, undecided_record["review_id_str"])
    assert conflicts["total"] == 1, (
        "two concurrent disagreeing decisions opened no conflict. Neither transaction ever saw "
        "the other's row, so the count never reached reviewers_required and the check-then-act in "
        f"`_maybe_open_conflict` had nothing behind it. /conflicts={conflicts}, "
        f"second reviewer parked={outcome['parked']}"
    )
    assert outcome["parked"], (
        "the second reviewer was not serialised: it finished while the first reviewer's "
        "transaction was still open, so the conflict check ran against a snapshot that could not "
        "contain the first decision"
    )

    flow = client.get(f"/api/reviews/{undecided_record['review_id_str']}/flow", headers=headers()).json()
    assert flow["counts"]["records_screened"] == 0, flow["counts"]
    assert flow["counts"]["not_yet_screened"] == 1, flow["counts"]


def test_two_reviewers_agreeing_at_the_same_instant_open_no_conflict(client, undecided_record):  # noqa: ANN001, ANN201
    """The negative control. Serialising the check must not invent a disagreement."""
    outcome = _decide_concurrently(
        undecided_record,
        first=("include", None),
        second=("include", None),
    )
    assert "error" not in outcome, f"the second reviewer's decision was refused: {outcome}"

    conflicts = _conflicts(client, undecided_record["review_id_str"])
    assert conflicts["total"] == 0, f"two agreeing reviewers opened a conflict: {conflicts}"

    flow = client.get(f"/api/reviews/{undecided_record['review_id_str']}/flow", headers=headers()).json()
    assert flow["counts"]["records_screened"] == 1, flow["counts"]
    assert flow["counts"]["included"] == 1, flow["counts"]
