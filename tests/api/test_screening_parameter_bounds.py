"""Three unbounded parameters on the screening router, and what each one cost.

Measured on `4ea77a0`, all three against a live database:

1. **`round_number` had no ceiling and the column is `int4`.**
   `round_number=2147483648` returned **HTTP 409** carrying `"detail": "integer
   out of range"` — raw Postgres text — on `POST /decisions`, on
   `POST /assignments`, and on the SAFE method `GET /screening/queue`. That is
   the `_integrity_handler` pass-through, and `app.py` documents exactly what it
   promises to pass through: "those are our own `RAISE EXCEPTION` strings,
   written to be read by whoever tripped them". `integer out of range` is
   neither ours nor readable, and 409 tells a client the request conflicts with
   the state of the review when in fact it can never be satisfied by any state.
   A rejected literal belongs at the edge, as a 422.

2. **`reviewer_subject_ids` had `min_length=1` and nothing else.**
   `POST /assignments` with **10,000** reviewer ids against a **3**-record corpus
   returned **201**, having synchronously written **30,000** rows. The route
   assigns every corpus member to every named reviewer by design — see
   `screening.create_assignments`, which reasons about a realistic first pass of
   "3,000-8,000 records at two or three reviewers" and batches the insert for
   exactly that shape. The batching bounds the STATEMENT count; nothing bounded
   the ROW count, and rows scale as `records x reviewers` with the multiplier
   supplied by the caller. A **100,000-character** reviewer id was accepted and
   stored too — the column is `Text`.

3. **`GET /decisions` had no pagination at all.**
   `?limit=1` returned `count=10`: the parameter does not exist and was silently
   ignored, which is worse than a large page because a client that believes it
   asked for one row gets the review. Every sibling list route is bounded —
   `/records` 422s above 500, `/conflicts` above 500 — and both carry a `total`
   so a caller can tell a full page from a truncated one. On a real dual review
   this route is `records x reviewers` rows in one response.

## Why these assert FastAPI's native 422 and not problem+json

`/records?limit=501` answers `422 application/json` with FastAPI's
`RequestValidationError` body, not `application/problem+json` — checked, not
assumed. These parameters are declared the same way, so they refuse the same way,
and asserting the sibling's actual shape is what keeps the router uniform. The
typed problem+json path is for refusals this service raises itself.
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

TENANT = tenant("tenant-screening-bounds")
LEAD = "screening-bounds-lead"
REVIEWER = "screening-bounds-reviewer"

CORPUS = 3

#: One past `int4`. The exact literal that produced `409 integer out of range`.
INT4_OVERFLOW = 2_147_483_648


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def _assert_edge_refusal(response, field: str) -> None:
    """422, FastAPI's validation shape, and the field NAMED.

    The status alone is not the assertion. A 409 and a 422 are both "not 2xx",
    and a 422 raised somewhere else in the request for some other reason would
    satisfy a status-only check while the bound being tested did not exist — so
    the failing location is read out of the body and matched against the
    parameter this test is about.
    """
    assert response.status_code == 422, (
        f"expected a 422 at the edge, got {response.status_code} with body {response.text[:300]!r}"
    )
    body = response.json()
    assert isinstance(body.get("detail"), list), f"not FastAPI's validation shape: {response.text[:300]!r}"
    located = {part for error in body["detail"] for part in error.get("loc", [])}
    assert field in located, f"the refusal does not name {field!r}; it located {sorted(map(str, located))}"


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
def review(client) -> dict[str, str]:  # noqa: ANN001
    """An activated review with a sealed corpus and one stage.

    The corpus matters for the `round_number` tests as much as for the
    assignment ones: `create_assignments` reads the corpus before it inserts, so
    a fixture without one refuses for the wrong reason and the overflow is never
    reached.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "bounds", "external_key": f"scr-bounds-{uuid.uuid4()}", "title": "Screening bounds"},
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
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Bound paper {n}", "year": 2020 + n, "doi": f"10.1000/b.{uuid.uuid4()}"}
                    for n in range(CORPUS)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert assigned.status_code == 201, assigned.text
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()
    assert len(queue["items"]) == CORPUS, queue
    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "record_ids": [item["review_record_id"] for item in queue["items"]],
    }


# ---------------------------------------------------------------------------
# DEFECT 2 — an int4 column behind an unbounded int
# ---------------------------------------------------------------------------


def test_an_overflowing_round_number_is_refused_at_the_edge_on_decisions(client, review) -> None:
    """`409 integer out of range` on `4ea77a0`."""
    response = client.post(
        f"/api/reviews/{review['review_id']}/decisions",
        json={
            "stage_id": review["stage_id"],
            "round_number": INT4_OVERFLOW,
            "review_record_id": review["record_ids"][0],
            "disposition": "include",
        },
        headers=headers(REVIEWER, "reviewer"),
    )
    _assert_edge_refusal(response, "round_number")


def test_an_overflowing_round_number_is_refused_at_the_edge_on_assignments(client, review) -> None:
    """`409 integer out of range` on `4ea77a0`."""
    response = client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={
            "stage_id": review["stage_id"],
            "round_number": INT4_OVERFLOW,
            "reviewer_subject_ids": [REVIEWER],
        },
        headers=headers(),
    )
    _assert_edge_refusal(response, "round_number")


def test_an_overflowing_round_number_is_refused_at_the_edge_on_the_queue(client, review) -> None:
    """The one that matters most: a SAFE method returning a 409 from the database.

    `GET` is the method a client retries, a proxy caches and a dashboard polls.
    Answering it with a state conflict carrying raw driver text is the clearest
    contradiction of `_integrity_handler`'s stated contract in the service.
    """
    response = client.get(
        f"/api/reviews/{review['review_id']}/screening/queue",
        params={"stage_id": review["stage_id"], "round_number": INT4_OVERFLOW},
        headers=headers(REVIEWER, "reviewer"),
    )
    _assert_edge_refusal(response, "round_number")


def test_no_screening_route_leaks_raw_driver_text_for_an_overflowing_round(client, review) -> None:
    """The property behind the three tests above, asserted as one.

    They pin the status; this pins what must NEVER be in the body. A future
    change that produced a 422 from the database layer instead of the edge would
    pass all three and still put `integer out of range` on the wire.
    """
    responses = [
        client.get(
            f"/api/reviews/{review['review_id']}/screening/queue",
            params={"stage_id": review["stage_id"], "round_number": INT4_OVERFLOW},
            headers=headers(REVIEWER, "reviewer"),
        ),
        client.post(
            f"/api/reviews/{review['review_id']}/assignments",
            json={
                "stage_id": review["stage_id"],
                "round_number": INT4_OVERFLOW,
                "reviewer_subject_ids": [REVIEWER],
            },
            headers=headers(),
        ),
    ]
    for response in responses:
        assert "integer out of range" not in response.text, (
            f"raw Postgres text reached the caller: {response.status_code} {response.text[:300]!r}"
        )


def test_a_second_screening_round_is_still_accepted(client, review) -> None:
    """The non-regression floor. A bound that refuses real traffic is not a fix.

    Round 2 is the ordinary case — a pilot/calibration pass followed by the real
    one, or a re-screen after a protocol amendment — and it must still work.
    """
    response = client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={"stage_id": review["stage_id"], "round_number": 2, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# DEFECT 3 — an unbounded multiplier on a `records x reviewers` write
# ---------------------------------------------------------------------------


def test_a_reviewer_list_past_the_bound_is_refused_before_any_row_is_written(client, review) -> None:
    """201 and 30,000 rows on `4ea77a0`, from a 3-record corpus.

    Asserted just past the bound rather than at the measured 10,000, because the
    bound is what is on trial and a test that has to write tens of thousands of
    rows to fail is a test nobody will run. The 10,000-id measurement is recorded
    in this module's docstring.
    """
    from api.routers.screening import MAX_REVIEWERS_PER_ASSIGNMENT

    response = client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={
            "stage_id": review["stage_id"],
            "reviewer_subject_ids": [f"reviewer-{n}" for n in range(MAX_REVIEWERS_PER_ASSIGNMENT + 1)],
        },
        headers=headers(),
    )
    _assert_edge_refusal(response, "reviewer_subject_ids")


def test_a_reviewer_id_past_the_bound_is_refused_before_it_is_stored(client, review) -> None:
    """A 100,000-character subject id was accepted and persisted on `4ea77a0`."""
    from api.routers.screening import MAX_SUBJECT_ID_LENGTH

    response = client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={
            "stage_id": review["stage_id"],
            "reviewer_subject_ids": ["r" * (MAX_SUBJECT_ID_LENGTH + 1)],
        },
        headers=headers(),
    )
    _assert_edge_refusal(response, "reviewer_subject_ids")


def test_a_panel_at_the_bound_is_still_accepted(client, review) -> None:
    """The non-regression floor for the multiplier.

    A full panel screening the same records — the agreement-calibration exercise
    that is the only real reason to name more than three — must still be
    assignable, and a Keycloak subject id must fit.
    """
    from api.routers.screening import MAX_REVIEWERS_PER_ASSIGNMENT

    response = client.post(
        f"/api/reviews/{review['review_id']}/assignments",
        json={
            "stage_id": review["stage_id"],
            "round_number": 3,
            "reviewer_subject_ids": [str(uuid.uuid4()) for _ in range(MAX_REVIEWERS_PER_ASSIGNMENT)],
        },
        headers=headers(),
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# DEFECT 4 — a list route with no page
# ---------------------------------------------------------------------------


def test_the_decisions_route_honours_limit_and_says_it_truncated(client, review) -> None:
    """`?limit=1` returned `count=10` on `4ea77a0` — the parameter did not exist.

    The counters are asserted together on purpose. `count == 1` alone would pass
    against a route that silently dropped everything past the first row, which is
    the failure `/conflicts` documents from the other side: a panel that read
    `{"open": 0}` off a truncated page while the export gate disagreed.
    """
    for record_id in review["record_ids"]:
        client.post(
            f"/api/reviews/{review['review_id']}/decisions",
            json={"stage_id": review["stage_id"], "review_record_id": record_id, "disposition": "include"},
            headers=headers(REVIEWER, "reviewer"),
        )

    everything = client.get(f"/api/reviews/{review['review_id']}/decisions", headers=headers()).json()
    assert everything["count"] == CORPUS, everything
    assert everything["total"] == CORPUS, everything
    assert everything["has_more"] is False, everything

    page = client.get(f"/api/reviews/{review['review_id']}/decisions", params={"limit": 1}, headers=headers()).json()
    assert len(page["items"]) == 1, page
    assert page["count"] == 1, page
    assert page["total"] == CORPUS, f"total must describe the whole visible population, not the page: {page}"
    assert page["has_more"] is True, f"a truncated page must say so: {page}"


def test_the_decisions_route_pages_with_offset_like_its_sibling(client, review) -> None:
    """`/records` is the shape being matched: `limit` + `offset`, and `total` beside them.

    Every row must be reachable, exactly once, by walking the offsets — a `limit`
    without a working `offset` is a route that can only ever serve its first page.
    """
    walked: list[str] = []
    for offset in range(0, CORPUS):
        page = client.get(
            f"/api/reviews/{review['review_id']}/decisions",
            params={"limit": 1, "offset": offset},
            headers=headers(),
        ).json()
        assert page["offset"] == offset, page
        walked.extend(item["decision_id"] for item in page["items"])

    assert len(walked) == CORPUS, walked
    assert len(set(walked)) == CORPUS, f"the pages overlap: {walked}"


def test_the_decisions_limit_is_bounded_like_its_siblings(client, review) -> None:
    """`/records` and `/conflicts` both 422 above 500. An uncapped `limit` is not pagination."""
    response = client.get(f"/api/reviews/{review['review_id']}/decisions", params={"limit": 501}, headers=headers())
    _assert_edge_refusal(response, "limit")
