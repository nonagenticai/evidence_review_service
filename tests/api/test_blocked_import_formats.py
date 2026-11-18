"""`AHTA-DISC-003` and `AHTA-DISC-004` were unreachable from every HTTP entry point.

`import_parser.unsupported_format_blocker` answers a recognised-but-unsupported
format with a blocker id and a sentence naming what is missing — Catchii/Cachii
workbook mappings, or workbook round-trip fixtures. Its only caller is
`imports.import_payload`, and the route ran

    if kind not in SOURCE_KINDS:
        raise ValidationFailedError(f"source_kind must be one of {...}; got {kind!r}")

five lines earlier, on BOTH entry paths — the `?source_kind=` query parameter and
the JSON envelope's `source_kind`, since `kind` is overwritten by the envelope
before that check. None of `xlsx`, `xls`, `workbook`, `catchii` or `cachii` is in
`SOURCE_KINDS`, so the blocker branch could never be reached.

Grepping `AHTA-DISC` across `tests/` returned zero hits, which is what that looks
like from the outside: the response had never been exercised because nothing
could produce it.

What a tester uploading a Catchii export actually got was the list of valid
enum values — no blocker id, no mention of workbooks, and nothing distinguishing
"we decided not to support this, and here is what is missing" from "you made a
typo".
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

TENANT = tenant("tenant-blocked-formats")
LEAD = "blocked-formats-lead"


def headers() -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": LEAD, "X-Actor-Role": "lead"}


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
def review_id(client) -> str:  # noqa: ANN001
    created = client.post(
        "/api/reviews",
        json={"project_id": "blocked", "external_key": f"blocked-{uuid.uuid4()}", "title": "Blocked formats"},
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["review_id"]


@pytest.mark.parametrize(
    ("kind", "blocker", "word"),
    [
        ("xlsx", "AHTA-DISC-004", "workbook"),
        ("xls", "AHTA-DISC-004", "workbook"),
        ("workbook", "AHTA-DISC-004", "workbook"),
        ("catchii", "AHTA-DISC-003", "mappings"),
        ("cachii", "AHTA-DISC-003", "mappings"),
    ],
)
def test_a_blocked_format_is_told_why_it_is_blocked(client, review_id, kind, blocker, word) -> None:  # noqa: ANN001
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind={kind}",
        content=b"irrelevant",
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body.get("blocker") == blocker, (
        f"a {kind!r} import carries blocker {body.get('blocker')!r}, not {blocker!r} — the generic "
        f"SOURCE_KINDS refusal ran first: {body!r}"
    )
    assert word in body.get("detail", "").lower(), f"the refusal does not say what is missing: {body!r}"
    assert "must be one of" not in body.get("detail", ""), (
        "the caller was handed the valid-enum message, which is the generic refusal this test exists "
        "to distinguish from a real blocker"
    )


def test_the_envelope_path_is_blocked_too(client, review_id) -> None:  # noqa: ANN001
    """`source_kind` in the JSON envelope overwrites the query param.

    Both entry paths reach the same check, so a fix applied to only one would
    leave the other answering with the enum.
    """
    response = client.post(
        f"/api/reviews/{review_id}/imports",
        content=json.dumps({"source_kind": "catchii", "payload": {"records": []}}),
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert response.json().get("blocker") == "AHTA-DISC-003", response.json()


def test_an_unrecognised_source_kind_still_gets_the_enum(client, review_id) -> None:  # noqa: ANN001
    """Scoping. The generic refusal is the RIGHT answer for a typo and must survive.

    Without this, replacing the enum message with a blanket blocker would pass
    every assertion above while telling somebody who typed `canonical-json` that
    workbook mappings are unavailable.
    """
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=banana",
        content=b"irrelevant",
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body.get("blocker") is None, body
    assert "must be one of" in body.get("detail", ""), body


def test_a_supported_format_is_not_caught_by_the_blocked_check(client, review_id) -> None:  # noqa: ANN001
    """The other scoping direction: nothing in `SOURCE_KINDS` may be blocked."""
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A real paper", "year": 2020}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code in (200, 201), response.text
    assert "blocker" not in response.text
