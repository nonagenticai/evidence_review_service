"""`ORDER BY decided_at` is a partial order over `screening_decisions`.

`blinded_reads.unblinded_current_decisions` is the single read the flow ledger,
the export gate and conflict detection all derive from, and every one of their
docstrings says the rows arrive ordered by `decided_at`. They only usually did.
`decided_at` is `server_default=now()` — TRANSACTION start, not statement time,
at microsecond resolution — so decisions written in one transaction carry the
identical value and nothing in the query decides between them.

Measured on 5481931, eight decisions written in one transaction:

    AssertionError: eight decisions with an identical `decided_at` came back in
    an order nothing in the query determines ... [UUID('e75ab75e-…'),
    UUID('1a320260-…'), UUID('5805b29f-…'), UUID('e30dd6ca-…'),
    UUID('71976a9c-…'), UUID('0ea54fc8-…'), UUID('1ef8e3ec-…'),
    UUID('2af732a3-…')]
    assert [UUID('e75ab7...ba741e'), ...] == [UUID('0ea54f...1d084d'), ...]
    At index 0 diff: UUID('e75ab75e-…') != UUID('0ea54fc8-…')

## The tie is constructed from the column's own default, not poked in

`screening_decisions` is append-only and the runtime role holds no `UPDATE`, so
a timestamp cannot be forced. It does not need to be: eight decisions inside one
`record_decision` loop in one transaction all take `now()` at that transaction's
start, which is the identical value. The test asserts they DID share it before
asserting anything about their order — without that guard a run in which the
stamps happened to differ would pass while testing nothing.

## What this did and did not reach

It reached an output: `derive` picked the exclusion reason off the FIRST vote,
so a reason disagreement written in a single transaction could hash two ways and
`canonical_counts_hash` was not reproducible. That route is closed by the change
that ships beside this one — a reason disagreement is now a disagreement, so
surviving votes all carry the same reason and the pick is order-free. What is
left is a documented contract that was not true, which is worth a `decision_id`
in the ORDER BY on its own: `flow:rebuild` cannot catch a non-determinism here,
because it compares a hash from this code against a hash from this code.

Same class as the unordered `select(ScreeningConflict)` scan fixed at
`flow.py:361`, and fixed the same way: state the whole order.
"""

from __future__ import annotations

import json
import os
import sys
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

TENANT = tenant("tenant-decision-order")
LEAD = "decision-order-lead"
REVIEWER = "decision-order-rv"

#: Enough decisions that an accidental pass is not a plausible explanation.
#: The ids are `uuid4`, so the chance that the physical order this read
#: happens to yield is ALSO ascending by id is 1/8! — about 1 in 40,000.
DECISION_COUNT = 8


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
def eight_undecided_records(client):  # noqa: ANN001, ANN201
    """One reviewer, one stage, `DECISION_COUNT` records with no decisions yet."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "decision-order", "external_key": f"do-{uuid.uuid4()}", "title": "Decision order"},
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
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Study {i}", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"} for i in range(DECISION_COUNT)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": DECISION_COUNT},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    assert len(queue) == DECISION_COUNT, queue
    return {
        "review_id": uuid.UUID(review_id),
        "stage_id": uuid.UUID(stage_id),
        "record_ids": [uuid.UUID(item["review_record_id"]) for item in queue],
    }


def test_decisions_sharing_a_decided_at_are_still_read_in_one_fixed_order(eight_undecided_records) -> None:
    """`decided_at` alone is a PARTIAL order, so it cannot decide this read."""
    from actor import Actor, ActorRole, bind_actor
    from application.services import blinded_reads
    from application.services.screening import record_decision

    fixture = eight_undecided_records
    reviewer = Actor(tenant_id=TENANT, subject_id=REVIEWER, role=ActorRole.REVIEWER)
    lead = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})

    # ONE transaction, so `decided_at` — `server_default=now()`, which is
    # TRANSACTION start and not statement time — is the identical value on all
    # eight rows. This is not a contrived value poked into the column: it is
    # what the column's own default produces, and two decisions landing in one
    # transaction is reachable from any future batch write.
    try:
        with Session(engine) as writer:
            writer.begin()
            bind_actor(writer, reviewer)
            for record_id in fixture["record_ids"]:
                record_decision(
                    writer,
                    reviewer,
                    fixture["review_id"],
                    stage_id=fixture["stage_id"],
                    round_number=1,
                    review_record_id=record_id,
                    disposition="include",
                    rationale="screened",
                )
            writer.commit()

        with Session(engine) as reader:
            reader.begin()
            bind_actor(reader, lead)
            rows = blinded_reads.unblinded_current_decisions(reader, review_id=fixture["review_id"])
    finally:
        engine.dispose()

    assert len(rows) == DECISION_COUNT, rows
    stamps = {row.decided_at for row in rows}
    assert len(stamps) == 1, (
        "the eight decisions did not share a `decided_at`, so this run never constructed the tie "
        f"it exists to test and proves nothing either way: {sorted(stamps)}"
    )

    got = [row.decision_id for row in rows]
    assert got == sorted(got), (
        "eight decisions with an identical `decided_at` came back in an order nothing in the "
        "query determines. `ORDER BY decided_at` is a PARTIAL order over this table — the column "
        "is a transaction-start timestamp at microsecond resolution — so Postgres may return "
        "these rows in any order it likes, on either of two reads of identical immutable "
        f"history: {got}"
    )
