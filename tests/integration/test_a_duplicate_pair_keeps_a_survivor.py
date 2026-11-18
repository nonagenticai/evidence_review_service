"""A merge removes duplicates. It must never remove the study.

Two deduplication runs over the same pair, each merged with a DIFFERENT
survivor, took BOTH members out of the corpus. `flow._collect` unions the
removals of every cluster whose current resolution is a merge:

    for member in members_by_cluster[cluster_id]:
        if member != resolution.survivor_record_id:
            removed[member] = resolution.resolution_id

Cluster 1 says "keep A, remove B". Cluster 2 — a later run over the same corpus,
which re-detects the same pair because a seal contains every record and not just
the surviving ones — says "keep B, remove A". The union is `{A, B}`. Neither is
screened, neither is excluded, neither is in the inclusion set, and nothing
anywhere records that a study left the review. The export seals and `:verify`
answers verified.

Nothing exotic is needed: the DEFAULT engine config, two imports, two seals, two
runs, and a lead who changed their mind about which record to keep.

## What "correct" is here, precisely

Not "a cluster's survivor is never removed" — that is too strong and would refuse
a legitimate shape. If A~B keeps A, and a later run finds A~C and keeps C, then
A is removed by cluster 2 while it was cluster 1's survivor; the three records
are transitively one study and keeping only C is right. B, A removed, C kept: the
study is still in the review.

The invariant is over CONNECTED COMPONENTS of the merge graph: every component
must retain at least one record. That is the honest statement of "a real study
must not vanish", and it is what :func:`components_without_survivor` computes.

## Where it is enforced

Twice, deliberately.

* `resolve_cluster` refuses the write. That is the fix — the user gets told at
  the moment they make the choice, naming the other cluster that conflicts.
* `flow._collect` raises `ConservationError`. That is the backstop, for rows
  written before the refusal existed and for any path that reaches the table
  without going through the service. A guard only at the write is a guard that
  historical data walks past.
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

TENANT = tenant("tenant-pair-survivor")
LEAD = "pair-survivor-lead"


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


def _import(client, review_id: str, records: list[dict]) -> None:  # noqa: ANN001
    got = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": records}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert got.status_code in (200, 201), got.text


def _seal(client, review_id: str) -> str:  # noqa: ANN001
    got = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert got.status_code in (200, 201), got.text
    body = got.json()
    assert not body.get("replayed"), "the seal was replayed: membership did not change, so no second run is possible"
    return body["corpus_version_id"]


def _run_and_find_pair(client, review_id: str, doi: str) -> dict:  # noqa: ANN001
    """Start a dedup run and return the cluster holding the shared-DOI pair."""
    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers())
    assert run.status_code in (200, 201), run.text
    body = run.json()
    assert not body.get("replayed"), "the run was replayed; the corpus or config did not actually change"

    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": body["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == 1, f"expected exactly the one shared-DOI cluster, got {len(clusters)}"
    cluster = clusters[0]
    assert len(cluster["members"]) == 2, cluster["members"]
    return cluster


def _merge(client, review_id: str, cluster_id: str, survivor_record_id: str):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor_record_id,
            "rationale": "same DOI",
        },
        headers=headers(),
    )


def _flow(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


def _review_with_a_pair_merged_once(client) -> dict:  # noqa: ANN001
    """A review whose one duplicate pair has been merged, keeping `first`.

    Four records, so the pair is a minority of the corpus and the counts below
    cannot be satisfied by an empty review.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "pair-survivor", "external_key": f"ps-{uuid.uuid4()}", "title": "Pair survivor"},
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
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )

    doi = f"10.1000/pair-survivor.{uuid.uuid4()}"
    _import(
        client,
        review_id,
        [
            {"title": "The pair, as indexed by PubMed", "year": 2021, "doi": doi},
            {"title": "The pair, as indexed by Embase", "year": 2021, "doi": doi},
            {"title": "An unrelated study", "year": 2021, "doi": f"10.1000/unrelated.{uuid.uuid4()}"},
        ],
    )
    _seal(client, review_id)

    cluster_one = _run_and_find_pair(client, review_id, doi)
    first, second = cluster_one["members"][0], cluster_one["members"][1]

    merged = _merge(client, review_id, cluster_one["cluster_id"], first["review_record_id"])
    assert merged.status_code in (200, 201), merged.text

    # A fourth record, so the membership changes and the next seal is a NEW
    # corpus version rather than a replay of the one already deduplicated.
    _import(client, review_id, [{"title": "A later arrival", "year": 2022, "doi": f"10.1000/later.{uuid.uuid4()}"}])
    _seal(client, review_id)

    return {
        "review_id": review_id,
        "doi": doi,
        "first": first,
        "second": second,
        "cluster_one_id": cluster_one["cluster_id"],
    }


# --------------------------------------------------------------------------


def test_a_second_run_cannot_remove_the_survivor_of_the_first(client) -> None:
    """THE DEFECT. Both members of one pair, removed, by two default-config runs.

    Before the fix this second merge answered 201 and `duplicates_removed`
    became 2: the study was in no bucket at all.
    """
    fixture = _review_with_a_pair_merged_once(client)
    review_id, second = fixture["review_id"], fixture["second"]

    cluster_two = _run_and_find_pair(client, review_id, fixture["doi"])
    assert cluster_two["cluster_id"] != fixture["cluster_one_id"], "the second run reused the first run's cluster"

    # The lead changes their mind: keep the OTHER one.
    reversed_merge = _merge(client, review_id, cluster_two["cluster_id"], second["review_record_id"])

    assert reversed_merge.status_code == 422, (
        "a merge that leaves a duplicate group with no surviving record was accepted "
        f"(HTTP {reversed_merge.status_code}). Both members of the pair are now removed and the "
        "study has left the review with no exclusion reason and no decision."
    )
    detail = json.dumps(reversed_merge.json())
    assert fixture["cluster_one_id"] in detail, (
        "the refusal must name the resolution it conflicts with, or the lead cannot act on it: " + detail
    )

    counts = _flow(client, review_id)["counts"]
    assert counts["duplicates_removed"] == 1, (
        f"one duplicate pair must remove exactly one record, not {counts['duplicates_removed']}"
    )
    assert counts["records_total"] == 4, counts


def test_the_first_run_can_still_be_reversed_by_a_split(client) -> None:
    """NEGATIVE CONTROL. The refusal must not make a cluster unresolvable.

    Splitting cluster one returns both records to the corpus, and the reversed
    merge on cluster two is then the only merge in play and must be accepted.
    Without this, "refuse when a component would empty" is indistinguishable
    from "refuse the second merge always".
    """
    fixture = _review_with_a_pair_merged_once(client)
    review_id, second = fixture["review_id"], fixture["second"]
    cluster_two = _run_and_find_pair(client, review_id, fixture["doi"])

    split = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_one_id']}:resolve",
        json={"resolution_type": "split", "rationale": "different studies after all"},
        headers=headers(),
    )
    assert split.status_code in (200, 201), split.text

    reversed_merge = _merge(client, review_id, cluster_two["cluster_id"], second["review_record_id"])
    assert reversed_merge.status_code in (200, 201), (
        "cluster one is split, so nothing is removed by it and this merge empties no component: " + reversed_merge.text
    )

    counts = _flow(client, review_id)["counts"]
    assert counts["duplicates_removed"] == 1, counts


def test_the_ledger_refuses_to_derive_over_rows_the_service_never_wrote(client) -> None:
    """THE BACKSTOP. The refusal above is the fix; this is what catches history.

    Every merge already in `duplicate_resolutions` was written before the check
    existed, so a guard only at the write is a guard that historical data walks
    straight past. The reversed merge is inserted directly — as the owner, with
    the tenant GUC bound, because `FORCE ROW LEVEL SECURITY` filters the owner
    too and an unbound GUC silently writes nothing.

    `/flow` must refuse rather than answer. Returning counts anyway is a ledger
    that has quietly lost a study, and the export gate and the published PRISMA
    diagram are both derived from it.
    """
    import sqlalchemy

    fixture = _review_with_a_pair_merged_once(client)
    review_id, second = fixture["review_id"], fixture["second"]
    cluster_two = _run_and_find_pair(client, review_id, fixture["doi"])

    assert _flow(client, review_id)["counts"]["duplicates_removed"] == 1, "the fixture is already inconsistent"

    owner_url = os.environ["MIGRATION_DATABASE_URL"]
    engine = sqlalchemy.create_engine(owner_url)
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": TENANT},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO duplicate_resolutions "
                "(resolution_id, cluster_id, review_id, tenant_id, resolution_type, "
                " supersedes_resolution_id, survivor_record_id, rationale, decided_by_subject_id) "
                "VALUES (:rid, :cid, :review, :tenant, 'merge', NULL, :survivor, 'written behind the service', :who)"
            ),
            {
                "rid": str(uuid.uuid4()),
                "cid": cluster_two["cluster_id"],
                "review": review_id,
                "tenant": TENANT,
                "survivor": second["review_record_id"],
                "who": LEAD,
            },
        )
    engine.dispose()

    refused = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert refused.status_code == 500, (
        f"the ledger derived counts over a corpus that lost a study (HTTP {refused.status_code}): {refused.text}"
    )

    # 500 alone would also be produced by an unrelated crash, so pin the BODY
    # and the records it names. This used to assert the exception propagated out
    # of a `raise_server_exceptions=True` client — which proved the guard fired
    # but said nothing about what an operator receives, and what they received
    # was a bare `Internal Server Error` with the message discarded.
    #
    # `conservation_violation` is this module's shape for "the database holds a
    # state the ledger cannot represent", and it is deliberately 5xx rather than
    # 4xx: after `resolve_cluster`'s refusal the only way to reach it is a write
    # that bypassed the service, so the request was not wrong — the data is, and
    # somebody should be paged. The body is what makes the page actionable.
    problem = refused.json()
    assert problem["code"] == "conservation_violation", problem
    assert second["review_record_id"] in problem["detail"], problem
    assert cluster_two["cluster_id"] in problem["detail"], problem


def test_a_merge_naming_the_same_survivor_twice_is_accepted(client) -> None:
    """NEGATIVE CONTROL. Re-merging the same pair the SAME way is not a conflict.

    A second run over an unchanged pair is the ordinary case — the lead confirms
    the earlier choice. The union is `{second}` either way and the component
    keeps `first`, so this must pass. It is the case a naive "this record is
    already removed" check would wrongly refuse.
    """
    fixture = _review_with_a_pair_merged_once(client)
    review_id, first = fixture["review_id"], fixture["first"]
    cluster_two = _run_and_find_pair(client, review_id, fixture["doi"])

    again = _merge(client, review_id, cluster_two["cluster_id"], first["review_record_id"])
    assert again.status_code in (200, 201), again.text

    counts = _flow(client, review_id)["counts"]
    assert counts["duplicates_removed"] == 1, counts
