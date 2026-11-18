"""The queue's progress counters were measured on two different populations.

## The defect

`GET /api/reviews/{id}/screening/queue` paginates — `limit` defaults to 50 and is
capped at 200 — and the response carries two numbers that a client renders as one
ratio:

* `assigned_to_me`, counted over the whole STAGE;
* `decided_by_me`, counted over the records **on this page**
  (`screening.py:357`: `len([i for i in items if i["my_decision"] is not None])`).

With 60 assignments and the default page, the second number cannot exceed 50 no
matter how much work the reviewer has done, and every decision taken on page 2
is invisible to it. A reviewer who has finished the stage reads "50 of 60" at
best, and — because the studio only ever requests the first page — "0 of 60"
after correctly deciding everything on page 2. The counter cannot answer the one
question it exists to answer: am I done?

## Measured, not assumed

Run against `97c441d` (the commit before this fix) with this file added and the
source unchanged, `uv run pytest tests/api/test_the_queue_counts_the_whole_stage.py`:

* `test_decided_by_me_counts_the_whole_stage_not_the_page` — **FAILED**:
  `assert 2 == 5`, with the reviewer having recorded five decisions in the stage
  (two on page 1, three on page 2). The three on page 2 are not counted.
* `test_the_queue_reports_the_stage_total_not_the_page_size` — **FAILED** with
  `KeyError: 'total'`. Structural: the field a client would need in order to be
  unable to render a truncated ratio did not exist.

Both pass after the change.

## Why this is asserted on the RESPONSE

Unlike `test_read_screen_throughput.py`, the defect here IS visible in the
payload — that is what makes it a lie rather than an inefficiency — so nothing is
gained by counting SQL. The one thing worth keeping honest at the SQL level (the
counters must not become a per-item read) is already pinned by
`test_read_screen_throughput.py::test_the_screening_queue_does_not_issue_two_statements_per_item`.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-queue-counts")
LEAD = "queue-counts-lead"
REVIEWER = "queue-counts-reviewer"
#: Settles `first`-stage eligibility for the `scoped` fixture WITHOUT touching
#: REVIEWER's own debt there — see that fixture's docstring.
SETTLER = "queue-counts-settler"

#: More than one default page, deliberately by a small margin: 60 records at
#: `limit=50` is the smallest fixture that reproduces the production shape (a
#: full first page plus a remainder) without the suite paying for 200.
RECORDS = 60
PAGE = 50

#: Decisions taken on the SECOND page. These are the ones a page-scoped counter
#: cannot see, and the number is deliberately not equal to `DECIDED_ON_PAGE_1`
#: so a counter that happened to report the wrong page is still red.
DECIDED_ON_PAGE_2 = 3
DECIDED_ON_PAGE_1 = 2


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


@pytest.fixture(scope="module")
def fx(client) -> dict:  # noqa: ANN001
    """One activated review, `RECORDS` records, all assigned to one reviewer."""
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "queue-counts",
            "external_key": f"qc-{uuid.uuid4()}",
            "title": "Counting the whole stage",
        },
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
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Paper {n}", "year": 2000, "doi": f"10.1/qc{uuid.uuid4()}"} for n in range(RECORDS)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["assignments_created"] == RECORDS, assigned.json()
    return {"review_id": review_id, "stage_id": stage_id}


def queue(client, fx: dict, **params) -> dict:  # noqa: ANN001, ANN003
    response = client.get(
        f"/api/reviews/{fx['review_id']}/screening/queue",
        params={"stage_id": fx["stage_id"], **params},
        headers=headers(REVIEWER, "reviewer"),
    )
    assert response.status_code == 200, response.text
    return response.json()


def decide(  # noqa: ANN001
    client,
    fx: dict,
    review_record_id: str,
    *,
    stage_id: str | None = None,
    round_number: int = 1,
    disposition: str = "include",
    supersedes_decision_id: str | None = None,
) -> dict:
    body = {
        "stage_id": stage_id or fx["stage_id"],
        "round_number": round_number,
        "review_record_id": review_record_id,
        "disposition": disposition,
    }
    if supersedes_decision_id is not None:
        body["supersedes_decision_id"] = supersedes_decision_id
    response = client.post(
        f"/api/reviews/{fx['review_id']}/decisions", json=body, headers=headers(REVIEWER, "reviewer")
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_the_cursor_is_present_on_a_truncated_page_and_absent_on_the_last(client, fx) -> None:  # noqa: ANN001
    """A client must be able to tell "there is more" from "that was all".

    `next_cursor` is the only signal, so its two states are pinned here rather
    than left to the L4 shape test, which pages a two-record queue at `limit=1`
    and never reaches a last page.
    """
    first = queue(client, fx, limit=PAGE)
    assert first["count"] == PAGE, first["count"]
    assert first["next_cursor"], f"a {RECORDS}-record queue at limit={PAGE} must offer a second page: {first!r}"

    second = queue(client, fx, limit=PAGE, cursor=first["next_cursor"])
    assert second["count"] == RECORDS - PAGE, second["count"]
    assert second["next_cursor"] is None, (
        f"the last page offers a cursor, which sends a client paginating forever: {second['next_cursor']!r}"
    )

    ids = {i["review_record_id"] for i in first["items"]} | {i["review_record_id"] for i in second["items"]}
    assert len(ids) == RECORDS, f"the two pages overlap or drop records: {len(ids)} distinct of {RECORDS}"

    whole = queue(client, fx, limit=200)
    assert whole["count"] == RECORDS
    assert whole["next_cursor"] is None, "an untruncated queue claims there is more"


def test_the_queue_reports_the_stage_total_not_the_page_size(client, fx) -> None:  # noqa: ANN001
    """`count` is the page; `total` and `assigned_to_me` are the stage.

    Without a stage-wide total in the payload, a client holding one page cannot
    render a ratio at all without inventing a denominator from `len(items)`.
    """
    page = queue(client, fx, limit=PAGE)
    assert page["count"] == PAGE, "`count` is not the number of items on this page"
    assert page["total"] == RECORDS, f"`total` reports {page['total']!r} for {RECORDS} assignments — it is the page"
    assert page["assigned_to_me"] == RECORDS, page["assigned_to_me"]

    last = queue(client, fx, limit=PAGE, cursor=page["next_cursor"])
    assert last["count"] == RECORDS - PAGE
    assert last["total"] == RECORDS, "`total` changed between pages of the same stage"
    assert last["assigned_to_me"] == RECORDS


def test_decided_by_me_counts_the_whole_stage_not_the_page(client, fx) -> None:  # noqa: ANN001
    """The gate. Decisions taken on page 2 must count on page 1's read.

    `assigned_to_me` is stage-wide and `decided_by_me` was page-scoped, so the
    pair was a ratio between two different populations. This test decides on
    BOTH pages, so a counter that is page-scoped in either direction is red: the
    page-1 read reports 2 of 5 on the defect, and the page-2 read reports 3 of 5.
    """
    first = queue(client, fx, limit=PAGE)
    second = queue(client, fx, limit=PAGE, cursor=first["next_cursor"])

    for item in second["items"][:DECIDED_ON_PAGE_2]:
        decide(client, fx, item["review_record_id"])
    for item in first["items"][:DECIDED_ON_PAGE_1]:
        decide(client, fx, item["review_record_id"])
    expected = DECIDED_ON_PAGE_1 + DECIDED_ON_PAGE_2

    after_first = queue(client, fx, limit=PAGE)
    assert after_first["decided_by_me"] == expected, (
        f"`decided_by_me` reports {after_first['decided_by_me']!r} on the first page after "
        f"{expected} decisions in the stage. It is counting the page, so the "
        f"{DECIDED_ON_PAGE_2} decisions taken beyond record {PAGE} are invisible and the reviewer "
        "cannot tell whether they are finished."
    )
    assert after_first["assigned_to_me"] == RECORDS, (
        "`assigned_to_me` moved; the pair must be two counts over the SAME population"
    )

    after_second = queue(client, fx, limit=PAGE, cursor=first["next_cursor"])
    assert after_second["decided_by_me"] == expected, (
        f"`decided_by_me` reports {after_second['decided_by_me']!r} on the second page; the counter "
        "still depends on which page was requested"
    )

    # And the per-item fact stays page-scoped, because that one IS about the page.
    assert len([i for i in after_first["items"] if i["my_decision"] is not None]) == DECIDED_ON_PAGE_1
    assert len([i for i in after_second["items"] if i["my_decision"] is not None]) == DECIDED_ON_PAGE_2


# ---------------------------------------------------------------------------
# Every predicate of the count, pinned individually
# ---------------------------------------------------------------------------
#
# The three tests above use one stage, one round and no corrections, so three of
# the four predicates in `own_progress_counts`'s `decided` subquery could be
# DELETED with the whole suite still green. An adversarial pass proved it:
#
#     | mutation                                | suite       |
#     |-----------------------------------------|-------------|
#     | drop `reviewer_subject_id == subject`   | 1 failed    |
#     | drop `supersedes_decision_id IS NULL`   | 779 passed  |
#     | drop `stage_id == stage_id`             | 779 passed  |
#     | drop `round_number == round_number`     | 779 passed  |
#
# This repository has been here before: `own_decisions_for`'s docstring records
# an identical finding — a predicate deleted, 749 tests still green — and this
# function was written to fix the defect that finding was about. So each
# predicate now has a test that fails when it alone is removed, and each was
# checked by removing it.

#: Small on purpose. These pin SCOPE, not pagination, and every record costs an
#: assignment row in three separate (stage, round) combinations.
SCOPED_RECORDS = 6


@pytest.fixture
def scoped(client) -> dict:  # noqa: ANN001
    """A review with TWO stages, and the first stage assigned in TWO rounds.

    Function-scoped: each of the tests below records decisions, and a shared
    fixture would let one test's writes satisfy another test's assertion.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "queue-counts", "external_key": f"qs-{uuid.uuid4()}", "title": "Scoped counts"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}, {"key": "ft", "name": "Full text"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Scoped {n}", "year": 2000, "doi": f"10.1/qs{uuid.uuid4()}"}
                    for n in range(SCOPED_RECORDS)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stages = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]
    assert len(stages) == 2, stages
    first, second = stages[0]["stage_id"], stages[1]["stage_id"]

    # SETTLE STAGE ORDER 1 BEFORE ASSIGNING STAGE ORDER 2 (`ERS #42`), WITH A
    # SETTLER SUBJECT — NEVER REVIEWER (`ERS #65`, finding 2).
    #
    # `create_assignments` now scopes stage order N > 1 to records that settled
    # `included` at stage N-1, so `second` (order 2) gets nothing until `first`
    # has a settled verdict on every record. It ALSO now refuses to assign a
    # reviewer at stage N while they still owe a decision at stage N-1 — an
    # assignment held, nothing decided — and REVIEWER must hold exactly that at
    # BOTH `first` rounds (1 and 2) AND `second` round 1 by the time this
    # fixture returns: every test below casts REVIEWER's first decision at one
    # of those three positions itself. So REVIEWER cannot be named in ANY
    # `first`-stage assignment before `second` is granted, or the guard would
    # refuse REVIEWER there. SETTLER carries the settling vote instead, on its
    # own round 1 of `first` — untouched by any test below, which only ever
    # look at REVIEWER's positions.
    settled_assignment = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": first, "reviewer_subject_ids": [SETTLER]},
        headers=headers(),
    )
    assert settled_assignment.status_code == 201, settled_assignment.text
    settling_records = [
        item["review_record_id"]
        for item in client.get(
            f"/api/reviews/{review_id}/screening/queue",
            params={"stage_id": first},
            headers=headers(SETTLER, "reviewer"),
        ).json()["items"]
    ]
    assert len(settling_records) == SCOPED_RECORDS, settling_records
    for record_id in settling_records:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": first,
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": "include",
            },
            headers=headers(SETTLER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    # `second`, WHILE REVIEWER OWES NOTHING AT `first` — REVIEWER holds no
    # assignment there yet.
    assigned_second = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": second, "round_number": 1, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned_second.status_code == 201, assigned_second.text
    assert assigned_second.json()["assignments_created"] == SCOPED_RECORDS, assigned_second.json()

    # ONLY NOW: `first`, rounds 1 and 2, for REVIEWER — deliberately left
    # undecided. Appending these does not touch the `second`-stage assignment
    # already granted.
    for stage_id, round_number in ((first, 1), (first, 2)):
        assigned = client.post(
            f"/api/reviews/{review_id}/assignments",
            json={"stage_id": stage_id, "round_number": round_number, "reviewer_subject_ids": [REVIEWER]},
            headers=headers(),
        )
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["assignments_created"] == SCOPED_RECORDS, assigned.json()

    return {"review_id": review_id, "stage_id": first, "other_stage_id": second}


def test_a_corrected_decision_is_counted_once_not_twice(client, scoped) -> None:  # noqa: ANN001
    """Pins `supersedes_decision_id IS NULL`.

    A correction is a second ROW on the same record — the table is append-only,
    so nothing is overwritten. Counting rows instead of records makes
    `decided_by_me` exceed `assigned_to_me` for a reviewer who changes their
    mind: "7 of 6 done", from a reviewer who has decided 6.

    ⚠️ `supersedes_decision_id IS NULL` selects the FIRST row of a supersession
    chain, not the last. That WAS a live defect on every read that returned a
    decision's content, and it is fixed — those now use
    `blinded_reads._is_current()`. The two COUNT paths deliberately still ask
    for the chain head, because the head is what counts RECORDS and it is the
    only form `ix_decisions_reviewer_progress` (`0010`) can serve.

    So this test pins the COUNT and deliberately does not assert which
    disposition `my_decision` shows — that belongs to
    `tests/api/test_the_queue_shows_the_current_decision.py`, which fails the
    moment this predicate is copy-pasted back over `own_decisions_for`.
    """
    records = [i["review_record_id"] for i in queue(client, scoped)["items"]]
    first = decide(client, scoped, records[0])
    decide(client, scoped, records[1])
    correction = decide(
        client, scoped, records[0], disposition="uncertain", supersedes_decision_id=first["decision_id"]
    )
    assert correction["supersedes_decision_id"] == first["decision_id"], correction

    body = queue(client, scoped)
    assert body["decided_by_me"] == 2, (
        f"`decided_by_me` is {body['decided_by_me']!r} after 2 records were decided and one of them "
        "corrected. The correction is being counted as a second decided record, so a reviewer who "
        "revises anything is told they have done more work than they have."
    )
    assert body["assigned_to_me"] == SCOPED_RECORDS
    assert body["decided_by_me"] <= body["assigned_to_me"], "more records decided than assigned"


def test_another_stage_of_the_same_review_is_not_counted(client, scoped) -> None:  # noqa: ANN001
    """Pins `stage_id == stage_id`.

    Title/abstract and full text are different populations of work with
    different assignment sets, and `assigned_to_me` has always been per-stage.
    Without this predicate a reviewer part-way through full text is told their
    title/abstract screening is unfinished, or finished, depending only on which
    stage they screened first.
    """
    records = [i["review_record_id"] for i in queue(client, scoped)["items"]]
    other = [i["review_record_id"] for i in queue(client, scoped, stage_id=scoped["other_stage_id"])["items"]]
    for record in records[:2]:
        decide(client, scoped, record)
    for record in other[:3]:
        decide(client, scoped, record, stage_id=scoped["other_stage_id"])

    here = queue(client, scoped)
    assert here["decided_by_me"] == 2, (
        f"`decided_by_me` is {here['decided_by_me']!r} for a stage with 2 decisions; the other "
        "stage's 3 are being counted into it"
    )
    there = queue(client, scoped, stage_id=scoped["other_stage_id"])
    assert there["decided_by_me"] == 3, f"the other stage reports {there['decided_by_me']!r} of its own 3"
    assert here["assigned_to_me"] == there["assigned_to_me"] == SCOPED_RECORDS


def test_another_round_of_the_same_stage_is_not_counted(client, scoped) -> None:  # noqa: ANN001
    """Pins `round_number == round_number`.

    A second round re-screens the same records under the same stage, so the
    round is the ONLY thing separating the two populations — every other column
    matches. It is also the case a stage predicate cannot catch.
    """
    records = [i["review_record_id"] for i in queue(client, scoped)["items"]]
    for record in records[:2]:
        decide(client, scoped, record)
    for record in records[:3]:
        decide(client, scoped, record, round_number=2)

    first = queue(client, scoped)
    assert first["decided_by_me"] == 2, (
        f"round 1 reports {first['decided_by_me']!r} of its own 2 decisions; round 2's 3 are being "
        "counted into it, on the same records and in the same stage"
    )
    second = queue(client, scoped, round_number=2)
    assert second["decided_by_me"] == 3, f"round 2 reports {second['decided_by_me']!r} of its own 3"
    assert first["assigned_to_me"] == second["assigned_to_me"] == SCOPED_RECORDS
