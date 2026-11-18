"""The merged-away filter ate the page, and with it the cursor.

`screening_queue` fetches `limit + 1` assignments to learn whether a further page
exists, then drops the rows a merge has already removed, and only THEN computes
`has_more = len(rows) > limit`. The filter shrinks the very window the lookahead
was measured against, so a single removed record anywhere inside it makes
`has_more` false — `next_cursor` comes back `null` while the reviewer still holds
assignments they have never been shown.

Measured on `bb7ea93` against this file's own six-pair fixture, one reviewer with
six live assignments, `?limit=1`:

    page 1: count=1  next_cursor=None  assigned_to_me=6

There is no way out of it from the client side. The cursor is base64 over
`"<assigned_at>|<assignment_id>"` and is deliberately opaque, so a caller cannot
synthesise the key for row two, and re-requesting page one returns page one. The
reviewer's remaining five records are unreachable through the only route that
serves them.

## What it blocks, sized honestly

`not_yet_screened` is derived from decisions against the live corpus. Records the
reviewer can never be served are records they can never decide, so the count never
reaches zero — and `create_export` refuses while it is above zero.

It fires when a round holds MORE assignments than the page limit AND a removed
record falls inside the fetched window. The studio asks for 200
(`fr` `frontend/src/lib/api/reviews.ts`, `QUEUE_PAGE_LIMIT`), so a review of 200
records or fewer is unaffected through that client. Above the limit it is a hard
block, measured on a 220-record round:

    collectQueue(limit=200): 192 of assigned_to_me=210, 1 page, next_cursor=None
    POST /exports          : 422 "18 records are not yet screened"
    POST /flow:rebuild     : does not help

`POST :close` is NOT among the casualties — it gates on unresolved conflicts, not
on `not_yet_screened`, and returns 200 on a stuck review. And there is an escape
the shape of the defect hides: splitting every merge returns the records to the
queue. It re-introduces the #56 defect the filter exists to fix, so this is
recoverable-but-terrible rather than unrecoverable.

## Why a fix to `has_more` alone would be wrong

Computing `has_more` from the RAW row count while still taking the cursor from the
FILTERED `rows[-1]` leaves the worst case exactly where it was: a page whose rows
were all removed has `rows == []`, so `next_cursor` is `None` again and the queue
stops. `test_a_page_whose_every_row_was_removed_still_advances` is the case that
separates the two.

## Why the fixture merges the FIRST member of each pair

`assigned_at` is the transaction timestamp and is identical across a batch of
assignments, so the queue's order is decided by the `assignment_id` tiebreak,
which is a `uuid4` and therefore random. A fixture that merged an arbitrary
member would reproduce the defect only most of the time.

So the fixture reads the queue BEFORE any merge — at which point nothing is
filtered and the response IS the order — and then, for each duplicate pair, names
the LATER-ordered member as the survivor. The first row of the queue is then
always a removed record, which is the exact case the lookahead cannot survive,
and the test is deterministic rather than sampled.
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

TENANT = tenant("tenant-queue-paging")
LEAD = "queue-paging-lead"
REVIEWER = "queue-paging-reviewer"


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


def _queue(client, review_id: str, stage_id: str, *, limit: int, cursor: str | None = None) -> dict:  # noqa: ANN001
    params: dict[str, object] = {"stage_id": stage_id, "limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    got = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params=params,
        headers=headers(REVIEWER, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()


def _page_to_exhaustion(client, review_id: str, stage_id: str, *, limit: int) -> list[str]:  # noqa: ANN001
    """Every record id the queue will serve, following its own cursor.

    Bounded, because the point of the exercise is that the queue's own paging
    terminates: an unbounded loop against a broken cursor hangs the suite instead
    of failing it.
    """
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(200):
        page = _queue(client, review_id, stage_id, limit=limit, cursor=cursor)
        seen.extend(item["review_record_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError(f"the queue never ran out of pages at limit={limit}; collected {len(seen)}")


def _seed(client, *, slug: str, records: list[dict]) -> dict:  # noqa: ANN001
    """A review activated against a locked protocol, imported and sealed."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "queue-paging", "external_key": f"{slug}-{uuid.uuid4()}", "title": "Queue paging"},
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
        content=json.dumps({"records": records}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    corpus = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER], "corpus_version_id": corpus},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    return {"review_id": review_id, "stage_id": stage_id, "corpus_version_id": corpus}


def _merge_every_cluster(client, review_id: str, *, survivor_of) -> int:  # noqa: ANN001
    """Run dedup, merge every cluster, and return how many records were removed.

    `survivor_of` is handed the cluster's member ids and names the one that
    lives. Passing it in rather than always taking `members[0]` is what lets a
    caller place the removals at a chosen position in the queue's order.
    """
    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers())
    assert run.status_code in (200, 201), run.text
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run.json()["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    removed = 0
    for cluster in clusters:
        members = [m["review_record_id"] for m in cluster["members"]]
        survivor = survivor_of(members)
        resolved = client.post(
            f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
            json={"resolution_type": "merge", "survivor_record_id": survivor, "rationale": "same DOI"},
            headers=headers(),
        )
        assert resolved.status_code in (200, 201), resolved.text
        removed += len(members) - 1
    return removed


@pytest.fixture
def six_pairs(client):  # noqa: ANN001, ANN201
    """Twelve records as six duplicate pairs; the queue's FIRST row is removed.

    Deterministic by construction — see the module docstring. Six survivors
    remain, so a queue that pages correctly hands the reviewer six records at
    every page size.
    """
    records = []
    for index in range(6):
        doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:10]}"
        records.append({"title": f"Trial {index}, from PubMed", "year": 2021, "doi": doi})
        records.append({"title": f"Trial {index}, from Embase", "year": 2021, "doi": doi})
    seeded = _seed(client, slug="six-pairs", records=records)

    # BEFORE the merge, so nothing is filtered and this IS the queue's order.
    order = [
        item["review_record_id"] for item in _queue(client, seeded["review_id"], seeded["stage_id"], limit=200)["items"]
    ]
    assert len(order) == 12, order
    position = {record_id: index for index, record_id in enumerate(order)}
    removed = _merge_every_cluster(
        client,
        seeded["review_id"],
        survivor_of=lambda members: max(members, key=lambda m: position[m]),
    )
    assert removed == 6, removed
    seeded["order_before_the_merge"] = order
    seeded["survivors"] = 6
    return seeded


# --------------------------------------------------------------------------


def test_a_removed_record_in_the_lookahead_window_does_not_end_the_queue(client, six_pairs) -> None:
    """THE DEFECT, at its smallest. One page of one, and the cursor is gone.

    `limit=1` fetches two rows; the first is a record the merge removed, so the
    filter leaves one, `1 > 1` is false, and the caller is told there is no next
    page while five of their six assignments have never been served.
    """
    page = _queue(client, six_pairs["review_id"], six_pairs["stage_id"], limit=1)

    assert page["assigned_to_me"] == 6, (
        "the fixture did not leave six live assignments, so this proves nothing: " + json.dumps(page)
    )
    assert page["next_cursor"] is not None, (
        "the queue reported no further page while the reviewer holds "
        f"{page['assigned_to_me']} assignments and has been served {len(page['items'])}. The cursor is "
        "opaque base64 over an internal key, so there is no request the client can make to reach the "
        "rest — and until they are screened, `not_yet_screened` never reaches zero and POST /exports "
        "refuses to seal."
    )
    assert len(page["items"]) == 1, (
        "a page of one served " + str(len(page["items"])) + " records: the removal filter is shrinking the "
        "page the caller asked for rather than the window it was drawn from"
    )


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 7, 11, 50])
def test_paging_the_queue_to_exhaustion_yields_every_assignment(client, six_pairs, limit: int) -> None:
    """THE INVARIANT. Whatever the page size, the reviewer reaches all their work.

    Asserted as a set as well as a count, because a lookahead fixed by
    over-fetching without advancing the cursor correctly would serve the right
    NUMBER of rows by serving one of them twice.
    """
    served = _page_to_exhaustion(client, six_pairs["review_id"], six_pairs["stage_id"], limit=limit)
    assigned = _queue(client, six_pairs["review_id"], six_pairs["stage_id"], limit=1)["assigned_to_me"]

    assert len(served) == len(set(served)), f"the queue served a record twice at limit={limit}: {served}"
    assert len(served) == assigned, (
        f"paging at limit={limit} reached {len(served)} of the reviewer's {assigned} assignments; "
        f"{assigned - len(served)} records are unreachable through the only route that serves them"
    )
    removed = set(six_pairs["order_before_the_merge"]) - set(served)
    assert len(removed) == 6, f"the survivors served do not match the six the merge left: {sorted(removed)}"


def test_a_page_whose_every_row_was_removed_still_advances(client) -> None:
    """The hardest case: the whole lookahead window is merged-away records.

    A fix that merely over-fetches by a fixed margin passes the six-pair fixture
    and fails here. Nine of ten records share one DOI, so at `limit=1` the first
    two rows are removed with probability 8/10 * 7/9 — and the assertion is on
    the exhaustion invariant, which holds however the tiebreak fell.
    """
    doi = f"10.1056/NEJMoa{uuid.uuid4().hex[:10]}"
    records = [{"title": f"The same trial, copy {i}", "year": 2021, "doi": doi} for i in range(9)]
    records.append({"title": "An unrelated study", "year": 2021, "doi": f"10.1001/jama.{uuid.uuid4().hex[:8]}"})
    seeded = _seed(client, slug="one-big-cluster", records=records)
    order = [
        item["review_record_id"] for item in _queue(client, seeded["review_id"], seeded["stage_id"], limit=200)["items"]
    ]
    # The LAST-ordered member survives, so every earlier member of the cluster —
    # and therefore the head of the queue — is a removed record.
    position = {record_id: index for index, record_id in enumerate(order)}
    removed = _merge_every_cluster(
        client, seeded["review_id"], survivor_of=lambda members: max(members, key=lambda m: position[m])
    )
    assert removed == 8, removed

    for limit in (1, 2, 3):
        served = _page_to_exhaustion(client, seeded["review_id"], seeded["stage_id"], limit=limit)
        assert sorted(served) == sorted(set(served)), served
        assert len(served) == 2, (
            f"nine of ten records were merged away and paging at limit={limit} reached {len(served)} of "
            "the two survivors"
        )


def test_a_queue_paged_to_exhaustion_can_reach_its_own_export(client, six_pairs) -> None:
    """THE CONSEQUENCE, end to end. Unreachable work is an unclosable review.

    This is the assertion that makes the defect a blocker rather than a paging
    annoyance: a reviewer who screens everything the queue will give them at a
    small page size must be able to seal. Under the defect the queue stops after
    one record, `not_yet_screened` stays at five, and `POST /exports` refuses
    with a count the lead cannot act on because nothing they can do surfaces the
    remaining records.
    """
    for record_id in _page_to_exhaustion(client, six_pairs["review_id"], six_pairs["stage_id"], limit=2):
        decided = client.post(
            f"/api/reviews/{six_pairs['review_id']}/decisions",
            json={
                "stage_id": six_pairs["stage_id"],
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": "include",
                "rationale": "meets the criteria",
            },
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    flow = client.get(f"/api/reviews/{six_pairs['review_id']}/flow", headers=headers()).json()
    assert flow["counts"]["not_yet_screened"] == 0, (
        "the reviewer screened everything the queue would serve and the ledger still counts unscreened "
        f"records: {flow['counts']}"
    )
    sealed = client.post(f"/api/reviews/{six_pairs['review_id']}/exports", headers=headers())
    assert sealed.status_code == 201, sealed.text
