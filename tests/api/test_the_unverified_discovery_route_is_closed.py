"""`POST /imports?source_kind=discovery_session` took caller bytes as a discovery session.

`infrastructure/clients/discovery_engine.py` opens by explaining that screening's
corpus used to arrive as bytes the BROWSER built, that the digest therefore named
a construction outside the trust boundary, and that the browser now names a batch
which this service goes and fetches. That is half true. The verified route,
`POST /imports/from-discovery`, was added ALONGSIDE the unverified one rather
than in place of it.

Reproduced before this module: `POST /api/reviews/{id}/imports?source_kind=discovery_session`
accepts an arbitrary body, has no `expected_digest`, never contacts
discovery-engine, and writes `raw_payload_digest` over whatever arrived. The
resulting `import_batches` row is indistinguishable downstream from a pulled one
except by `source_ref.fetch_mode`, which is null on this path — and nothing
downstream reads it. `identified_total`, the corpus manifest and the PRISMA
appendix all rest on that digest.

So the two routes wrote the same `source_kind` with different meanings: on one
it means "these are bytes discovery-engine froze and we hashed them ourselves",
on the other "somebody posted this".

## What is NOT changed here

`models.records.SOURCE_KINDS` and the database still accept `discovery_session`,
and must: it is the value `/imports/from-discovery` writes for every legitimately
pulled batch, and historical rows carry it. The refusal is on the generic route
only — the one that takes bytes.

## The migration this deliberately does not write

Tightening `import_batches.source_kind` in the database would invalidate every
historical row written through the unverified path, which is exactly the wrong
answer: those rows are real imports and their provenance, thin as it is, is what
an auditor needs. If a constraint is ever wanted it is not on `source_kind` but
on the pair — something of the form

    CHECK (source_kind <> 'discovery_session'
           OR source_ref ->> 'fetch_mode' = 'discovery_engine_pull')

applied only to rows created after the cutover, since existing
`discovery_session` rows have a null `fetch_mode` and would fail it. That needs a
backfill decision about what to do with them and belongs in its own change.
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

TENANT = tenant("tenant-unverified-discovery")
LEAD = "unverified-discovery-lead"


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
        json={"project_id": "unverified", "external_key": f"unverified-{uuid.uuid4()}", "title": "Unverified route"},
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["review_id"]


#: A body that PARSES as a discovery session export. The point is that parsing
#: was never the issue: these bytes are well-formed and still have no provenance.
_PLAUSIBLE = json.dumps(
    {
        "document_version": 3,
        "session_id": "019fe51a-ccf7-72af-8367-347f5872ee19",
        "records": [{"title": "A study a browser made up", "doi": "10.1000/fabricated"}],
        "record_count": 1,
    }
).encode()


def test_the_query_parameter_form_is_refused(client, review_id) -> None:  # noqa: ANN001
    """`?source_kind=discovery_session` with a body is no longer an import."""
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=discovery_session",
        content=_PLAUSIBLE,
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422, response.text
    detail = response.json().get("detail", "")
    # The refusal has to name the route that DOES work, or it is a dead end for
    # whoever is holding a session they legitimately want to import.
    assert "from-discovery" in detail, f"the refusal does not name the verified route: {detail!r}"


def test_the_envelope_form_is_refused_too(client, review_id) -> None:  # noqa: ANN001
    """Both entry paths funnel through one `kind`, and both must be closed.

    The envelope overwrites `kind` AFTER the query parameter is read, so a guard
    placed on the query parameter alone would leave this path open — which is
    how `unsupported_format_blocker` became unreachable in the first place.
    """
    response = client.post(
        f"/api/reviews/{review_id}/imports",
        json={"source_kind": "discovery_session", "payload": json.loads(_PLAUSIBLE)},
        headers=headers(),
    )

    assert response.status_code == 422, response.text
    assert "from-discovery" in response.json().get("detail", "")


def test_the_refusal_is_not_the_generic_enum_message(client, review_id) -> None:  # noqa: ANN001
    """ "must be one of ..." would send the caller looking for a typo they did not make.

    `discovery_session` is a real source kind that this service really writes.
    The caller's mistake is the ROUTE, and the message has to say so — the same
    distinction `test_blocked_import_formats` draws for workbook uploads.
    """
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=discovery_session",
        content=_PLAUSIBLE,
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )

    detail = response.json().get("detail", "")
    assert "must be one of" not in detail, f"the generic enum refusal ran instead: {detail!r}"


def test_nothing_was_written_by_the_refused_import(client, review_id) -> None:  # noqa: ANN001
    """A refusal that still created the batch would be the defect with extra steps."""
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=discovery_session",
        content=_PLAUSIBLE,
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )

    listed = client.get(f"/api/reviews/{review_id}/imports", headers=headers())
    assert listed.status_code == 200, listed.text
    kinds = [item["source_kind"] for item in listed.json()["items"]]
    assert "discovery_session" not in kinds, f"the refused import was written anyway: {kinds!r}"


def test_the_upload_route_still_accepts_the_kinds_that_carry_their_own_bytes(client, review_id) -> None:  # noqa: ANN001
    """Non-vacuity: only `discovery_session` is closed, not the route.

    `canonical_json`, `ris`, `bibtex` and `csv` ARE caller-supplied bytes by
    definition — a reviewer uploading an EndNote export is the intended use, and
    no upstream exists to corroborate them against. They are unaffected.
    """
    response = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "An uploaded study", "doi": "10.1000/uploaded"}]}).encode(),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["accepted_count"] == 1


def test_the_database_vocabulary_still_permits_the_value(client) -> None:  # noqa: ANN001
    """The pulled route writes `discovery_session`, so the value stays legal.

    Pinned because the tempting fix is to drop the value from the model tuple
    and add a migration narrowing the CHECK constraint. That would invalidate
    every historical row and break `/imports/from-discovery`, which writes this
    exact value on the verified path.
    """
    from models.records import SOURCE_KINDS as MODEL_SOURCE_KINDS

    assert "discovery_session" in MODEL_SOURCE_KINDS
