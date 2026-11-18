"""A reviewer was asked to screen a record deduplication had already removed.

Found by driving a complete two-stage review through the image production is
serving, pulled by digest. Assignments are append-only and are minted before
deduplication is resolved, so a record the lead later merges away stays assigned
— and the queue kept serving it.

Measured against that image, a three-record corpus with one duplicate pair
merged:

    flow duplicates_removed : 1
    queue length            : 3      <- the merged-away record is in it
    decision on it          : 201    <- accepted
    ledger afterwards       : records_screened 0, included 0

So the reviewer read it, judged it, and `derive` discarded the verdict: the
record is bucketed `duplicate_removed` whatever they said. Silent on both sides —
nothing told them the record was gone, and nothing told them their decision would
not count. On the 93-paper corpus this review is for, that is real wasted effort.

## The denominator had to move with it

Filtering the queue alone left `assigned_to_me` counting the removed record while
`decided_by_me` could never reach it: three served, three decided, **"3 of 4"
forever**. That is the same "0 of 60" confusion the progress counter was added to
remove, one step along, and
`test_the_queue_never_reveals_the_other_reviewer_s_progress` is what caught it.

## Filtered on the CURRENT resolution

A merge later reversed by a split returns the record to the queue rather than
stranding it — the same reason `flow._collect` reads the chain tail, and the
control below.
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

TENANT = tenant("tenant-queue-merged")
LEAD = "queue-merged-lead"
REVIEWER = "queue-merged-reviewer"


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


@pytest.fixture
def merged_pair(client):  # noqa: ANN001, ANN201
    """Three records, one duplicate pair merged, one reviewer assigned."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "queue-merged", "external_key": f"qm-{uuid.uuid4()}", "title": "Queue merged"},
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
    doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:8]}"
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": "The trial, from PubMed", "year": 2021, "doi": doi},
                    {"title": "The trial, from Embase", "year": 2021, "doi": doi},
                    {"title": "An unrelated study", "year": 2021, "doi": f"10.1001/jama.{uuid.uuid4().hex[:6]}"},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    corpus = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]

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

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER], "corpus_version_id": corpus},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "cluster_id": pair["cluster_id"],
        "survivor": survivor,
        "loser": loser,
    }


def _queue(client, fixture) -> dict:  # noqa: ANN001
    got = client.get(
        f"/api/reviews/{fixture['review_id']}/screening/queue",
        params={"stage_id": fixture["stage_id"], "limit": 50},
        headers=headers(REVIEWER, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_the_queue_omits_the_record_the_merge_removed(client, merged_pair) -> None:
    """THE DEFECT. The reviewer was served work whose verdict cannot land."""
    counts = client.get(f"/api/reviews/{merged_pair['review_id']}/flow", headers=headers()).json()["counts"]
    assert counts["duplicates_removed"] == 1, counts

    queue = _queue(client, merged_pair)
    served = [item["review_record_id"] for item in queue["items"]]
    assert merged_pair["loser"] not in served, (
        "the queue served a record deduplication had already removed; the reviewer's verdict on it "
        f"would be discarded by the ledger: {served}"
    )
    assert merged_pair["survivor"] in served, "the SURVIVOR must still be screened"
    assert len(served) == counts["records_total"] - counts["duplicates_removed"], (
        f"the queue and the ledger disagree about what is still in the review: {len(served)} vs {counts}"
    )


def test_the_progress_counter_can_reach_the_end(client, merged_pair) -> None:
    """The denominator moves with the queue, or the bar never fills.

    Filtering the queue alone left `assigned_to_me` counting the removed record
    while `decided_by_me` could never reach it — "2 of 3" for a reviewer with
    nothing left to do.
    """
    queue = _queue(client, merged_pair)
    for item in queue["items"]:
        got = client.post(
            f"/api/reviews/{merged_pair['review_id']}/decisions",
            json={
                "stage_id": merged_pair["stage_id"],
                "round_number": 1,
                "review_record_id": item["review_record_id"],
                "disposition": "include",
                "rationale": "screened",
            },
            headers=headers(REVIEWER, "reviewer"),
        )
        assert got.status_code == 201, got.text

    after = _queue(client, merged_pair)
    assert after["assigned_to_me"] == after["decided_by_me"], (
        "the reviewer has decided every record they were served and the counter still says there is "
        f"work left: {after['decided_by_me']} of {after['assigned_to_me']}"
    )


def test_a_split_returns_the_record_to_the_queue(client, merged_pair) -> None:
    """NEGATIVE CONTROL. Filtered on the CURRENT resolution, not on ever-merged.

    Without this, "hide merged members" is indistinguishable from "hide them
    permanently", and reversing a wrong merge — which `resolve_cluster` exists to
    allow — would strand the record where no reviewer can reach it.
    """
    assert merged_pair["loser"] not in [i["review_record_id"] for i in _queue(client, merged_pair)["items"]]

    split = client.post(
        f"/api/reviews/{merged_pair['review_id']}/duplicate-clusters/{merged_pair['cluster_id']}:resolve",
        json={"resolution_type": "split", "rationale": "different trials after all"},
        headers=headers(),
    )
    assert split.status_code in (200, 201), split.text

    served = [i["review_record_id"] for i in _queue(client, merged_pair)["items"]]
    assert merged_pair["loser"] in served, (
        "the merge was reversed and the record is still hidden from the reviewer: " + json.dumps(served)
    )
