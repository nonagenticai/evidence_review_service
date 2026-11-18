"""Clicking Seal twice minted two permanent, indistinguishable inclusion sets.

`create_export` never asked whether this review had already been exported, and
`inclusion_exports` has no uniqueness beyond its primary key. Measured on
`1d98b19`, three POSTs on an untouched review:

    export 1: 201 id=f527e4ec manifest=sha256:cb405894… digest=sha256:ea5c49fd…
    export 2: 201 id=758b95ac manifest=sha256:cb405894… digest=sha256:ea5c49fd…
    export 3: 201 id=c4a83170 manifest=sha256:cb405894… digest=sha256:ea5c49fd…
    all three :verify -> true      GET /exports count=3

Every member of the manifest is a function of immutable state, so the duplicates
are byte-identical — and the table is append-only, so they are permanent. A
consumer citing "the inclusion set" of this review is handed three
indistinguishable seals and has to guess which one is meant.

## Replayed, not refused

The house pattern, and the same spelling: `imports.seal_corpus_version`,
`imports.import_payload` and `deduplication.create_run` all return the existing
row with `replayed=True` when the inputs have not moved. It is also what the
caller wants — the studio gets an export back and can render it, which matters
because `fr`'s `reviews.ts` has no read path for exports at all and its Seal
button therefore cannot know one already exists.

## Keyed on the manifest, not on "has any export"

`test_a_changed_review_can_still_be_exported_again` is the control that makes
that distinction load-bearing. A reopen bumps `flow_revision_number`, so a
genuinely new inclusion set has a different manifest and must still be minted.
Refusing on "this review has an export" would make an inclusion set
un-reissuable after a re-screen, which is a worse defect than the one being
fixed.
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

TENANT = tenant("tenant-one-export")
LEAD = "one-export-lead"


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
def exportable(client):  # noqa: ANN001, ANN201
    """A review screened to completion, ready to seal."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "one-export", "external_key": f"oe-{uuid.uuid4()}", "title": "One export"},
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
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["rv"]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers("rv", "reviewer"),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "round_number": 1,
            "review_record_id": record_id,
            "disposition": "include",
            "rationale": "meets the criteria",
        },
        headers=headers("rv", "reviewer"),
    )
    assert decided.status_code == 201, decided.text
    return review_id


def _export(client, review_id: str):  # noqa: ANN001, ANN201
    return client.post(f"/api/reviews/{review_id}/exports", headers=headers())


def _list(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/exports", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_sealing_an_unchanged_review_three_times_leaves_one_export(client, exportable) -> None:
    """THE DEFECT. Three permanent seals, byte-identical, all verifying."""
    seals = []
    for _ in range(3):
        got = _export(client, exportable)
        assert got.status_code == 201, got.text
        seals.append(got.json())

    assert len({s["manifest_hash"] for s in seals}) == 1, "the fixture changed between calls; this proves nothing"
    assert len({s["export_id"] for s in seals}) == 1, (
        "an unchanged review minted more than one inclusion set, and they are byte-identical and "
        f"permanent, so a consumer citing 'the' export has to guess: {[s['export_id'] for s in seals]}"
    )
    assert _list(client, exportable)["count"] == 1, _list(client, exportable)

    assert seals[0]["replayed"] is False, "the first seal is not a replay"
    assert seals[1]["replayed"] is True and seals[2]["replayed"] is True, (
        "the repeats did not say they were replays, so a caller cannot tell 'I sealed this' from "
        f"'this was already sealed': {seals[1]}"
    )


def test_the_replayed_export_still_verifies(client, exportable) -> None:
    """A replay is the real row, not a summary of one.

    Cheap to get wrong: returning a constructed body rather than the stored
    export would give a client something that looks right and cannot be verified.
    """
    first = _export(client, exportable).json()
    again = _export(client, exportable).json()

    verified = client.get(f"/api/reviews/{exportable}/exports/{again['export_id']}:verify", headers=headers())
    assert verified.status_code == 200 and verified.json()["verified"] is True, verified.text
    assert again["payload_digest"] == first["payload_digest"], (first, again)

    with_payload = client.get(
        f"/api/reviews/{exportable}/exports/{again['export_id']}?with_payload=true", headers=headers()
    )
    assert with_payload.status_code == 200, with_payload.text
    assert with_payload.json()["payload"]["included"], "the replayed export has no items"


def test_a_changed_review_can_still_be_exported_again(client, exportable) -> None:
    """THE CONTROL, and the reason this is keyed on the manifest.

    A reopen bumps `flow_revision_number`, which is part of the sealed manifest,
    so this is a genuinely different inclusion set and must be minted. Keying the
    check on "this review has an export" instead would make a review
    un-reissuable after a re-screen — a worse defect than the one being fixed.
    """
    first = _export(client, exportable).json()
    assert first["replayed"] is False

    closed = client.post(f"/api/reviews/{exportable}:close", json={"rationale": "done"}, headers=headers())
    assert closed.status_code in (200, 201), closed.text
    reopened = client.post(
        f"/api/reviews/{exportable}:reopen",
        json={"rationale": "more to do", "approval_attestation_ref": "fr://approval/reopen-1"},
        headers=headers(),
    )
    assert reopened.status_code in (200, 201), reopened.text

    second = _export(client, exportable)
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is False, (
        "the review was reopened, so this is a new inclusion set and must be sealed rather than "
        "replayed: " + second.text
    )
    assert second.json()["export_id"] != first["export_id"], second.text
    assert second.json()["manifest_hash"] != first["manifest_hash"], (
        "the reopen did not move the manifest, so this control is not testing what it claims"
    )
    assert _list(client, exportable)["count"] == 2, _list(client, exportable)
