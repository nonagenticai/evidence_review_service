"""`FND-003` — the event and the state it describes are one commit, or neither.

`13-…:§5.3` gives one application command one transaction, and step 4 of that
transaction is "create its outbox event". That is the whole guarantee and it is
a NEGATIVE one: after this, there is no ordering of failures that leaves a
committed decision nobody was told about, or an announcement of something that
never happened.

## Why this drives the services rather than the API

The claim is about a transaction boundary, and `actor_scope` is where that
boundary is. Driving HTTP would test the same code through a layer that commits
on the caller's behalf, which is exactly the layer the rollback arm has to be
able to take away.

## The rollback arm is what makes the rest non-vacuous

Every assertion that a row appeared would also pass if the outbox were written
in its own transaction, or after the commit, or by a background task. So one
test raises inside the scope and asserts that BOTH the review and its event are
gone. Without it this file proves only that a row gets written eventually.

Skipped rather than faked without postgres: `ck_domain_outbox_registered_event`,
`ck_domain_outbox_payload_is_minimal` and the append-only trigger are all server
side, and an in-memory double would run this green while enforcing none of them.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import text  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: Its own tenant, for the reason every other module in this directory states:
#: nothing truncates between modules, so a shared label would make one suite's
#: counts depend on whether another ran.
TENANT = tenant("tenant-domain-outbox")
LEAD = "lead-outbox"


def _actor(role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=uuid.UUID(TENANT), subject_id=LEAD, role=ActorRole(role))


def _events(session: Any, aggregate_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT event_type, schema_ref, producer, resource_scope, tenant_id, aggregate_type, "
            "       aggregate_id, aggregate_version, payload, content_hash "
            "  FROM domain_outbox WHERE aggregate_id = :aid ORDER BY aggregate_version, occurred_at"
        ),
        {"aid": aggregate_id},
    ).mappings()
    return [dict(row) for row in rows]


def _review_events(session: Any, review_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every event about a review, including the ones whose aggregate is a child.

    Keyed on the payload's `review_id` rather than on `aggregate_id`, because an
    import batch and a deduplication run are their own aggregates.
    """
    rows = session.execute(
        text(
            "SELECT event_type, aggregate_type, aggregate_version, payload FROM domain_outbox "
            " WHERE payload->>'review_id' = :rid ORDER BY occurred_at"
        ),
        {"rid": str(review_id)},
    ).mappings()
    return [dict(row) for row in rows]


def test_a_created_review_commits_its_event_with_itself() -> None:
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="outbox",
            external_key=f"created-{uuid.uuid4()}",
            title="An event commits with its cause",
        )
        review_id = review.review_id

    with actor_scope(_actor()) as session:
        rows = _events(session, review_id)

    assert [row["event_type"] for row in rows] == ["review.created.v1"]
    row = rows[0]
    assert row["schema_ref"] == "urn:datacompany:research-event-envelope:1"
    assert row["producer"] == "evidence-review"
    assert row["resource_scope"] == "tenant"
    assert str(row["tenant_id"]) == TENANT
    assert row["aggregate_type"] == "review"
    assert row["aggregate_version"] == 1
    assert row["payload"]["review_id"] == str(review_id)


def test_a_failed_command_leaves_neither_the_state_nor_the_event() -> None:
    """The arm the whole file rests on.

    `create_review` succeeds, the event is added, and then the scope raises
    before it can commit. If the outbox row survives, the event was not part of
    this transaction — and every other assertion here would still have passed.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    external_key = f"rolled-back-{uuid.uuid4()}"
    captured: dict[str, uuid.UUID] = {}

    class DeliberateError(RuntimeError):
        pass

    with pytest.raises(DeliberateError):
        with actor_scope(_actor()) as session:
            review = review_svc.create_review(
                session,
                _actor(),
                project_id="outbox",
                external_key=external_key,
                title="Never committed",
            )
            captured["review_id"] = review.review_id
            raise DeliberateError("the command fails after the event was written")

    with actor_scope(_actor()) as session:
        surviving_reviews = session.execute(
            text("SELECT count(*) FROM reviews WHERE external_key = :key"),
            {"key": external_key},
        ).scalar_one()
        surviving_events = session.execute(
            text("SELECT count(*) FROM domain_outbox WHERE aggregate_id = :aid"),
            {"aid": captured["review_id"]},
        ).scalar_one()

    assert surviving_reviews == 0, "the review survived a rollback"
    assert surviving_events == 0, (
        "the outbox row survived a transaction the review did not. The event is not being written "
        "in the same transaction as its cause, which is the entire point of the outbox."
    )


def test_the_lifecycle_edges_each_name_themselves() -> None:
    """Six events from one emit point, keyed by the edge rather than the target.

    Arriving at `active` is an activation, a resume or a reopen depending on the
    state left behind; a consumer told only "now active" cannot tell a resumed
    review from a reopened one, and a reopen is the edge `13-…` requires an
    approval attestation for.

    `draft -> configured` deliberately raises nothing — it is not one of the
    twenty-one, and the database's closed CHECK would refuse an invented name.
    """
    from application.services import reviews as review_svc
    from database import actor_scope

    with actor_scope(_actor()) as session:
        review = review_svc.create_review(
            session,
            _actor(),
            project_id="outbox",
            external_key=f"lifecycle-{uuid.uuid4()}",
            title="Every edge names itself",
        )
        review_id = review.review_id
        protocol = review_svc.create_protocol_version(
            session,
            _actor(),
            review,
            inclusion_criteria={"population": "adults"},
            exclusion_criteria={"design": "not an RCT"},
            stage_definitions=[{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 1}],
            exclusion_reason_vocabulary=["wrong_design"],
            reviewers_required=1,
        )
        review_svc.lock_protocol_version(session, _actor(), protocol)
        review_svc.activate(
            session,
            _actor(),
            review,
            protocol_version_id=protocol.protocol_version_id,
            rationale="ready",
        )
        review_svc.transition(session, _actor(), review, to_state="paused", rationale="waiting")
        review_svc.transition(session, _actor(), review, to_state="active", rationale="resuming")
        review_svc.transition(session, _actor(), review, to_state="closed", rationale="done")

    with actor_scope(_actor()) as session:
        rows = _events(session, review_id)

    assert [row["event_type"] for row in rows] == [
        "review.created.v1",
        "review.activated.v1",
        "review.paused.v1",
        "review.resumed.v1",
        "review.closed.v1",
    ], rows

    # The versions are the review's own revision counter, not a per-event
    # sequence: that is what lets a consumer defer a future version when an
    # earlier one is missing (`13-…:§8.2`).
    versions = [row["aggregate_version"] for row in rows]
    assert versions == sorted(versions), versions
    assert len(set(versions)) == len(versions), f"two events claim the same review revision: {versions}"

    activated = next(row for row in rows if row["event_type"] == "review.activated.v1")
    assert activated["payload"]["to_state"] == "active"
    assert activated["payload"]["from_state"] in ("draft", "configured")

    resumed = next(row for row in rows if row["event_type"] == "review.resumed.v1")
    assert resumed["payload"]["from_state"] == "paused"

    assert not [row for row in rows if row["payload"].get("to_state") == "configured"], (
        "draft -> configured raised an event; it is not one of the twenty-one"
    )
