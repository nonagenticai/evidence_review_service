"""The three screens a demo loads issued one statement set per row.

Measured on `fa03b75`, counted at the DBAPI:

| screen | statements | at |
|---|---|---|
| `GET /reviews` | `1 + 3N` = **151** | the default page of 50 (`le=200` -> 601) |
| `GET /screening/queue` | `2N + 2` = **102** | the default page of 50 (`le=200` -> 402) |
| `GET /conflicts` | `1 + 3C..4C`, **unbounded** | the route had no `limit` at all |

`GET /reviews` is the first screen the studio opens. `GET /screening/queue` is
where a reviewer spends the whole review. `GET /conflicts` is the lead's
adjudication panel, and `C` grows with every disagreement the reviewers have.

## Why the assertion is a pair, and why the second half is the load-bearing one

The apparatus is `test_assignment_throughput.py`'s, for its reasons: the batched
and per-row implementations return byte-identical bodies, so the response cannot
see the difference and a wall-clock test would be measuring the machine.

But an upper bound alone is a weak gate here. `1 + 3N` at N=6 is 19, and a bound
of "at most 25" would pass on the defect at a small fixture size while still
being 601 statements on a real one. So every test below runs TWO page sizes and
asserts the counts are **EQUAL** — the defect is a growth rate, and only
invariance measures a growth rate. The absolute ceiling is kept as well, because
size-invariance alone is satisfied by a route that is uniformly terrible.

And both are worthless if the listener never fired, so each carries the
non-vacuity floor: an empty capture satisfies `<= 4` and satisfies `a == b` twice
over.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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

TENANT = tenant("tenant-read-throughput")
LEAD = "read-throughput-lead"
REVIEWER_A = "read-throughput-a"
REVIEWER_B = "read-throughput-b"

#: Two sizes, both small. The claim is that the count does not TRACK the size,
#: which two points demonstrate as well as one enormous one and in a fraction of
#: the time. At the smaller size the defect's own count is already 3x the bound.
SMALL = 4
LARGE = 16


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@dataclass
class _Capture:
    all_statements: list[str] = field(default_factory=list)
    selects: list[str] = field(default_factory=list)

    def assert_the_read_was_observed(self, context: str) -> None:
        assert self.all_statements, (
            f"{context}: the before_cursor_execute listener recorded NO SQL AT ALL. The hook is on an "
            "engine the request did not use, or the request never reached the database. Every count "
            "below is vacuous until this holds."
        )
        assert self.selects, (
            f"{context}: {len(self.all_statements)} statements were emitted and none was a SELECT. "
            "The filter stopped matching, so the bounds below would pass against an empty list."
        )


@contextmanager
def counting_selects() -> Iterator[_Capture]:
    from sqlalchemy import event

    from database import get_engine

    engine = get_engine()
    capture = _Capture()

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202, ARG001
        capture.all_statements.append(statement)
        if " ".join(statement.split()).upper().startswith("SELECT"):
            capture.selects.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield capture
    finally:
        event.remove(engine, "before_cursor_execute", _record)


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


def _review(client, records: int, reviewers: int = 1, project: str = "read-throughput") -> dict:  # noqa: ANN001
    review_id = client.post(
        "/api/reviews",
        json={"project_id": project, "external_key": f"rt-{uuid.uuid4()}", "title": f"Throughput {records}"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": reviewers,
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
                    {"title": f"Paper {n}", "year": 2000 + n, "doi": f"10.1000/rt{uuid.uuid4()}"}
                    for n in range(records)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    subjects = [REVIEWER_A, REVIEWER_B][:reviewers] or [REVIEWER_A]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": subjects},
        headers=headers(),
    )
    return {"review_id": review_id, "stage_id": stage_id}


# --------------------------------------------------------------------------
# GET /reviews  —  was 1 + 3N
# --------------------------------------------------------------------------

#: MEASURED at 7 on the fixed implementation: the listing, the history tail, the
#: activation tail, the protocol read, and three the request makes before the
#: route body (`load_review`/GUC binding). Set to measured + 1 so one added read
#: is not a failure and a per-row regression is: at N=16 the defect's own count
#: is 49, and at the default page of 50 it is 151.
MAX_LIST_REVIEWS_SELECTS = 8


def test_listing_reviews_does_not_issue_a_statement_set_per_review(client) -> None:
    project = f"list-{uuid.uuid4()}"
    for _ in range(LARGE):
        _review(client, records=1, project=project)

    counts = {}
    for size in (SMALL, LARGE):
        with counting_selects() as capture:
            listed = client.get("/api/reviews", params={"project_id": project, "limit": size}, headers=headers())
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["items"]) == size, listed.json()
        capture.assert_the_read_was_observed(f"GET /reviews limit={size}")
        counts[size] = len(capture.selects)

    assert counts[SMALL] == counts[LARGE], (
        f"the statement count TRACKS the page size: {counts[SMALL]} at {SMALL} reviews, "
        f"{counts[LARGE]} at {LARGE}. That is the 1+3N shape; at the default page of 50 it is 151."
    )
    assert counts[LARGE] <= MAX_LIST_REVIEWS_SELECTS, (
        f"{counts[LARGE]} SELECTs to list {LARGE} reviews (bound {MAX_LIST_REVIEWS_SELECTS}). "
        "Size-invariant but uniformly expensive is still a defect."
    )


# --------------------------------------------------------------------------
# GET /screening/queue  —  was 2N + 2
# --------------------------------------------------------------------------

#: MEASURED at 11. Was 9 (bound 10); the queue now excludes records a merge has
#: already removed, and reading that set costs TWO more — this fixture has
#: duplicate clusters, so `merged_groups` does its full three statements.
#:
#: A review that has never been deduplicated pays ONE, not three: `merged_groups`
#: returns early on an empty cluster list, which is the case worth optimising
#: because this is the reviewer's working screen.
#:
#: Both are CONSTANT, and the size-invariance assertion below is what proves it —
#: that is the assertion that matters. The defect's count at a 16-item page is
#: 34, and at the maximum page of 200 it is 402.
MAX_QUEUE_SELECTS = 12


def test_the_screening_queue_does_not_issue_two_statements_per_item(client) -> None:
    fx = _review(client, records=LARGE)

    counts = {}
    for size in (SMALL, LARGE):
        with counting_selects() as capture:
            queue = client.get(
                f"/api/reviews/{fx['review_id']}/screening/queue",
                params={"stage_id": fx["stage_id"], "limit": size},
                headers=headers(REVIEWER_A, "reviewer"),
            )
        assert queue.status_code == 200, queue.text
        assert len(queue.json()["items"]) == size, queue.json()
        capture.assert_the_read_was_observed(f"GET /screening/queue limit={size}")
        counts[size] = len(capture.selects)

    assert counts[SMALL] == counts[LARGE], (
        f"the statement count TRACKS the page size: {counts[SMALL]} at {SMALL} items, {counts[LARGE]} "
        f"at {LARGE}. That is the 2N+2 shape; at the maximum page of 200 it is 402, on the screen a "
        "reviewer spends the whole review inside."
    )
    assert counts[LARGE] <= MAX_QUEUE_SELECTS, (
        f"{counts[LARGE]} SELECTs for a {LARGE}-item queue page (bound {MAX_QUEUE_SELECTS})"
    )


# --------------------------------------------------------------------------
# GET /conflicts  —  was 1 + 3C..4C, and had no limit
# --------------------------------------------------------------------------

#: MEASURED at 20. Was 11 (bound 12), and 9 before that.
#:
#: The jump to 20 buys ONE definition of "is this conflict outstanding". This
#: route computed `total_open` as `len(all) - len(adjudicated)`, which misses the
#: other way a disagreement ends: when a reviewer supersedes their own decision
#: and the two agree, `flow._unresolved_conflicts` calls it settled, so the export
#: seals while this panel still reported it open. It now borrows that helper, and
#: the helper re-reads the review's conflicts, stage positions, adjudications and
#: votes — nine constant statements.
#:
#: Nine is a real cost and it is worth it: two spellings of one question is the
#: same defect class as the ledger and conflict detection disagreeing about
#: `reviewers_required`, and that one silently exported on two of three required
#: reviewers.
#:
#: Every one of the nine is CONSTANT, and the size-invariance assertion below is
#: what proves that — it is the assertion that matters. The defect's count at 16
#: conflicts is 49, and the route had no `limit` bounding C at all. Raising this
#: number again without the invariance assertion still passing would be hiding a
#: regression, not recording a cost.
#:
#: The batched-adjudication fix in `_unresolved_conflicts` landed here too: it
#: was one statement PER conflict, which took this route to 35 at 16 conflicts —
#: and the export path had been paying that on every seal, unmeasured.
MAX_CONFLICT_SELECTS = 21


def _disagree_on(client, fx: dict, count: int) -> None:  # noqa: ANN001
    queue = client.get(
        f"/api/reviews/{fx['review_id']}/screening/queue",
        params={"stage_id": fx["stage_id"], "limit": 200},
        headers=headers(REVIEWER_A, "reviewer"),
    ).json()["items"]
    for item in queue[:count]:
        client.post(
            f"/api/reviews/{fx['review_id']}/decisions",
            json={"stage_id": fx["stage_id"], "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER_A, "reviewer"),
        )
        client.post(
            f"/api/reviews/{fx['review_id']}/decisions",
            json={
                "stage_id": fx["stage_id"],
                "review_record_id": item["review_record_id"],
                "disposition": "exclude",
                "exclusion_reason_code": "wrong_design",
            },
            headers=headers(REVIEWER_B, "reviewer"),
        )


def test_listing_conflicts_does_not_issue_a_statement_set_per_conflict(client) -> None:
    fx = _review(client, records=LARGE, reviewers=2)
    _disagree_on(client, fx, LARGE)

    counts = {}
    for size in (SMALL, LARGE):
        with counting_selects() as capture:
            listed = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": size}, headers=headers())
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["items"]) == size, listed.json()
        capture.assert_the_read_was_observed(f"GET /conflicts limit={size}")
        counts[size] = len(capture.selects)

    assert counts[SMALL] == counts[LARGE], (
        f"the statement count TRACKS the conflict count: {counts[SMALL]} at {SMALL}, {counts[LARGE]} "
        f"at {LARGE}. That is the 1+3C shape, and this route had no `limit` at all, so C is bounded "
        "only by how much the reviewers disagreed."
    )
    assert counts[LARGE] <= MAX_CONFLICT_SELECTS, (
        f"{counts[LARGE]} SELECTs for {LARGE} conflicts (bound {MAX_CONFLICT_SELECTS})"
    )


def test_the_conflicts_route_has_a_limit_at_all(client) -> None:
    """It had none. An unbounded response on a quantity the reviewers control."""
    fx = _review(client, records=LARGE, reviewers=2)
    _disagree_on(client, fx, LARGE)

    listed = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 3}, headers=headers())
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 3, listed.json()

    refused = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 0}, headers=headers())
    assert refused.status_code == 422, refused.text


def test_a_truncated_conflict_list_says_so(client) -> None:
    """The `limit` shipped without any way to see that it had bitten.

    `13b315f` added `limit` to an unbounded route — right — and returned only
    `items`/`count`/`open`, so a full page is indistinguishable from a complete
    list. Measured on six conflicts at `limit=2`: the panel reads
    `{"count": 2, "open": 2}`, the lead resolves those two, the panel reads
    `{"open": 0}`, and `POST /exports` refuses with `422` naming unresolved
    conflicts — because `_unresolved_conflicts` is unbounded, so the export gate
    was right and the panel was lying with nothing on screen to explain it.

    That is the same silent-truncation class `925cacf` removed from the export,
    reintroduced one route over, by me.
    """
    fx = _review(client, records=LARGE, reviewers=2)
    _disagree_on(client, fx, LARGE)

    truncated = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 2}, headers=headers()).json()
    assert len(truncated["items"]) == 2
    assert truncated["has_more"] is True, (
        "a truncated conflict list does not say it was truncated, so a full page reads as the "
        f"whole review: {truncated!r}"
    )
    assert truncated["total"] == LARGE, (
        f"`total` reports {truncated['total']!r} for {LARGE} conflicts — it is describing the page"
    )
    assert truncated["total_open"] == LARGE, (
        "`total_open` is what the export gate turns on; a page-scoped number would tell a lead "
        f"there is nothing left to adjudicate: {truncated!r}"
    )

    whole = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 200}, headers=headers()).json()
    assert whole["has_more"] is False, (
        "an untruncated list claims there is more, which would send a client paginating forever"
    )
    assert whole["total"] == whole["count"] == LARGE


def test_the_conflict_totals_are_review_wide_not_page_wide(client) -> None:
    """`total_open` must fall as conflicts are adjudicated, from any page size.

    Without this, hard-coding `total_open = total` satisfies the test above.
    """
    fx = _review(client, records=LARGE, reviewers=2)
    _disagree_on(client, fx, LARGE)

    page = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 3}, headers=headers()).json()
    for conflict in page["items"]:
        client.post(
            f"/api/reviews/{fx['review_id']}/conflicts/{conflict['conflict_id']}:unblind",
            json={"reason": "adjudicating"},
            headers=headers(),
        )
        resolved = client.post(
            f"/api/reviews/{fx['review_id']}/conflicts/{conflict['conflict_id']}:resolve",
            json={"disposition": "include", "rationale": "eligible"},
            headers=headers(),
        )
        assert resolved.status_code in (200, 201), resolved.text

    after = client.get(f"/api/reviews/{fx['review_id']}/conflicts", params={"limit": 3}, headers=headers()).json()
    assert after["total_open"] == LARGE - 3, (
        f"`total_open` is {after['total_open']!r} after adjudicating 3 of {LARGE}; it is not tracking the review"
    )
    assert after["total"] == LARGE, "`total` moved when nothing was deleted"


# --------------------------------------------------------------------------
# POST /exports  —  `_collect` ran twice
# --------------------------------------------------------------------------


def test_the_export_collects_the_flow_inputs_once(client) -> None:
    """`create_export` called `_collect` and `current_flow_revision` twice each.

    `_collect` is nested — 2 statements per duplicate cluster, 1 per screening
    conflict, on a fixed base — so doubling it doubled a superlinear read against
    tables that cannot change inside one transaction.

    Asserted by counting the ONE statement `_collect` always issues exactly once
    per call: the `SELECT import_rows.status` that opens it. Two of them means
    two collections. This is a per-call marker rather than a total, so it stays
    honest when the rest of the export changes shape.
    """
    fx = _review(client, records=SMALL)
    queue = client.get(
        f"/api/reviews/{fx['review_id']}/screening/queue",
        params={"stage_id": fx["stage_id"], "limit": 200},
        headers=headers(REVIEWER_A, "reviewer"),
    ).json()["items"]
    for item in queue:
        client.post(
            f"/api/reviews/{fx['review_id']}/decisions",
            json={"stage_id": fx["stage_id"], "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER_A, "reviewer"),
        )

    with counting_selects() as capture:
        created = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers())
    assert created.status_code == 201, created.text
    capture.assert_the_read_was_observed("POST /exports")

    # The EXACT opening statement of `_collect` — `select(ImportRow.status)` with
    # only the `review_id` predicate. A looser `FROM import_rows` filter also
    # matched the quarantine read further down the same function, which carries
    # `AND status = ...`, and counted one call as two.
    collects = [
        s
        for s in capture.selects
        if " ".join(s.split()).upper().startswith("SELECT IMPORT_ROWS.STATUS FROM IMPORT_ROWS WHERE")
        and "STATUS = " not in " ".join(s.split()).upper()
    ]
    assert collects, (
        "the `SELECT import_rows.status` that opens `_collect` was not observed at all. The marker "
        "this test counts no longer matches, so the assertion below is vacuous."
    )
    assert len(collects) == 1, (
        f"`_collect` ran {len(collects)} times in one export. It reads the same tables in the same "
        "transaction each time, and it is nested per cluster and per conflict."
    )


# --------------------------------------------------------------------------
# GET /flow  —  `_collect` was 2 statements per cluster and 1 per conflict
# --------------------------------------------------------------------------

#: MEASURED on the flattened implementation. Set to measured + 2, and the
#: size-invariance assertions below are what actually guard the growth rate.
#:
#: ⚠️ RAISED 24 -> 25 on 2026-09-06 for `require_no_owed_decisions`, the guard
#: that stopped `GET /flow` handing a reviewer the disposition they are blinded
#: from. It is ONE statement per read and does not depend on the corpus, which
#: is why the `counts["small"] == counts["large"]` assertions above still hold
#: — and those, not this ceiling, are what would catch a per-cluster
#: regression. A ceiling raised without that invariance check would be a
#: ratchet loosened to fit whatever the code now does.
MAX_FLOW_SELECTS = 25


def _cluster_corpus(client, records: int, project: str) -> dict:  # noqa: ANN001
    """A review whose corpus is `records` DUPLICATE PAIRS, so it has `records` clusters."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": project, "external_key": f"flow-{uuid.uuid4()}", "title": f"Flow {records}"},
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
    rows = []
    for n in range(records):
        doi = f"10.5000/pair{uuid.uuid4()}"
        rows.append({"title": f"Paper {n} a", "year": 2000 + n, "doi": doi})
        rows.append({"title": f"Paper {n} b", "year": 2000 + n, "doi": doi})
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": rows}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers())
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run.json()["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == records, f"expected {records} clusters, got {len(clusters)}"
    # Resolve every one as a merge, which is what makes `_collect`'s cluster loop
    # do work rather than `continue` past it on the first line.
    for cluster in clusters:
        survivor = cluster["members"][0]["review_record_id"]
        resolved = client.post(
            f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
            json={"resolution_type": "merge", "survivor_record_id": survivor, "rationale": "same DOI"},
            headers=headers(),
        )
        assert resolved.status_code in (200, 201), resolved.text
    return {"review_id": review_id, "clusters": len(clusters)}


def test_the_flow_ledger_does_not_issue_two_statements_per_duplicate_cluster(client) -> None:
    """`_collect` ran `current_resolution` + a members read for EVERY cluster.

    It is reached from the plain `GET /api/reviews/{id}/flow`, and twice more
    inside `rebuild_flow`, so `2 x clusters` was paid on every read of a response
    that is a handful of integers. On a review with 300 duplicate clusters that
    is 600 statements before anything else.

    Two sizes, and the counts must be EQUAL. A ceiling alone would pass on the
    defect at a small fixture size while still being 600 statements on a real
    corpus — the defect is a growth rate.
    """
    small = _cluster_corpus(client, SMALL, f"flow-{uuid.uuid4()}")
    large = _cluster_corpus(client, LARGE, f"flow-{uuid.uuid4()}")

    counts = {}
    for name, fx in (("small", small), ("large", large)):
        with counting_selects() as capture:
            flow = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers())
        assert flow.status_code == 200, flow.text
        capture.assert_the_read_was_observed(f"GET /flow ({name}, {fx['clusters']} clusters)")
        counts[name] = len(capture.selects)

    assert counts["small"] == counts["large"], (
        f"the flow ledger's statement count TRACKS the cluster count: {counts['small']} at "
        f"{SMALL} clusters, {counts['large']} at {LARGE}. That is the 2-per-cluster shape."
    )
    assert counts["large"] <= MAX_FLOW_SELECTS, (
        f"{counts['large']} SELECTs for a flow read over {LARGE} clusters (bound {MAX_FLOW_SELECTS})"
    )


def test_the_flow_ledger_does_not_issue_a_statement_per_conflict(client) -> None:
    """The second loop: one adjudication fetch per conflict, on every flow read."""
    small = _review(client, records=SMALL, reviewers=2, project=f"flowc-{uuid.uuid4()}")
    _disagree_on(client, small, SMALL)
    large = _review(client, records=LARGE, reviewers=2, project=f"flowc-{uuid.uuid4()}")
    _disagree_on(client, large, LARGE)

    counts = {}
    for name, fx in (("small", small), ("large", large)):
        with counting_selects() as capture:
            flow = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers())
        assert flow.status_code == 200, flow.text
        capture.assert_the_read_was_observed(f"GET /flow ({name})")
        counts[name] = len(capture.selects)

    assert counts["small"] == counts["large"], (
        f"the flow ledger's statement count TRACKS the conflict count: {counts['small']} at "
        f"{SMALL} conflicts, {counts['large']} at {LARGE}."
    )
    assert counts["large"] <= MAX_FLOW_SELECTS, (
        f"{counts['large']} SELECTs for a flow read over {LARGE} conflicts (bound {MAX_FLOW_SELECTS})"
    )
