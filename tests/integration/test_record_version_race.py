"""Two leads merging onto the same survivor at once — `ER-003.4`.

`apply_survivorship` computes `version_number = previous + 1` from a read, which
is racy on its own. It is reachable: two clusters produced by two dedup runs can
name the same record as survivor, and `resolve_cluster` is per-cluster.

Two structural guards already exist in `0003` and neither is enough by itself:
`uq_record_version_number` and `uq_record_version_supersession_linear` make a
FORK impossible, so the chain can never branch — but they achieve that by
refusing the loser, which surfaces as `409 state_conflict` on a request that was
perfectly legitimate. The lead's choice would be silently discarded and the only
signal would be an error telling them their request conflicted with the state of
a review they were correctly editing.

`SELECT ... FOR UPDATE` on `review_records` is what turns that into a queue: the
loser blocks, re-reads the tail (now v2), and writes v3 superseding it. Both
choices land, in one linear attributable chain, tail wins — the same semantics
`duplicate_resolutions` already has for merge -> split -> merge.

`ON CONFLICT DO NOTHING` + re-select, which `current_flow_revision` uses, would be
wrong here. That idiom is correct there because the loser's row is the row it
wanted. It is false for survivorship: the loser wants ITS OWN field choices
applied, and silently accepting the winner's version would discard an attributed
human decision without telling anybody.

Driven deterministically, following `test_flow_revision_race.py`: the loser parks
on the row lock until the winner commits, so the interleaving is constructed
rather than sampled.
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

TENANT = tenant("tenant-version-race")
LEAD = "version-race-lead"


@pytest.fixture
def survivor():  # noqa: ANN201
    """One imported record, plus two other versions to survive fields from."""
    from actor import Actor, ActorRole
    from application.services.imports import import_payload
    from application.services.reviews import activate, create_protocol_version, create_review, lock_protocol_version
    from database import actor_scope

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        review = create_review(
            session,
            actor,
            project_id="version-race",
            external_key=f"vrace-{uuid.uuid4()}",
            title="Two merges at once",
        )
        protocol = create_protocol_version(
            session,
            actor,
            review,
            inclusion_criteria={},
            exclusion_criteria={},
            stage_definitions=[{"key": "ta", "name": "TA"}],
            exclusion_reason_vocabulary=["wrong_design"],
            reviewers_required=1,
        )
        lock_protocol_version(session, actor, protocol)
        activate(session, actor, review, protocol_version_id=protocol.protocol_version_id, rationale="go")
        import json as _json

        import_payload(
            session,
            actor,
            review.review_id,
            source_kind="canonical_json",
            payload=_json.dumps(
                {
                    "records": [
                        {"title": "Survivor", "year": 2021, "doi": f"10.1000/s{uuid.uuid4()}"},
                        {"title": "Source A", "year": 2022, "doi": f"10.1000/a{uuid.uuid4()}"},
                        {"title": "Source B", "year": 2023, "doi": f"10.1000/b{uuid.uuid4()}"},
                    ]
                }
            ).encode(),
        )
        review_id = review.review_id

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            rows = conn.execute(
                text(
                    "SELECT review_record_id, review_record_version_id, title "
                    "FROM review_record_versions WHERE review_id = :r ORDER BY title"
                ),
                {"r": str(review_id)},
            ).all()
    finally:
        engine.dispose()

    by_title = {r[2]: r for r in rows}
    return {
        "review_id": review_id,
        "survivor_record_id": by_title["Survivor"][0],
        "source_a_version_id": by_title["Source A"][1],
        "source_b_version_id": by_title["Source B"][1],
    }


def _versions(record_id: uuid.UUID) -> list[tuple]:
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            return conn.execute(
                text(
                    "SELECT version_number, review_record_version_id, supersedes_version_id, title "
                    "FROM review_record_versions WHERE review_record_id = :r ORDER BY version_number"
                ),
                {"r": str(record_id)},
            ).all()
    finally:
        engine.dispose()


def test_two_concurrent_merges_onto_one_survivor_both_land(survivor):  # noqa: ANN001
    """Neither lead is refused, and the chain stays linear.

    Without the row lock the loser takes `uq_record_version_number` and gets a
    `409` — its field choice discarded, on a request nothing was wrong with.
    """
    from actor import Actor, ActorRole, bind_actor
    from application.services.imports import apply_survivorship
    from models.records import ReviewRecordVersion

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    loser_result: dict[str, object] = {}
    loser_started = threading.Event()

    winner_session = Session(engine)
    winner_session.begin()
    bind_actor(winner_session, actor)
    source_a = winner_session.get(ReviewRecordVersion, survivor["source_a_version_id"])
    winner_version = apply_survivorship(
        winner_session,
        actor,
        survivor["review_id"],
        survivor_record_id=survivor["survivor_record_id"],
        sources={"title": source_a},
        resolution_id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        subject=LEAD,
    )
    winner_version_id = winner_version.review_record_version_id
    assert winner_version.version_number == 2

    def loser() -> None:
        session = Session(engine)
        try:
            session.begin()
            bind_actor(session, actor)
            source_b = session.get(ReviewRecordVersion, survivor["source_b_version_id"])
            loser_started.set()
            # Parks on the `review_records` row lock until the winner commits.
            version = apply_survivorship(
                session,
                actor,
                survivor["review_id"],
                survivor_record_id=survivor["survivor_record_id"],
                sources={"title": source_b},
                resolution_id=uuid.uuid4(),
                cluster_id=uuid.uuid4(),
                subject=LEAD,
            )
            loser_result["version_number"] = version.version_number
            loser_result["supersedes"] = version.supersedes_version_id
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the failure IS the finding
            loser_result["error"] = f"{type(exc).__name__}: {exc}"
            session.rollback()
        finally:
            session.close()

    thread = threading.Thread(target=loser, daemon=True)
    thread.start()
    loser_started.wait(timeout=10)
    thread.join(timeout=1)
    winner_session.commit()
    winner_session.close()
    thread.join(timeout=30)
    engine.dispose()

    assert "error" not in loser_result, (
        f"the second lead's merge was refused: {loser_result['error']}. Their field choice is gone, "
        "and nothing about their request was wrong."
    )
    assert loser_result["version_number"] == 3, (
        f"the loser wrote version {loser_result['version_number']}; it should have re-read the tail "
        "the winner committed and superseded it"
    )
    assert loser_result["supersedes"] == winner_version_id, (
        "the loser did not supersede the winner's version — the chain has forked, or it superseded v1"
    )

    chain = _versions(survivor["survivor_record_id"])
    assert [row[0] for row in chain] == [1, 2, 3], f"the version chain is not 1,2,3: {chain}"
    assert chain[2][3] == "Source B", "the tail does not carry the LAST lead's choice"
    assert chain[1][2] == chain[0][1], "v2 does not supersede v1"
    assert chain[2][2] == chain[1][1], "v3 does not supersede v2"
