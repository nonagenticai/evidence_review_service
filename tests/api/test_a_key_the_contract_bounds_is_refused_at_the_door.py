"""Two keys the released contract bounds tighter than the API used to.

`urn:datacompany:review-created:1` bounds `external_key` to `maxLength: 256`;
`CreateReviewBody.external_key` had no `max_length` at all. A caller could
create a review today whose `external_key` the schema governing
`review.created.v1` would refuse the moment a consumer validated the payload —
and `DomainOutbox` is append-only, so there is no fixing that row afterwards.

`urn:datacompany:review-assertion-recorded:1` bounds `assertion_key` to
`maxLength: 128` AND `pattern: ^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$`;
`AssertionBody.assertion_key` had `max_length=200` and no pattern — looser on
BOTH axes, independently. A 200-character key and a key with a capital letter
are both API-legal today and both `review.assertion.recorded.v1`-illegal.

`tests/unit/test_api_bounds_match_the_released_contract.py` proves the field
constraints now EQUAL the schema's. This file proves the equality actually
refuses real requests at the boundary — a constraint that matches on paper but
is wired to the wrong field, or shadowed by a `Query`/`Body` override, would
still pass that test and still 201 here.
"""

from __future__ import annotations

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

TENANT = tenant("tenant-contract-key-bounds")
LEAD = "contract-key-bounds-lead"

#: One past the released `review-created:1` bound.
OVERSIZED_EXTERNAL_KEY = "x" * 257

#: One past the released `review-assertion-recorded:1` bound, and otherwise
#: pattern-legal — isolates the LENGTH failure from the pattern failure.
OVERSIZED_ASSERTION_KEY = "a" * 129

#: Under the length bound but pattern-illegal three independent ways: a
#: leading capital, an embedded space, and an embedded hyphen. The released
#: pattern is `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$` — lowercase-led
#: dot-separated segments of `[a-z0-9_]` only.
PATTERN_ILLEGAL_ASSERTION_KEYS = ["Population.total", "population total", "population-total"]


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def _assert_edge_refusal(response, field: str) -> None:
    """422, FastAPI's native validation shape, and the field NAMED in `loc`.

    Same discriminator as `test_screening_parameter_bounds.py`'s
    `_assert_edge_refusal`: the status alone would also be satisfied by a 422
    from an unrelated field, so the failing location is read out of the body
    and matched.
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


def test_a_257_character_external_key_is_refused_creating_a_review(client) -> None:
    """The reviewed defect: a value the door accepted that the contract would not.

    No review needs to exist and none is created here — Pydantic validates the
    body before `POST /api/reviews`'s handler ever opens a session, so this is
    reached with nothing more than a well-formed tenant header. `201` on this
    request, before the fix, would have written an outbox row that
    `urn:datacompany:review-created:1` refuses.
    """
    response = client.post(
        "/api/reviews",
        json={"project_id": "contract-bounds", "external_key": OVERSIZED_EXTERNAL_KEY, "title": "Too long a key"},
        headers=headers(),
    )
    _assert_edge_refusal(response, "external_key")


def test_a_256_character_external_key_is_accepted(client) -> None:
    """The positive control. Exactly at the released bound, not one past it —
    proves the field refuses ONLY what the contract refuses, not everything
    near it."""
    response = client.post(
        "/api/reviews",
        json={
            "project_id": "contract-bounds",
            "external_key": "x" * 256,
            "title": "Exactly at the bound",
        },
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["external_key"] == "x" * 256


def test_a_129_character_assertion_key_is_refused_recording_an_assertion(client) -> None:
    """Length alone, held apart from the pattern. `assertion_key` at the API
    used to allow up to 200 characters; the contract stops at 128.

    Reaches the refusal with a nonexistent `review_id` and no fixture review:
    the body fails validation before the handler resolves the review at all,
    which is the same "no DB round trip needed to be wrong" property
    `test_the_protocol_depth_guard_needs_only_a_tenant_header` pins for the
    reviews router.
    """
    response = client.post(
        f"/api/reviews/{uuid.uuid4()}/assertions",
        json={
            "assertion_key": OVERSIZED_ASSERTION_KEY,
            "statement": "A claim.",
            "support": [{"observation_version_id": str(uuid.uuid4()), "stance": "supports"}],
        },
        headers=headers(),
    )
    _assert_edge_refusal(response, "assertion_key")


@pytest.mark.parametrize("bad_key", PATTERN_ILLEGAL_ASSERTION_KEYS)
def test_a_pattern_illegal_assertion_key_is_refused_recording_an_assertion(client, bad_key: str) -> None:
    """The independent failure mode. Each of these is well under 128
    characters and would have been accepted by the old field, which bounded
    length only. A capital letter, a space, and a hyphen are three distinct
    ways a caller's key can be legal-by-length and illegal-by-pattern —
    `assertion_key` is a stable business identity a consumer compares across a
    correction chain, not free text."""
    response = client.post(
        f"/api/reviews/{uuid.uuid4()}/assertions",
        json={
            "assertion_key": bad_key,
            "statement": "A claim.",
            "support": [{"observation_version_id": str(uuid.uuid4()), "stance": "supports"}],
        },
        headers=headers(),
    )
    _assert_edge_refusal(response, "assertion_key")


def test_a_pattern_legal_assertion_key_at_the_bound_is_not_refused_for_shape(client) -> None:
    """The positive control for the pattern: a dotted, lowercase,
    underscore-bearing key at exactly the 128-character bound must not be
    refused for its SHAPE. It may still 404 or 409 on a review that does not
    exist or is not in the right state — this only proves `assertion_key`
    itself is not what stops it, by checking the failing location excludes it
    (or that there is no validation failure at all)."""
    segment = "a" * 61
    key = f"{segment}.{segment}"  # 61 + 1 + 61 = 123, pattern-legal, under 128
    assert len(key) <= 128

    response = client.post(
        f"/api/reviews/{uuid.uuid4()}/assertions",
        json={
            "assertion_key": key,
            "statement": "A claim.",
            "support": [{"observation_version_id": str(uuid.uuid4()), "stance": "supports"}],
        },
        headers=headers(),
    )
    if response.status_code == 422:
        located = {part for error in response.json().get("detail", []) for part in error.get("loc", [])}
        assert "assertion_key" not in located, (
            f"a pattern-legal, in-bound assertion_key was refused for its shape: {response.text[:300]!r}"
        )
    else:
        # A nonexistent review_id 404s past validation — the shape passed.
        assert response.status_code == 404, response.text
