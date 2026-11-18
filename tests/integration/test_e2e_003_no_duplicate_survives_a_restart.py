"""E2E-003 — the determinate core: retry, restart and restore create no duplicate.

`15-…:649` — "`E2E-003` | API/worker/n8n crash and duplicate callback during
review | one job/decision/export and recovered workflow".
`implementation-plans/90-…:309` — "terminate API/worker/n8n around
submit/commit/callback; duplicate callback; assert one job/decision/export and
recovered step".
`15-…:182`, the W2 exit gate — "retry, restart and restore create no duplicate
review decision or export".

## ⛔ What this file deliberately does NOT do

The acceptance suite as written kills three processes: the API, the worker and
n8n. None of those three is runnable here. There is no worker loop in this
service at all (`application/services/jobs.py` says so at length — `claim_next`
has no caller on a loop), the dedicated `datacompany-n8n` instance is deliberately
unmerged (`consumer_registry.NOT_REGISTERED["n8n"]`), and the FR callback
endpoint that would receive a duplicate delivery lives in `fr`, not here. A test
that `SIGKILL`ed a subprocess it had started itself would be measuring the
subprocess.

What IS provable against a live Postgres alone is every CARDINALITY the
acceptance criterion actually asserts — one job, one decision, one export — plus
the two transactional properties that make those cardinalities survive a crash
at the worst possible instant. A process death is only ever observable through
the rows it did or did not leave behind, so the rows are what is asserted:

1. the same submission twice yields ONE `DurableJob` (and a different key yields
   two, or the first assertion passes for the wrong reason);
2. the same decision delivered twice yields ONE decision and ONE domain event;
3. the same export requested twice yields ONE `inclusion_exports` row;
4. a death AFTER commit and BEFORE the relay loses nothing and duplicates
   nothing — the relay is run, then run again, and the durable log still holds
   exactly one row and exactly one ORIGINAL delivery per registered consumer;
5. a death BEFORE commit leaves no job, no decision and no outbox row — which is
   the whole reason the outbox is written in the caller's transaction rather
   than beside it.

## Why the fixtures come from `test_e2e_acceptance`

They are imported, not re-implemented. `E2E-002` already builds the only state
these assertions can be made against — a locked, activated, imported, deduped,
fully screened and adjudicated review with a sealed export — and a second
hand-rolled copy of that ladder would be a second thing to keep in step with the
product. They are module-scoped, so this module gets its own review and its own
export; nothing here mutates `E2E-002`'s state and nothing there mutates this
one's.

Only the FIXTURES and the header helpers are imported. Importing the test
functions too would re-collect all forty of `E2E-002`'s tests under this file's
node ids, and the isolation gate counts by node id — this file's floor would
then be satisfied by copies of another suite's tests.

## The relay is the real one

`relay.relay_pending` and `relay.replay` are called directly against the live
database. A mock relay would prove that a mock is idempotent.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tests.integration import test_e2e_acceptance as _e2e002  # noqa: E402
from tests.integration.test_e2e_acceptance import LEAD, REVIEWER_B, TENANT, headers, lead_headers  # noqa: E402

# The fixtures, REBOUND rather than imported by name. See the module docstring
# for why the test functions are not brought over at all.
#
# `from … import review` would work identically at runtime and ruff refuses it:
# every test below takes `review` as a parameter, and a parameter shadowing an
# imported name is `F811`, eight times over. Blanket-noqa'ing a redefinition
# warning across a file is exactly how a genuine one gets hidden later, so the
# names are bound by assignment instead — `_e2e002` is then a real use of the
# import and the parameters shadow an ordinary module-level binding, which is
# what a pytest fixture reference has always been.
adjudicated = _e2e002.adjudicated
app_client = _e2e002.app_client
dedup = _e2e002.dedup
export = _e2e002.export
review = _e2e002.review
screened = _e2e002.screened

DB_URL = os.getenv("DATABASE_URL", "")

# ⚠️ The prefix is load-bearing. `tests/conftest.py` keys `DB_ABSENT_SKIP_PREFIX`
# on exactly this opening, and a skip reason that does not match makes the
# isolation gate misreport a database-absent run as a narrowed one.
pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT_UUID = uuid.UUID(TENANT)

#: `relay_pending` moves at most `DEFAULT_BATCH` rows a pass and reads the outbox
#: with no tenant predicate, so a run on a database where the runtime role was
#: not demoted drains every tenant. Bounded so a relay that is not making
#: progress fails loudly instead of spinning.
_MAX_PASSES = 50


def _actor(subject: str = LEAD, role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=subject, role=ActorRole(role))


def _scalar(sql: str, **params: Any) -> Any:
    """One value, read through a properly bound actor scope.

    `actor_scope` binds all three GUCs the policies read; a raw engine
    connection binding only `app.current_tenant` matches zero rows on the
    blinded tables and would make every count below silently vacuous.
    """
    from database import actor_scope

    with actor_scope(_actor()) as session:
        return session.execute(text(sql), params).scalar()


# ---------------------------------------------------------------------------
# 1. Duplicate submit — one job
# ---------------------------------------------------------------------------


def test_the_same_submission_twice_enqueues_one_job(review):
    """`jobs.enqueue` is idempotent on `(tenant, review, job_type, key)`.

    Two SEPARATE committed transactions, not two calls on one session: the
    failure this describes is a client that submitted, lost the response to an
    API restart, and submitted again. One session would let SQLAlchemy's
    identity map answer the second call without the database ever being asked.
    """
    from application.services import jobs
    from database import actor_scope

    key = f"e2e003-{uuid.uuid4()}"
    command_hash = "sha256:" + "a" * 64

    with actor_scope(_actor()) as session:
        first = jobs.enqueue(
            session,
            _actor(),
            review_id=uuid.UUID(review["review_id"]),
            job_type="export",
            command_hash=command_hash,
            idempotency_key=key,
        )
        first_id = first.job_id

    with actor_scope(_actor()) as session:
        second = jobs.enqueue(
            session,
            _actor(),
            review_id=uuid.UUID(review["review_id"]),
            job_type="export",
            command_hash=command_hash,
            idempotency_key=key,
        )
        second_id = second.job_id

    assert second_id == first_id, "a resubmission with the same key must return the existing job"
    rows = _scalar("SELECT count(*) FROM durable_jobs WHERE idempotency_key = :k", k=key)
    assert rows == 1, f"the same idempotency key left {rows} durable_jobs rows"


def test_a_different_idempotency_key_enqueues_a_second_job(review):
    """The non-vacuity control for the test above.

    Without it, an `enqueue` that refused to insert anything at all — or one
    whose lookup matched on tenant and review while ignoring the key entirely —
    would satisfy "exactly one row" perfectly. Two keys must produce two jobs.
    """
    from application.services import jobs
    from database import actor_scope

    command_hash = "sha256:" + "b" * 64
    keys = [f"e2e003-distinct-{uuid.uuid4()}" for _ in range(2)]
    job_ids = []

    for key in keys:
        with actor_scope(_actor()) as session:
            job = jobs.enqueue(
                session,
                _actor(),
                review_id=uuid.UUID(review["review_id"]),
                job_type="export",
                command_hash=command_hash,
                idempotency_key=key,
            )
            job_ids.append(job.job_id)

    assert job_ids[0] != job_ids[1], "two different idempotency keys must be two different jobs"
    rows = _scalar("SELECT count(*) FROM durable_jobs WHERE idempotency_key = ANY(:k)", k=keys)
    assert rows == 2, f"two distinct keys left {rows} durable_jobs rows"


# ---------------------------------------------------------------------------
# 2. Duplicate callback — one decision, one event
# ---------------------------------------------------------------------------


def test_the_same_decision_delivered_twice_records_one_decision_and_one_event(app_client, review, screened):
    """A duplicate delivery of a decision the service already holds.

    This is what "duplicate callback" means on THIS side of the wire. The
    callback endpoint named in the acceptance criterion lives in `fr`; what
    reaches this service is `POST /decisions`, and a retried n8n step or a
    replayed FR callback delivers the identical body a second time.

    The refusal is a `409` rather than a silent `200`, and that is the right
    answer rather than merely an answer: a reviewer whose correction was
    swallowed as "already recorded" would have no way to tell a duplicate from
    a lost write. What the criterion asserts is the CARDINALITY, and the
    cardinality is checked in the database — one row, one event — not inferred
    from the status code.
    """
    review_id, stage_id = review["review_id"], review["stage_id"]
    record_id = screened["records"][0]

    before = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_B)
    ).json()["items"]
    assert len(before) == 1, f"the fixture must have left exactly one decision here: {before}"
    decision_id = before[0]["decision_id"]

    events_before = _scalar(
        "SELECT count(*) FROM domain_outbox WHERE event_type = 'review.decision.recorded.v1' "
        "AND payload->>'review_id' = :r",
        r=review_id,
    )
    assert events_before > 0, (
        "no decision event is visible for this review, so the count below would be measuring "
        "row-level security rather than the outbox"
    )

    duplicate = app_client.post(
        f"/api/reviews/{review_id}/decisions",
        json={"stage_id": stage_id, "review_record_id": record_id, "disposition": "include"},
        headers=headers(REVIEWER_B),
    )
    assert duplicate.status_code == 409, duplicate.text

    after = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_B)
    ).json()["items"]
    assert [i["decision_id"] for i in after] == [decision_id], (
        f"the duplicate delivery changed the reviewer's decision set: {after}"
    )

    rows = _scalar(
        "SELECT count(*) FROM screening_decisions WHERE review_id = :r AND review_record_id = :rec "
        "AND reviewer_subject_id = :s",
        r=review_id,
        rec=record_id,
        s=REVIEWER_B,
    )
    assert rows == 1, f"the duplicate delivery left {rows} screening_decisions rows"

    events_after = _scalar(
        "SELECT count(*) FROM domain_outbox WHERE event_type = 'review.decision.recorded.v1' "
        "AND payload->>'review_id' = :r",
        r=review_id,
    )
    assert events_after == events_before, (
        "the refused duplicate still wrote a decision event; a consumer would see two recordings of one decision"
    )


# ---------------------------------------------------------------------------
# 3. Duplicate export — one export
# ---------------------------------------------------------------------------


def test_the_same_export_requested_twice_seals_one_export(app_client, review, export):
    """`POST /{review_id}/exports` twice on an unchanged review.

    `inclusion_exports` is append-only, so a duplicate is permanent: a consumer
    citing "the inclusion set" of this review would be handed two
    indistinguishable seals and have to guess. The second request is replayed
    onto the existing row rather than refused — the same shape
    `seal_corpus_version` and `create_run` use.
    """
    review_id = review["review_id"]

    again = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert again.status_code == 201, again.text
    body = again.json()
    assert body["replayed"] is True, "a second seal of an unchanged review must be reported as a replay"
    assert body["export_id"] == export["export_id"]
    assert body["manifest_hash"] == export["manifest_hash"]
    assert body["payload_digest"] == export["payload_digest"]

    listed = app_client.get(f"/api/reviews/{review_id}/exports", headers=lead_headers()).json()
    assert listed["count"] == 1, f"the review holds {listed['count']} exports after two identical requests"

    rows = _scalar("SELECT count(*) FROM inclusion_exports WHERE review_id = :r", r=review_id)
    assert rows == 1, f"the table holds {rows} inclusion_exports rows for this review"


# ---------------------------------------------------------------------------
# 4. Crash between commit and effect
# ---------------------------------------------------------------------------


def _relay_until_drained() -> tuple[int, int]:
    from application.services import relay
    from database import actor_scope

    relayed_total = 0
    deliveries_total = 0
    for _ in range(_MAX_PASSES):
        with actor_scope(_actor()) as session:
            relayed, deliveries = relay.relay_pending(session)
        relayed_total += relayed
        deliveries_total += deliveries
        if relayed == 0:
            return relayed_total, deliveries_total
    raise AssertionError(
        f"the relay made {_MAX_PASSES} passes without draining; it is not making progress, and no "
        "conclusion about duplication can be drawn from a relay that has not finished"
    )


def test_a_death_after_commit_and_before_the_relay_delivers_exactly_once():
    """The outbox's reason for existing, exercised as a crash.

    The process is killed at the one instant that matters: the domain state and
    its event are COMMITTED, and the relay has not run. Nothing here fakes that
    instant — the review is created through the real service inside the real
    `actor_scope`, and the relay is simply not called yet. The assertion before
    the first pass is what proves the window is genuinely open; without it a
    relay that had already run would make every assertion below trivially true.

    Then the relay runs, and runs AGAIN. `relay_pending` is at-least-once by
    construction — it can crash after inserting into the log and before
    recording that it did — so `ON CONFLICT (event_id) DO NOTHING` plus a
    derived `NOT EXISTS` cursor is the whole of the tolerance, and a second pass
    is the only thing that can measure it.

    ## `replay` is NOT asserted to be idempotent, because it is not

    `relay.replay` creates NEW delivery rows on purpose and its own docstring
    refuses to pretend otherwise: exactly-once lives in the consumer's inbox
    index, not in the relay. So what is asserted is the property that actually
    holds and that a duplicate would break — the ORIGINAL delivery
    (`replay_of_delivery_id IS NULL`) stays at exactly one per consumer however
    many times replay runs, and each replayed row names that original. An
    assertion that replay created nothing would be false, and asserting a false
    thing weakly enough to pass is how a suite stops being evidence.
    """
    from application.services import relay
    from application.services import reviews as review_svc
    from database import actor_scope
    from domain.policies import consumer_registry

    # Where the log stands BEFORE anything of ours reaches it, so `replay` below
    # can be aimed at our own events and cannot fan out the whole log.
    floor_position = _scalar("SELECT coalesce(max(log_position), 0) FROM research_domain_events") or 0

    # THE COMMIT. One transaction, the review and its event together.
    with actor_scope(_actor()) as session:
        created = review_svc.create_review(
            session,
            _actor(),
            project_id="e2e003",
            external_key=f"crash-{uuid.uuid4()}",
            title="Death between commit and relay",
        )
        review_id = created.review_id

    event_id = _scalar(
        "SELECT event_id FROM domain_outbox WHERE aggregate_id = :a AND event_type = 'review.created.v1'",
        a=review_id,
    )
    assert event_id is not None, "the event did not commit with the state it describes"

    # ⛔ THE PROCESS DIED HERE. The row is durable; nothing has relayed it.
    assert _scalar("SELECT count(*) FROM research_domain_events WHERE event_id = :e", e=event_id) == 0, (
        "the event was already in the log, so this test is not measuring the crash window it names"
    )

    consumers = consumer_registry.consumers_for("review.created.v1")
    assert consumers, "no consumer is registered for review.created.v1; the fan-out assertions below are vacuous"

    # RESTART: the relay runs for the first time.
    _relay_until_drained()
    assert _scalar("SELECT count(*) FROM research_domain_events WHERE event_id = :e", e=event_id) == 1
    originals = _scalar(
        "SELECT count(*) FROM event_deliveries WHERE event_id = :e AND replay_of_delivery_id IS NULL", e=event_id
    )
    assert originals == len(consumers), f"expected one delivery per registered consumer, got {originals}"

    # AND THE RELAY DIED AGAIN, after inserting and before recording that it had.
    _relay_until_drained()
    assert _scalar("SELECT count(*) FROM research_domain_events WHERE event_id = :e", e=event_id) == 1, (
        "a second relay pass wrote the event into the durable log twice"
    )
    assert (
        _scalar(
            "SELECT count(*) FROM event_deliveries WHERE event_id = :e AND replay_of_delivery_id IS NULL",
            e=event_id,
        )
        == originals
    ), "a second relay pass created a second original delivery"

    # RESTORE: an operator replays from before our own events. See the docstring
    # for why this asserts additivity rather than idempotency.
    consumer = consumers[0]
    original_id = _scalar(
        "SELECT delivery_id FROM event_deliveries WHERE event_id = :e AND consumer = :c "
        "AND replay_of_delivery_id IS NULL",
        e=event_id,
        c=consumer,
    )
    with actor_scope(_actor()) as session:
        replayed = relay.replay(session, consumer=consumer, from_position=floor_position)
    assert replayed >= 1, "the replay recreated nothing, so it cannot show what a replay does to the originals"

    assert _scalar("SELECT count(*) FROM research_domain_events WHERE event_id = :e", e=event_id) == 1, (
        "a replay changed the durable log; §8.1 requires it to leave event_id untouched"
    )
    assert (
        _scalar(
            "SELECT count(*) FROM event_deliveries WHERE event_id = :e AND replay_of_delivery_id IS NULL",
            e=event_id,
        )
        == originals
    ), "a replay minted a second ORIGINAL delivery instead of a row pointing at the first"
    assert (
        _scalar(
            "SELECT count(*) FROM event_deliveries WHERE event_id = :e AND consumer = :c "
            "AND replay_of_delivery_id = :o",
            e=event_id,
            c=consumer,
            o=original_id,
        )
        >= 1
    ), "the replayed delivery does not name the original it replays"


# ---------------------------------------------------------------------------
# 5. Interrupted mid-transaction
# ---------------------------------------------------------------------------


def test_a_death_before_commit_leaves_no_job_no_decision_and_no_outbox_row(app_client, review, screened):
    """The other side of the same window, and the outbox's whole point.

    A job, a screening decision and the decision's event are written on one
    session — the arrangement `13-…:§5.3` requires, one application command to
    one transaction — and the process dies before the commit. If the event were
    written anywhere but the caller's transaction, a consumer would now be
    holding a recording of a decision this service does not have.

    The decision is a CORRECTION (`supersedes_decision_id`), because every
    record in the fixture is already decided and a bare second decision would be
    refused by the very guard the previous test exercises. That makes this a
    strictly harder case: the correction is a legal write that really would have
    landed.
    """
    from actor import bind_actor
    from application.services import jobs, screening
    from database import get_session_factory

    review_id, stage_id = review["review_id"], review["stage_id"]
    record_id = screened["records"][0]

    current = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_B)
    ).json()["items"]
    assert len(current) == 1, current
    superseded_id = current[0]["decision_id"]

    key = f"e2e003-rollback-{uuid.uuid4()}"
    reviewer = _actor(REVIEWER_B, "reviewer")

    # ⛔ NOT `actor_scope`: that commits on a clean exit, which is the one thing
    # this test must not do. The session is driven by hand so the rollback is
    # the process death rather than an error path.
    session = get_session_factory()()
    try:
        bind_actor(session, reviewer)
        job = jobs.enqueue(
            session,
            reviewer,
            review_id=uuid.UUID(review_id),
            job_type="export",
            command_hash="sha256:" + "c" * 64,
            idempotency_key=key,
        )
        job_id = job.job_id
        recorded = screening.record_decision(
            session,
            reviewer,
            uuid.UUID(review_id),
            stage_id=uuid.UUID(stage_id),
            round_number=1,
            review_record_id=uuid.UUID(record_id),
            disposition="uncertain",
            rationale="thinking again",
            supersedes_decision_id=uuid.UUID(superseded_id),
        )
        decision_id = recorded["decision_id"]
        session.flush()
        # Everything is in the transaction and visible to it — without this the
        # rollback below would be proving that nothing was ever written.
        assert session.execute(text("SELECT count(*) FROM durable_jobs WHERE job_id = :j"), {"j": job_id}).scalar() == 1
        assert (
            session.execute(
                text("SELECT count(*) FROM domain_outbox WHERE aggregate_id = :a"), {"a": decision_id}
            ).scalar()
            == 1
        )

        # ⛔ THE PROCESS DIED HERE, before commit.
        session.rollback()
    finally:
        session.close()

    assert _scalar("SELECT count(*) FROM durable_jobs WHERE idempotency_key = :k", k=key) == 0, (
        "the job survived a transaction that never committed"
    )
    assert _scalar("SELECT count(*) FROM screening_decisions WHERE decision_id = :d", d=decision_id) == 0, (
        "the decision survived a transaction that never committed"
    )
    assert _scalar("SELECT count(*) FROM domain_outbox WHERE aggregate_id = :a", a=decision_id) == 0, (
        "the event outlived the state it describes — a consumer would be told about a decision "
        "this service does not hold"
    )

    unchanged = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_B)
    ).json()["items"]
    assert [i["decision_id"] for i in unchanged] == [superseded_id], (
        f"the rolled-back correction moved the reviewer's current decision: {unchanged}"
    )
