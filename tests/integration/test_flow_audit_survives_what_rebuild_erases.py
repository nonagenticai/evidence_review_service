"""`flow:rebuild` reports a rewritten input ONCE and destroys the proof. `flow:audit` does not.

`0015` closed the hole: `import_batches`, `import_rows`, `duplicate_clusters` and
`duplicate_cluster_members` are append-only by trigger now, and the runtime role
cannot write them at all. This file is about the OTHER half of the same item —
what the service can SAY about a rewrite that happened anyway, at owner
privilege, which is the only privilege that can still do it.

Neither existing check can say anything useful.

* **`:verify` is blind by construction.** `verify_export` re-hashes the STORED
  payload and compares it against itself. Rewrite the inputs underneath a sealed
  export and it still answers `verified` — correctly, because the artefact is
  intact. It is the world the artefact describes that moved.
* **`flow:rebuild` is one-shot and self-erasing.** It compares the derivation
  against the newest `flow_projections` row and then calls
  `compute_flow(persist=True)`, which WRITES the new hash as the newest
  projection. So the first rebuild after a rewrite answers
  `matches_previous_projection: false` — and consumes the only record that the
  two ever disagreed. The second answers `true`. That is asserted below, in
  order, because it is the reason a separate operation had to exist.

`flow:rebuild` is not changed by any of this and must not be: it is a cache-drift
detector and correcting the cache it finds stale is what it is FOR. The
anti-prescription in the brief this file was written against says so explicitly.

## The tamper, and why it is this one

Deleting a `duplicate_cluster_members` row from a MERGED cluster is the rewrite
that keeps the ledger derivable. `flow._collect` builds `removed_by_merge` by
walking each merged cluster's members, so dropping the non-survivor returns that
record to the corpus: `duplicates_removed` falls to 0 and `not_yet_screened`
rises to 1, and `check_conservation` still balances — `records_total ==
duplicates_removed + records_screened + not_yet_screened` holds either way.

Every simpler tamper on `import_rows` breaks conservation instead
(`identified_total` moves while `record_ids` does not), which `derive` raises
`ConservationError` for. That is also detected — `flow:audit` reports it as
`derivable: false` rather than 500ing — but it is a different finding, and it
would let `flow:rebuild` off the hook here: a rebuild that cannot derive writes
no projection, so there would be nothing to erase and nothing to demonstrate.

## Persisting nothing is asserted from the database, not from the body

`persisted: false` in the response is a claim. The assertion is a row census of
`flow_projections` and `flow_revisions` taken around each call. It matters more
than it looks: `compute_flow(persist=False)` — the obvious way to write this
endpoint — still opens a `flow_revisions` row through `current_flow_revision`,
which `test_flow_revision_race` pins. `flow_audit` therefore calls `_collect` and
`derive` directly and never touches `compute_flow`, and this census is what would
notice if that changed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-flow-audit")
LEAD = "flow-audit-lead"
REVIEWER = "flow-audit-reviewer"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def owner() -> Iterator[Engine]:
    engine = create_engine(OWNER_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def sealed(client: TestClient) -> dict[str, str]:
    """Three records, one duplicate pair merged, both survivors screened, one export sealed."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "flow-audit", "external_key": f"fa-{uuid.uuid4()}", "title": "Flow audit"},
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
    imported = client.post(
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
    assert imported.status_code == 201, imported.text
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

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 50},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    assert len(queue) == 2, f"the merged-away record should not be served: {queue}"
    for item in queue:
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

    export = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert export.status_code in (200, 201), export.text
    return {
        "review_id": review_id,
        "cluster_id": pair["cluster_id"],
        "loser": loser,
        "export_id": export.json()["export_id"],
    }


# ---------------------------------------------------------------------------


def _audit(client: TestClient, review_id: str) -> dict:
    got = client.post(f"/api/reviews/{review_id}/flow:audit", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


def _rebuild(client: TestClient, review_id: str) -> dict:
    got = client.post(f"/api/reviews/{review_id}/flow:rebuild", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


def _census(owner: Engine, review_id: str) -> dict[str, int]:
    """Rows in the two tables the flow endpoints can write."""
    with owner.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        return {
            table: int(
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE review_id = :r"),  # nosec B608
                    {"r": review_id},
                ).scalar_one()
            )
            for table in ("flow_projections", "flow_revisions")
        }


def _rewrite_the_inputs(owner: Engine, review_id: str, loser: str) -> None:
    """Drop the merged-away member — the tamper `0015`'s trigger refuses.

    Performed at OWNER privilege with the guard explicitly disarmed, because
    after `0015` there is no other way to do it: the runtime role has no DELETE
    on this table and the trigger refuses the owner too. That is the point — this
    is the residual attacker, the one holding the owner credential, and the
    question this file answers is whether the service can still SAY so.
    """
    with owner.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(
            text("ALTER TABLE duplicate_cluster_members DISABLE TRIGGER trg_duplicate_cluster_members_append_only")
        )
        try:
            removed = conn.execute(
                text("DELETE FROM duplicate_cluster_members WHERE review_id = :r AND review_record_id = :m"),
                {"r": review_id, "m": loser},
            ).rowcount
        finally:
            conn.execute(
                text("ALTER TABLE duplicate_cluster_members ENABLE TRIGGER trg_duplicate_cluster_members_append_only")
            )
    assert removed == 1, f"the tamper removed {removed} rows, so nothing below is measuring a rewrite"


def test_audit_outlives_the_rebuild_that_erases_the_evidence(
    client: TestClient, owner: Engine, sealed: dict[str, str]
) -> None:
    """The whole sequence, in one test because the ORDER is the finding.

    Split across test functions this would depend on collection order, and the
    third step's meaning comes entirely from the second having already run.
    """
    review_id = sealed["review_id"]

    # 1. CLEAN. The seal and the live history agree, and the audit says so.
    before = _audit(client, review_id)
    assert before["inputs_rewritten"] is False, before
    assert before["derivable"] is True, before
    assert before["exports_audited"] >= 1, before
    assert {f["outcome"] for f in before["export_findings"]} == {"agrees"}, before["export_findings"]

    # 2. AND IT WROTE NOTHING. Asserted from the tables, not from `persisted`.
    census_before = _census(owner, review_id)
    assert before["persisted"] is False, before
    _audit(client, review_id)
    _audit(client, review_id)
    assert _census(owner, review_id) == census_before, (
        "flow:audit wrote to flow_projections or flow_revisions. Even with persist=False, "
        "compute_flow opens a revision row through current_flow_revision — flow_audit must not "
        f"call it: {census_before} -> {_census(owner, review_id)}"
    )

    # 3. THE REWRITE.
    _rewrite_the_inputs(owner, review_id, sealed["loser"])

    after = _audit(client, review_id)
    assert after["derivable"] is True, after
    assert after["inputs_rewritten"] is True, (
        "an input of a SEALED export was rewritten and flow:audit did not report it. The seal's "
        f"canonical_counts_hash no longer describes the live history: {after}"
    )
    finding = next(f for f in after["export_findings"] if f["export_id"] == sealed["export_id"])
    assert finding["outcome"] == "inputs_rewritten", finding
    # NOT `diverged_after_new_history`. Nothing was appended since the seal, and
    # that is the whole discriminator: a derivation over append-only tables
    # cannot change while nothing is appended.
    assert not any(finding["appended_since_seal"].values()), finding
    assert "duplicates_removed" in finding["differing_counts"], finding
    assert "not_yet_screened" in finding["differing_counts"], finding

    # 4. WHAT `flow:rebuild` DOES WITH IT — reports it once, then cannot again.
    first = _rebuild(client, review_id)
    assert first["matches_previous_projection"] is False, (
        "the first rebuild after a rewrite must notice the drift; if it does not, this test's "
        f"tamper did not move the counts and step 5 proves nothing: {first}"
    )
    second = _rebuild(client, review_id)
    assert second["matches_previous_projection"] is True, (
        "THE PREMISE OF THIS WHOLE FILE. The second rebuild is expected to report agreement, "
        "because the first one persisted the tampered derivation as the newest projection. If "
        f"this is False, flow:rebuild is no longer self-erasing and the premise needs re-reading: {second}"
    )

    # 5. THE POINT. The audit still reports it, after the rebuild consumed its
    #    own evidence — same finding, same export, no cache involved.
    outlived = _audit(client, review_id)
    assert outlived["inputs_rewritten"] is True, (
        "flow:audit stopped reporting the rewrite once flow:rebuild had overwritten the "
        "projection. It is anchored on the append-only sealed export precisely so that cannot "
        f"happen — something is reading the projection as the anchor: {outlived}"
    )
    assert outlived["persisted"] is False, outlived

    # 6. AND THE ARITHMETIC OF WHO WROTE WHAT. Two rebuilds, five audits, and
    #    exactly two new projection rows.
    census_after = _census(owner, review_id)
    assert census_after["flow_projections"] == census_before["flow_projections"] + 2, (
        f"expected exactly the two rebuilds to have written a projection: {census_before} -> {census_after}"
    )
    assert census_after["flow_revisions"] == census_before["flow_revisions"], census_after
