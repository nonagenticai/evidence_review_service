"""Three uniqueness guards were `SELECT`-then-`INSERT` with no index behind them.

`create_export`, `import_payload` and `create_run` each ask "does a row with this
key already exist?" and, seeing none, insert one. Under READ COMMITTED — the
default this service runs at — two transactions take that SELECT before either
commits, both see nothing, and both insert. Verified against the live database
before writing this file: `\\d import_batches` and `\\d inclusion_exports` show
**only the primary key**, and `ix_dedup_runs_digest` on `deduplication_runs`
exists but is **NOT unique**. Nothing under any of the three checks enforces what
the check is for.

The three are not equally bad:

* `inclusion_exports`, keyed on `manifest_hash` — two `POST /exports` mint two
  permanent seals sharing one manifest, both answering `verified: true`, and the
  append-only trigger refuses `DELETE`, so the duplicate can never be removed.
  This makes the whole of #53 a TOCTOU: it collapses the second seal only when
  the second caller can SEE the first.
* `import_batches`, keyed on `(review_id, raw_payload_digest)` — the worst, and
  the one with a second failure behind it. 400 records in becomes 800 sealed,
  every downstream count self-consistent because `identified_total` is derived
  from `import_rows`; and every LATER import of those same bytes then reaches
  `scalar_one_or_none()` on two rows, raises `MultipleResultsFound`, and leaves
  the route with a bare `500 text/plain` — outside the problem+json contract
  every other error in this service honours.
* `deduplication_runs`, keyed on `(review_id, input_digest)` — two cluster sets
  over the same corpus and config, and `resolve_cluster` can be pointed at
  either, so "which cluster set is current?" gets two answers.

## Why the race is constructed rather than sampled

Two threads on a barrier "hoping to overlap" measures the scheduler. The
interleaving here is built: the winner runs its whole call and does NOT commit;
the loser then runs the same call in its own session and, under READ COMMITTED,
cannot see the winner's uncommitted row.

Without a unique index the loser sails through and commits FIRST — the winner has
not committed yet — so two rows exist for one key and the test fails with the
count. With the index the loser's INSERT parks on it: Postgres makes a writer
that conflicts with an uncommitted tuple wait for that transaction's outcome. The
winner is released only after the loser is known to be parked, at which point
`ON CONFLICT DO NOTHING` skips, the loser re-selects — a fresh statement snapshot
under READ COMMITTED sees the committed winner — and returns the winner's row as
a replay. Same shape as `test_flow_revision_race`, and the same shape the fix
copies from `flow.current_flow_revision`.

Measured on `bb7ea93` (the RED run of this file), all three:

    exports         : 2 rows for one manifest_hash
    import batches  : 2 rows for one (review_id, raw_payload_digest)
    dedup runs      : 2 rows for one (review_id, input_digest)

## The flow revision is opened before every race

`create_export` calls `_compute_flow`, which calls `current_flow_revision`, which
ALREADY has this fix. If the revision row does not exist yet, the loser parks on
`uq_flow_revision_number` instead of on the export — it would then be released
after the winner's commit, see the winner's export, replay it, and this file would
report one row while proving nothing about `inclusion_exports` at all. So the
fixture opens the revision first with a plain `GET /flow`, and
`test_the_export_race_parks_on_the_export_and_not_on_the_revision` pins that.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

TENANT = tenant("tenant-twin-writer")
LEAD = "twin-writer-lead"
REVIEWER = "twin-writer-reviewer"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _lead_actor():  # noqa: ANN202
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)


def _count(sql: str, params: dict[str, Any]) -> int:
    """A row count taken outside both racing sessions, with the tenant GUC bound.

    Its own engine and its own transaction: counting from either racer's session
    would read that racer's uncommitted work and answer 1 for the wrong reason.
    """
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def _race(call: Callable[[Session], Any]) -> dict[str, Any]:
    """Run `call` in two sessions, the loser parked mid-write, and report both.

    The winner's transaction stays open across the loser's whole attempt, which
    is what makes the loser's SELECT blind to it. Nothing here samples: the only
    thing that decides whether the loser blocks is whether the database has an
    index to block it on.
    """
    from actor import bind_actor

    actor = _lead_actor()
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    results: dict[str, Any] = {}
    loser_started = threading.Event()

    winner_session = Session(engine)
    winner_session.begin()
    bind_actor(winner_session, actor)
    results["winner"] = call(winner_session)

    def loser() -> None:
        session = Session(engine)
        try:
            session.begin()
            bind_actor(session, actor)
            loser_started.set()
            results["loser"] = call(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the failure IS the finding
            results["error"] = f"{type(exc).__name__}: {exc}"
            session.rollback()
        finally:
            session.close()

    thread = threading.Thread(target=loser, daemon=True)
    thread.start()
    loser_started.wait(timeout=10)
    # One second for the loser to run its check-then-insert. WITHOUT an index it
    # finishes and commits inside this window, before the winner has committed
    # anything — which is the defect. WITH one it is parked on the index and this
    # join times out, which is the fix.
    results["loser_finished_before_the_winner_committed"] = thread.join(timeout=1) or not thread.is_alive()
    winner_session.commit()
    winner_session.close()
    thread.join(timeout=60)
    engine.dispose()
    assert not thread.is_alive(), "the loser never returned; it is still parked after the winner committed"
    return results


def _seed_review(client, slug: str) -> str:  # noqa: ANN001
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "twin-writer", "external_key": f"{slug}-{uuid.uuid4()}", "title": "Twin writer"},
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
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )
    assert activated.status_code in (200, 201), activated.text
    return review_id


# ---------------------------------------------------------------- exports --


@pytest.fixture
def exportable(client):  # noqa: ANN001, ANN201
    """A review screened to completion, with its flow revision already open."""
    review_id = _seed_review(client, "export-race")
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "round_number": 1,
            "review_record_id": record_id,
            "disposition": "include",
            "rationale": "meets the criteria",
        },
        headers=headers(REVIEWER, "reviewer"),
    )
    assert decided.status_code == 201, decided.text
    # Opens `flow_revisions` before the race — see the module docstring.
    assert client.get(f"/api/reviews/{review_id}/flow", headers=headers()).status_code == 200
    return review_id


def test_two_concurrent_seals_of_one_manifest_leave_one_export(client, exportable) -> None:
    """THE DEFECT. Two permanent, indistinguishable, undeletable seals.

    `inclusion_exports` carries an append-only trigger, so the loser's row cannot
    be removed afterwards by anybody, at any privilege this service holds.
    """
    from application.services.flow import create_export

    review_id = uuid.UUID(exportable)
    outcome = _race(lambda session: create_export(session, _lead_actor(), review_id))

    assert "error" not in outcome, f"the second sealer was refused: {outcome.get('error')}"
    manifests = {outcome["winner"]["manifest_hash"], outcome["loser"]["manifest_hash"]}
    assert len(manifests) == 1, f"the two racers sealed different manifests, so this proves nothing: {manifests}"

    rows = _count(
        "SELECT count(*) FROM inclusion_exports WHERE review_id = :r AND manifest_hash = :m",
        {"r": str(review_id), "m": outcome["winner"]["manifest_hash"]},
    )
    assert rows == 1, (
        f"{rows} permanent inclusion sets were sealed for ONE manifest_hash. They are byte-identical, "
        "they both verify, and the table's append-only trigger refuses DELETE, so a consumer citing "
        "'the' inclusion set of this review has to guess and nobody can clean it up"
    )
    assert outcome["loser"]["export_id"] == outcome["winner"]["export_id"], (
        "the loser was handed a different export than the one that exists: " + json.dumps(outcome["loser"])
    )
    assert outcome["loser"]["replayed"] is True, (
        "the loser minted rather than replayed, so it cannot tell 'I sealed this' from 'this was "
        "already sealed': " + json.dumps(outcome["loser"])
    )


def test_the_export_race_parks_on_the_export_and_not_on_the_revision(client, exportable) -> None:
    """The race above must be about `inclusion_exports`, not about `flow_revisions`.

    `current_flow_revision` already holds this fix. If its row were still absent
    the loser would park THERE, be released by the winner's commit, then see the
    winner's export and replay it — one row, and a green test measuring the wrong
    table. This asserts the revision exists before any racing begins.
    """
    revisions = _count("SELECT count(*) FROM flow_revisions WHERE review_id = :r", {"r": exportable})
    assert revisions >= 1, (
        "the flow revision is not open before the race, so the export race parks on "
        "uq_flow_revision_number and proves nothing about inclusion_exports"
    )


# ---------------------------------------------------------------- imports --


def test_two_concurrent_imports_of_one_payload_leave_one_batch(client) -> None:
    """THE WORST OF THE THREE. The corpus doubles and every count agrees with it.

    `identified_total` is derived from `import_rows`, so a doubled import is not
    detectable by comparing the ledger against itself: it is internally
    consistent and simply wrong. `import_batches` is append-only too.
    """
    from application.services.imports import import_payload

    review_id = uuid.UUID(_seed_review(client, "import-race"))
    payload = json.dumps(
        {"records": [{"title": f"Study {i}", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"} for i in range(4)]}
    ).encode()

    outcome = _race(
        lambda session: import_payload(session, _lead_actor(), review_id, source_kind="canonical_json", payload=payload)
    )

    assert "error" not in outcome, f"the second importer was refused: {outcome.get('error')}"
    digest = outcome["winner"]["raw_payload_digest"]
    assert outcome["loser"]["raw_payload_digest"] == digest, outcome

    batches = _count(
        "SELECT count(*) FROM import_batches WHERE review_id = :r AND raw_payload_digest = :d",
        {"r": str(review_id), "d": digest},
    )
    assert batches == 1, (
        f"identical bytes into one review wrote {batches} batches. Every record in the payload is now "
        "in the review twice, `identified_total` counts them all, and the next import of the same "
        "bytes hits scalar_one_or_none() on two rows -> MultipleResultsFound -> a bare 500 text/plain "
        "outside the problem+json contract"
    )
    rows = _count("SELECT count(*) FROM import_rows WHERE review_id = :r", {"r": str(review_id)})
    assert rows == 4, f"the corpus holds {rows} rows for a 4-record payload"
    assert outcome["loser"]["replayed"] is True, json.dumps(outcome["loser"])
    assert outcome["loser"]["import_batch_id"] == outcome["winner"]["import_batch_id"], outcome


def test_a_later_import_of_the_same_bytes_stays_inside_the_problem_contract(client) -> None:
    """The 500 the doubled batch causes, asserted at the edge.

    A unique index prevents the second batch; this pins the CONSEQUENCE the brief
    names, so that a future change which reintroduces duplicate batches is caught
    by a contract assertion and not only by a row count.
    """
    review_id = _seed_review(client, "import-replay")
    body = json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]})
    first = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=body,
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert first.status_code == 201, first.text
    again = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=body,
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["replayed"] is True, again.text
    assert again.json()["import_batch_id"] == first.json()["import_batch_id"], again.text


# ------------------------------------------------------------ dedup runs --


def test_two_concurrent_dedup_runs_over_one_corpus_leave_one_run(client) -> None:
    """Two cluster sets for one `(corpus, engine, config)`, and both resolvable.

    `resolve_cluster` can be pointed at either set, and the resolutions of one are
    invisible to the other — so "which cluster set is current?" has two answers
    and a merge taken against the losing set does not remove the record.
    """
    from application.services.deduplication import create_run

    review_id = uuid.UUID(_seed_review(client, "dedup-race"))
    doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:10]}"
    client.post(
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
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text

    outcome = _race(lambda session: create_run(session, _lead_actor(), review_id))

    assert "error" not in outcome, f"the second run was refused: {outcome.get('error')}"
    digest = outcome["winner"]["input_digest"]
    assert outcome["loser"]["input_digest"] == digest, outcome

    runs = _count(
        "SELECT count(*) FROM deduplication_runs WHERE review_id = :r AND input_digest = :d",
        {"r": str(review_id), "d": digest},
    )
    assert runs == 1, (
        f"{runs} deduplication runs exist for one corpus, one engine and one config. They propose "
        "different cluster ids over the same records, `resolve_cluster` accepts either, and a merge "
        "against the losing set removes nothing"
    )
    clusters = _count("SELECT count(*) FROM duplicate_clusters WHERE review_id = :r", {"r": str(review_id)})
    assert clusters == 1, f"{clusters} clusters were proposed over one duplicate pair"
    assert outcome["loser"]["replayed"] is True, json.dumps(outcome["loser"])
    assert outcome["loser"]["dedup_run_id"] == outcome["winner"]["dedup_run_id"], outcome


def test_the_loser_parks_rather_than_racing_to_a_result(client, exportable) -> None:
    """The mechanism, asserted directly: the second writer WAITS.

    Every count above would also be satisfied by a fix that simply refused the
    second writer, or by one that got lucky with timing. This pins the actual
    shape — the loser is still running one second after it started, because it is
    blocked on a unique index waiting for the winner's outcome — so a "fix" that
    only re-orders the SELECT, or that passes because the scheduler happened to
    serialise the two calls, cannot be mistaken for this one.
    """
    from application.services.flow import create_export

    review_id = uuid.UUID(exportable)
    outcome = _race(lambda session: create_export(session, _lead_actor(), review_id))
    assert outcome["loser_finished_before_the_winner_committed"] is False, (
        "the second writer ran its whole check-then-insert and committed while the winner's "
        "transaction was still open: nothing in the database made it wait, so the two SELECTs were "
        "both blind and both INSERTs stood"
    )
