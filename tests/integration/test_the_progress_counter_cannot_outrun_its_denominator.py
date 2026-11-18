"""`decided_by_me` counted records `assigned_to_me` had stopped counting: "3 of 2".

`blinded_reads.own_progress_counts` returns the pair the queue renders as the
reviewer's only completion signal. #56 removed merged-away records from the
DENOMINATOR — the `assigned` subquery carries `notin_(removed)` — and left the
NUMERATOR alone. A decision taken before the lead merged the record is still a
row in `screening_decisions` with `supersedes_decision_id IS NULL`, so it is
still counted, on a record the queue no longer serves and the ledger buckets
`duplicate_removed`.

Measured on `bb7ea93`, three records including one duplicate pair, all three
screened, then merged:

    {"count": 2, "total": 2, "assigned_to_me": 2, "decided_by_me": 3}

150% complete. The two halves of one number were fixed in one direction only,
which is strictly worse than the "3 of 4" they replaced: an unreachable
denominator at least reads as "there is work left", whereas a numerator past its
denominator says nothing at all about whether the reviewer may stop.

## The current studio hides it, which is not the same as it being fixed

`collectQueue` in `fr` recomputes `decided_by_me` from the joined items rather
than reading the field, so today's studio renders "2 of 2" — measured. This is an
API-contract defect that one client happens to mask: any other consumer of
`GET /screening/queue`, and any future version of that one, reads "3 of 2"
straight off the wire. It is asserted here against the API, where it lives.

The two counts are computed as two subqueries of ONE statement precisely because
they must move together, and this is the failure that argument was written
against.

## The write that produces the orphan is refused now too

`record_decision` had no removal guard at all: a decision on a merged-away record
returned 201, and `derive` then discarded it into `duplicate_removed`. Both
directions were silent. The queue has not served such a record since #56, so the
only ways to reach it are a stale page, a client that kept an id, or a merge
landing between a reviewer's read and their POST — and in every one of them
accepting the write is the wrong answer, because the verdict provably cannot
land. See `test_a_decision_on_a_merged_away_record_is_refused` and the comment at
the guard.

Decisions taken BEFORE the merge are untouched: they were valid when they were
made, the tables are append-only, and rewriting history to make a counter tidy
would be a far worse defect than the counter. `test_a_decision_taken_before_the_merge_is_not_unwritten`
is the control.
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

TENANT = tenant("tenant-progress-ceiling")
LEAD = "progress-ceiling-lead"
REVIEWER = "progress-ceiling-reviewer"


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


def _build(client, *, slug: str, screen_before_the_merge: bool) -> dict:  # noqa: ANN001
    """Three records including a duplicate pair, screened either side of the merge.

    `screen_before_the_merge` is the only difference between the two fixtures
    below, and it separates two questions that look like one:

    * TRUE — the decisions were legitimate when they were taken and the merge
      retrospectively removed one of their records. Nothing is refused; what is
      at stake is only what the counter says afterwards.
    * FALSE — the merge has already happened when the reviewer POSTs, so the
      write is the thing being judged. This is the case `record_decision` had no
      guard for at all.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "progress-ceiling", "external_key": f"{slug}-{uuid.uuid4()}", "title": "Progress ceiling"},
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
    doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:10]}"
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "The trial, from PubMed", "year": 2021, "doi": doi},
                    {"title": "The trial, from Embase", "year": 2021, "doi": doi},
                    {"title": "An unrelated study", "year": 2021, "doi": f"10.1001/jama.{uuid.uuid4().hex[:8]}"},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    corpus = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER], "corpus_version_id": corpus},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text

    if screen_before_the_merge:
        queue = client.get(
            f"/api/reviews/{review_id}/screening/queue",
            params={"stage_id": stage_id, "limit": 50},
            headers=headers(REVIEWER, "reviewer"),
        ).json()
        assert len(queue["items"]) == 3, queue
        for item in queue["items"]:
            decided = client.post(
                f"/api/reviews/{review_id}/decisions",
                json={
                    "stage_id": stage_id,
                    "round_number": 1,
                    "review_record_id": item["review_record_id"],
                    "disposition": "include",
                    "rationale": "meets the criteria",
                },
                headers=headers(REVIEWER, "reviewer"),
            )
            assert decided.status_code == 201, decided.text

    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers()).json()
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    pair = next(c for c in clusters if len(c["members"]) == 2)
    survivor = pair["members"][0]["review_record_id"]
    loser = pair["members"][1]["review_record_id"]
    merged = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{pair['cluster_id']}:resolve",
        json={"resolution_type": "merge", "survivor_record_id": survivor, "rationale": "same DOI"},
        headers=headers(),
    )
    assert merged.status_code in (200, 201), merged.text
    return {"review_id": review_id, "stage_id": stage_id, "survivor": survivor, "loser": loser}


@pytest.fixture
def screened_then_merged(client):  # noqa: ANN001, ANN201
    """Three records, ALL screened, and only then does the lead merge the pair."""
    return _build(client, slug="screened-then-merged", screen_before_the_merge=True)


@pytest.fixture
def merged_before_screening(client):  # noqa: ANN001, ANN201
    """The merge lands FIRST, so nothing has been decided on the removed record.

    Separate from `screened_then_merged` because a POST against a record the
    reviewer has already decided trips `state_conflict` ("a correction must
    supersede the previous decision") before any removal guard is reached — a 409
    that says nothing about whether the record is still in the review. Measured
    on `bb7ea93` while writing this file, which is why the two cases are two
    fixtures.
    """
    return _build(client, slug="merged-before-screening", screen_before_the_merge=False)


def _queue(client, fixture) -> dict:  # noqa: ANN001
    got = client.get(
        f"/api/reviews/{fixture['review_id']}/screening/queue",
        params={"stage_id": fixture["stage_id"], "limit": 50},
        headers=headers(REVIEWER, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_the_reviewer_is_never_told_they_decided_more_than_they_hold(client, screened_then_merged) -> None:
    """THE DEFECT. `decided_by_me` counts a record `assigned_to_me` does not."""
    queue = _queue(client, screened_then_merged)

    assert queue["assigned_to_me"] == 2, (
        "the fixture's merge did not move the denominator, so this proves nothing: " + json.dumps(queue)
    )
    assert queue["decided_by_me"] <= queue["assigned_to_me"], (
        f"the queue reports {queue['decided_by_me']} of {queue['assigned_to_me']} — the reviewer's only "
        "completion signal reads over 100%. #56 removed merged-away records from the denominator and "
        "left the numerator counting them: " + json.dumps(queue)
    )
    assert queue["decided_by_me"] == 2, json.dumps(queue)


def test_the_two_counts_agree_when_the_work_is_finished(client, screened_then_merged) -> None:
    """The signal has to be usable, not merely bounded.

    Clamping the numerator to the denominator would satisfy the assertion above
    and still be wrong — the pair must equal each other here, because the
    reviewer has in fact decided every record that is still in the review.
    """
    queue = _queue(client, screened_then_merged)
    assert queue["decided_by_me"] == queue["assigned_to_me"], (
        "the reviewer has decided every record still in the review and the counter disagrees: " + json.dumps(queue)
    )
    assert [item["review_record_id"] for item in queue["items"]] == [] or all(
        item["my_decision"] is not None for item in queue["items"]
    ), "a record the queue still serves has no decision on it, so the counts above describe something else"


def test_a_decision_taken_before_the_merge_is_not_unwritten(client, screened_then_merged) -> None:
    """THE CONTROL. The count moves; the history does not.

    `screening_decisions` is append-only and the decision was valid when it was
    taken. A fix that deleted or superseded it to make the counter tidy would be
    a far worse defect than the counter — it would rewrite a named human's
    recorded judgement — so the row must still be there and still be readable by
    the lead.
    """
    listed = client.get(
        f"/api/reviews/{screened_then_merged['review_id']}/decisions",
        params={"review_record_id": screened_then_merged["loser"], "limit": 500},
        headers=headers(),
    )
    assert listed.status_code == 200, listed.text
    on_record = listed.json()["items"]
    assert on_record, (
        "the decision taken on the record before it was merged away has been erased from the "
        "append-only history: " + listed.text
    )


def test_a_decision_on_a_merged_away_record_is_refused(client, merged_before_screening) -> None:
    """The write that mints the orphan is refused, not silently discarded.

    The merge lands before the reviewer POSTs — a stale queue page, a client that
    kept an id, or a merge arriving between a reviewer's read and their submit.
    Under the defect this returned 201 and `derive` discarded the verdict into
    `duplicate_removed`: the reviewer was told their judgement was recorded and
    the ledger never counted it. Measured on `bb7ea93` in exactly this shape and
    written down in `_records_removed_by_a_merge`'s own comment.

    422 rather than 404: the record exists, the caller holds an assignment on it
    and can see it in `GET /records`, so a 404 is a claim they can disprove. What
    is wrong is the state of the review, which is what `unprocessable_state`
    names, and the detail says a merge removed it so the caller can tell "this
    was merged away" from "you have no assignment".
    """
    refused = client.post(
        f"/api/reviews/{merged_before_screening['review_id']}/decisions",
        json={
            "stage_id": merged_before_screening["stage_id"],
            "round_number": 1,
            "review_record_id": merged_before_screening["loser"],
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
            "rationale": "not relevant",
        },
        headers=headers(REVIEWER, "reviewer"),
    )
    assert refused.status_code == 422, (
        "a decision on a record deduplication had already removed was accepted; `derive` buckets it "
        "`duplicate_removed` whatever it says, so the reviewer is told their verdict was recorded and "
        "the ledger never counts it: " + refused.text
    )
    assert refused.headers["content-type"].startswith("application/problem+json"), refused.headers
    body = refused.json()
    assert body["type"].endswith("unprocessable_state"), body
    assert "merge" in body["detail"], body


def test_the_surviving_record_can_still_be_decided(client, merged_before_screening) -> None:
    """NEGATIVE CONTROL. The guard refuses removal, not deduplication.

    Same review, same cluster, same POST shape as the refusal above — only the
    record differs. Without it, "refuse decisions on merged-away records" is
    indistinguishable from "refuse decisions on any record that was ever in a
    cluster", which would lock the SURVIVOR — the one record of the pair the
    review exists to screen — out of the stage entirely, and the refusal test
    would still be green.
    """
    accepted = client.post(
        f"/api/reviews/{merged_before_screening['review_id']}/decisions",
        json={
            "stage_id": merged_before_screening["stage_id"],
            "round_number": 1,
            "review_record_id": merged_before_screening["survivor"],
            "disposition": "include",
            "rationale": "meets the criteria",
        },
        headers=headers(REVIEWER, "reviewer"),
    )
    assert accepted.status_code == 201, accepted.text
