"""`POST /assignments` must not issue one statement per record per reviewer.

`create_assignments` inserted with a nested loop: one `INSERT ... ON CONFLICT DO
NOTHING RETURNING` per (record, reviewer) pair. Measured on `393ae61`, 200
records and two reviewers produced **400** `INSERT INTO screening_assignments`
statements, counted at the DBAPI.

That scales as `records x reviewers`. A realistic systematic-review first pass is
3,000-8,000 records at two or three reviewers — 6,000 to 24,000 round trips,
serially, inside one transaction, through PgBouncer, on a synchronous route with
no job behind it. The gateway gives up long before the transaction does, and the
caller cannot distinguish that timeout from a failure: the work is still running,
and retrying is safe only because of `uq_assignment_once`.

## Why this is asserted at the DBAPI and not on the response

The two implementations return byte-identical bodies. `assignments_created`,
`records` and `reviewers` are the same either way; the only difference is how
many times the process went to the database. So a response-level test cannot see
this defect at all, and a wall-clock test would be measuring the machine. A
`before_cursor_execute` listener counts exactly the thing that changed.

The corpus here is 200 records rather than 8,000 on purpose. The assertion is
that the count does not TRACK the corpus, which two sizes demonstrate as well as
one enormous one does and in a fraction of the time — the defect is a growth
rate, not a threshold.
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

TENANT = tenant("tenant-throughput")
LEAD = "throughput-lead"

#: Big enough that 1-per-pair is unmistakable (400 statements vs 1), small
#: enough that the fixture imports in well under a second.
SMALL_CORPUS = 60
LARGE_CORPUS = 200

#: `ceil(rows / ASSIGNMENT_INSERT_CHUNK)` for every size used here is 1. The
#: allowance of one more is for a future chunk-size change, not for slop: at
#: 400 rows the defect's count is 400, so nothing about this bound is marginal.
MAX_INSERT_STATEMENTS = 2


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


@dataclass
class _Capture:
    """What the DBAPI actually emitted inside a `counting_assignment_inserts` block.

    Two lists rather than one, because "nothing was counted" has two entirely
    different causes and the failure message has to say which. An empty
    `all_statements` means the listener never fired — it is on the wrong engine,
    or the block never reached the database at all. An empty `inserts` with a
    populated `all_statements` means the listener fired and the FILTER stopped
    matching, which is the more insidious of the two because everything looks
    wired up.
    """

    all_statements: list[str] = field(default_factory=list)
    inserts: list[str] = field(default_factory=list)

    def assert_the_insert_was_observed(self, context: str) -> None:
        """The non-vacuity floor. Call it before asserting any bound on the count.

        An assertion that passes on zero observations is not a measurement.
        Every bound in this file is either an upper bound or an equality against
        another count, and an empty capture satisfies all of them: `0 <= 2`
        holds, and `0 == 0` holds twice as convincingly. So the moment this
        listener stops seeing the thing it counts — the emitted SQL changes shape
        and `INSERT INTO SCREENING_ASSIGNMENTS` no longer prefixes it, the
        `get_engine` singleton is rebuilt so the hook lands on an engine the
        route does not use, or the route stops writing at all — every test below
        reports green while measuring nothing, and the 400-statement defect this
        file exists to pin could come back underneath it unobserved.

        "0 statements, none of them too many" certifies exactly as much as
        "0 unhealthy out of 0 examined": nothing. Both are what a broken selector
        produces, and neither is distinguishable from success unless something
        asserts that the selector selected.

        This is deliberately a floor on the CAPTURE and not on the response. The
        response cannot see it — that is the premise of the whole file: the
        batched and the one-per-pair implementations return byte-identical
        bodies. Only the listener knows, so only the listener can be checked for
        having been awake.
        """
        assert self.all_statements, (
            f"{context}: the before_cursor_execute listener recorded no SQL AT ALL. That is a "
            "broken capture, not a fast route — the hook is attached to an engine the request "
            "did not use, or the request never reached the database. Every count taken here is "
            "vacuous until this holds."
        )
        assert self.inserts, (
            f"{context}: {len(self.all_statements)} statements were emitted inside the block and "
            "NOT ONE matched `INSERT INTO SCREENING_ASSIGNMENTS`. The filter no longer recognises "
            "the assignment insert, so the bounds below would pass against an empty list while the "
            "route did whatever it liked."
        )


@contextmanager
def counting_assignment_inserts() -> Iterator[_Capture]:
    """Every `INSERT INTO screening_assignments` issued inside the block.

    Attached to the engine rather than to a session, because the route opens its
    own `actor_scope` and there is no session to hand it. `get_engine` is a
    module-level singleton, which is what makes that reachable from a test.

    Everything else the engine emits is kept too, and unfiltered, purely so that
    :meth:`_Capture.assert_the_insert_was_observed` can tell a detached listener
    apart from a filter that stopped matching.
    """
    from sqlalchemy import event

    from database import get_engine

    engine = get_engine()
    capture = _Capture()

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202, ARG001
        capture.all_statements.append(statement)
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith("INSERT INTO SCREENING_ASSIGNMENTS"):
            capture.inserts.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield capture
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def _review_with_a_sealed_corpus(client, record_count: int) -> dict[str, str]:
    """An active review with `record_count` distinct records sealed into it."""
    created = client.post(
        "/api/reviews",
        json={
            "project_id": "throughput",
            "external_key": f"throughput-{uuid.uuid4()}",
            "title": f"Throughput {record_count}",
        },
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]
    assert (
        client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers()).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol_id, "rationale": "throughput fixture"},
            headers=headers(),
        ).status_code
        == 200
    )

    marker = uuid.uuid4().hex[:8]
    corpus = {
        "records": [
            {"title": f"Study {marker}-{i}", "doi": f"10.1000/{marker}-{i}", "year": 2020} for i in range(record_count)
        ]
    }
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(corpus),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code == 201

    stages = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()
    return {"review_id": review_id, "stage_id": stages["items"][0]["stage_id"]}


def _assign(client, review: dict[str, str], reviewers: list[str]):  # noqa: ANN202
    return client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={"stage_id": review["stage_id"], "round_number": 1, "reviewer_subject_ids": reviewers},
        headers=headers(),
    )


def test_the_assignment_insert_does_not_scale_with_the_corpus(client) -> None:
    """The defect, at the size where it is already 400 statements.

    `assignments_created` is asserted alongside the statement count, because a
    "fix" that inserted fewer rows would also reduce the statement count and
    would pass a count-only assertion.
    """
    review = _review_with_a_sealed_corpus(client, LARGE_CORPUS)

    with counting_assignment_inserts() as capture:
        response = _assign(client, review, ["throughput-rev-a", "throughput-rev-b"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["records"] == LARGE_CORPUS
    assert body["assignments_created"] == LARGE_CORPUS * 2, "rows were lost, not batched"
    capture.assert_the_insert_was_observed(f"{LARGE_CORPUS} records x 2 reviewers")
    assert len(capture.inserts) <= MAX_INSERT_STATEMENTS, (
        f"{len(capture.inserts)} INSERT statements for {LARGE_CORPUS} records x 2 reviewers "
        f"({LARGE_CORPUS * 2} rows); the insert is still one statement per pair"
    )


def test_the_statement_count_is_the_same_for_a_corpus_three_times_the_size(client) -> None:
    """The growth rate, which is the actual defect.

    A single-size assertion could be satisfied by a chunk that happens to fit.
    Two sizes with an identical count is what says the statement count is a
    function of the chunk size and not of the corpus.

    This is the assertion in the file with the least tolerance for a broken
    capture, and the reason the floor exists. `len(a) == len(b)` is not an upper
    bound that an empty capture merely slips under — it is an equality that an
    empty capture satisfies EXACTLY, on both sides, and reports as the strongest
    possible evidence of the property. `0 == 0` is what a listener that sees
    nothing produces, and it is indistinguishable from a perfect result.
    """
    small = _review_with_a_sealed_corpus(client, SMALL_CORPUS)
    large = _review_with_a_sealed_corpus(client, LARGE_CORPUS)
    reviewers = ["throughput-rev-c", "throughput-rev-d", "throughput-rev-e"]

    with counting_assignment_inserts() as small_capture:
        small_response = _assign(client, small, reviewers)
    with counting_assignment_inserts() as large_capture:
        large_response = _assign(client, large, reviewers)

    assert small_response.json()["assignments_created"] == SMALL_CORPUS * 3
    assert large_response.json()["assignments_created"] == LARGE_CORPUS * 3
    # Both sides, separately: an equality is only evidence if each half is a
    # measurement in its own right.
    small_capture.assert_the_insert_was_observed(f"{SMALL_CORPUS} records x 3 reviewers")
    large_capture.assert_the_insert_was_observed(f"{LARGE_CORPUS} records x 3 reviewers")
    assert len(small_capture.inserts) == len(large_capture.inserts), (
        f"{len(small_capture.inserts)} statements for {SMALL_CORPUS} records and "
        f"{len(large_capture.inserts)} for {LARGE_CORPUS}; the count tracks the corpus"
    )


def test_a_retried_assignment_is_still_a_no_op_through_the_batched_path(client) -> None:
    """`ON CONFLICT DO NOTHING` retry-safety, exercised through the new statement.

    This is the property the per-row `RETURNING` was there to count correctly —
    psycopg reports `-1` for `rowcount` on a conflicting insert, which once
    produced a negative `assignments_created`. `len(result.all())` has to be the
    faithful multi-row form of it, and a batch that conflicts ENTIRELY is where
    that is easiest to get wrong.
    """
    review = _review_with_a_sealed_corpus(client, SMALL_CORPUS)
    reviewers = ["throughput-rev-f", "throughput-rev-g"]

    first = _assign(client, review, reviewers)
    assert first.status_code == 201, first.text
    assert first.json()["assignments_created"] == SMALL_CORPUS * 2

    with counting_assignment_inserts() as capture:
        again = _assign(client, review, reviewers)

    assert again.status_code == 201, again.text
    assert again.json()["assignments_created"] == 0, "a retried assignment created rows a second time"
    assert again.json()["records"] == SMALL_CORPUS
    # The floor carries more weight here than anywhere else, because this is the
    # one test whose headline assertion is an expected ZERO. `assignments_created
    # == 0` is exactly what a route that quietly stopped issuing the insert would
    # also return, and `len(inserts) <= 2` would wave that through. Requiring the
    # statement to have been OBSERVED is what separates "the batch ran and every
    # row conflicted" — the property under test — from "no batch ran".
    capture.assert_the_insert_was_observed(f"a full-conflict retry of {SMALL_CORPUS} records x 2 reviewers")
    assert len(capture.inserts) <= MAX_INSERT_STATEMENTS

    # And a reviewer added to an existing round is a partial conflict: the two
    # existing reviewers contribute nothing, the new one contributes every
    # record. Full-conflict and no-conflict batches both work above; this is the
    # mixed case, where an off-by-one in the RETURNING count would show.
    widened = _assign(client, review, [*reviewers, "throughput-rev-h"])
    assert widened.json()["assignments_created"] == SMALL_CORPUS
