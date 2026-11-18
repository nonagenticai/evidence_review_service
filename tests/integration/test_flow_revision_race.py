"""Two people opening the same review's ledger at once — `ER-005`.

`current_flow_revision` opens the revision on first use, and it is reached from
the plain `GET /api/reviews/{id}/flow`: `compute_flow(persist=False)` still needs
a revision to report, and `actor_scope` commits the transaction the read ran in.
So a GET writes exactly one row, once, and `uq_flow_revision_number` is a hard
unique.

With a plain `session.add` the two first readers of a review raced it. One won;
the other took a unique violation, `_integrity_handler` turned it into
`409 state_conflict`, and somebody who had merely opened a page was told their
request conflicted with the current state of the review. There is no retry that
helps and nothing they did wrong — and it happens exactly once per review, on
first use, which is the moment two people are most likely to be looking at it
together.

The race is driven deterministically rather than by launching two requests and
hoping they overlap: the losing INSERT blocks on the unique index until the
winner commits, so the interleaving can be constructed exactly instead of
sampled.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

import pytest
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

TENANT = tenant("tenant-flow-race")
LEAD = "race-lead"


@pytest.fixture
def review_id() -> uuid.UUID:
    """A review with nothing else on it. The race is about first use."""
    from actor import Actor, ActorRole
    from application.services.reviews import create_review
    from database import actor_scope

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        review = create_review(
            session,
            actor,
            project_id="flow-race",
            external_key=f"race-{uuid.uuid4()}",
            title="Two readers at once",
        )
        return review.review_id


def _revision_count(review_id: uuid.UUID) -> int:
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            return conn.execute(
                text("SELECT count(*) FROM flow_revisions WHERE review_id = :r"), {"r": str(review_id)}
            ).scalar_one()
    finally:
        engine.dispose()


def test_two_first_readers_of_a_review_both_succeed(review_id):
    """Both callers get a revision; the database holds exactly one row.

    The loser's `ON CONFLICT DO NOTHING` returns nothing, so it re-selects — and
    reads the winner's row, which is the row it wanted. `01-…:504-505` says the
    revision number is a function of the reopen history, so two readers asking
    at the same instant are asking for the SAME revision, and answering them
    both with it is not a compromise: it is the correct answer twice.
    """
    from actor import Actor, ActorRole, bind_actor
    from application.services.flow import current_flow_revision

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    loser_result: dict[str, object] = {}
    loser_started = threading.Event()

    winner_session = Session(engine)
    winner_session.begin()
    bind_actor(winner_session, actor)
    # Read off the identifier now: the session is closed before it is compared,
    # and an expired attribute would raise instead of failing the assertion.
    winner_revision_id = current_flow_revision(winner_session, actor, review_id).flow_revision_id

    def loser() -> None:
        session = Session(engine)
        try:
            session.begin()
            bind_actor(session, actor)
            loser_started.set()
            # Blocks here until the winner commits: the unique index makes the
            # second INSERT wait on the first writer's outcome.
            revision = current_flow_revision(session, actor, review_id)
            loser_result["flow_revision_id"] = revision.flow_revision_id
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the failure IS the finding
            loser_result["error"] = f"{type(exc).__name__}: {exc}"
            session.rollback()
        finally:
            session.close()

    thread = threading.Thread(target=loser, daemon=True)
    thread.start()
    loser_started.wait(timeout=10)
    # The loser is now inside its INSERT and parked on the index. Releasing the
    # winner is what decides its fate.
    thread.join(timeout=1)
    winner_session.commit()
    winner_session.close()
    thread.join(timeout=30)
    engine.dispose()

    assert "error" not in loser_result, f"the second reader was refused: {loser_result['error']}"
    assert loser_result["flow_revision_id"] == winner_revision_id, (
        "the two readers were handed different revisions of the same review"
    )
    assert _revision_count(review_id) == 1


def test_the_ledger_read_is_the_door_the_write_is_behind(review_id):
    """A GET commits a row, which is why any of this matters.

    Asserted explicitly so that a future change making `GET /flow` read-only does
    not silently make the test above vacuous — it would keep passing while
    measuring nothing.
    """
    from actor import Actor, ActorRole
    from application.services.flow import compute_flow
    from database import actor_scope

    assert _revision_count(review_id) == 0
    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        compute_flow(session, actor, review_id, persist=False)
    assert _revision_count(review_id) == 1, "the plain ledger read no longer opens the revision"
