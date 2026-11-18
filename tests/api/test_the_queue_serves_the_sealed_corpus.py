"""A reviewer must be shown the corpus their assignment was minted from.

## The defect

`create_assignments` resolves a `corpus_version_id`, reads that corpus's members
to decide which records to assign, returns the id in its response — and does not
store it (`screening.py:235` reports it; no column held it). `screening_queue`
then resolved each record's content with `current_record_versions`, the review's
TAIL.

While every record has exactly one version the two agree, which is why nothing
caught this: the whole existing suite operates on single-version records.
`apply_survivorship` writes a v2. From that moment a reviewer whose assignment
came from corpus v1 is served the merged v2 title and abstract, decides on it,
and the decision is attributed to a record whose sealed corpus contains different
content.

`create_export` already refuses to seal an export citing a version the corpus
does not contain (`test_export_seals_the_current_version.py`). That refusal fires
at seal time — after the human judgement it invalidates has been made, and with
no way to recover it except re-screening. This file pins the cause instead of the
consequence.

## Why this is asserted on the RESPONSE and not on the SQL

The opposite choice from `test_export_seals_the_current_version.py`, and for a
reason that is worth stating rather than inferring.

That file could not assert on a response: `review_record_versions` is append-only,
so heap order IS insertion order, and last-row-wins landed on the tail *by
accident* — the observable difference was only in the SQL.

Here the two answers are genuinely different rows. The corpus pins v1; the tail
is v2; `apply_survivorship` gives them different titles by construction. So the
queue's own payload discriminates, and a response-level assertion is both
stronger evidence and immune to the query being spelled a third way.

## Measured, not assumed

Run against `43d2b8e` (the commit before ER-M009) with this file added and the
source unchanged:

* `test_the_queue_serves_the_version_the_corpus_sealed` — **FAILED**:
  `assert 'Tocilizumab in severe COVID-19 (merged)' == 'Tocilizumab in severe
  COVID-19'`. The queue served the survivorship title to a reviewer assigned from
  a corpus that does not contain it. This is the gate.
* `test_an_assignment_records_the_corpus_it_was_minted_from` — **FAILED**, with
  `KeyError: 'corpus_version_id'` reading the column off the row. Structural: it
  proves the column is absent, which is what makes the first failure possible.

Both pass after `0009` + the two call-site changes.
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

TENANT = tenant("tenant-queue-corpus")
LEAD = "queue-corpus-lead"
REVIEWER = "queue-corpus-reviewer"

#: One shared DOI so the dedup engine clusters them, and two titles so a
#: survivorship choice has something to be about. The reviewer is assigned
#: BEFORE the merge, so `SEALED_TITLE` is what the corpus holds for them.
SHARED_DOI = "10.1000/queue-corpus"
SEALED_TITLE = "Tocilizumab in severe COVID-19"
MERGED_TITLE = "Tocilizumab in severe COVID-19 (merged)"


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
def assigned_before_the_merge(client):  # noqa: ANN001, ANN201
    """A sealed corpus, a reviewer assigned from it, and an unresolved duplicate cluster.

    The ORDER is the whole fixture: seal, then assign, then merge. Assigning after
    the merge would make the corpus and the tail agree again and the gate would
    pass against the broken code.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "queue-corpus", "external_key": f"qc-{uuid.uuid4()}", "title": "Queue corpus"},
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

    doi = f"{SHARED_DOI}.{uuid.uuid4()}"
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": SEALED_TITLE, "year": 2021, "doi": doi},
                    {"title": MERGED_TITLE, "year": 2021, "doi": doi},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text

    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert sealed.status_code in (200, 201), sealed.text
    corpus_version_id = sealed.json()["corpus_version_id"]

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER], "corpus_version_id": corpus_version_id},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text

    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers())
    assert run.status_code in (200, 201), run.text
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run.json()["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == 1, f"fixture expected exactly one cluster, got {len(clusters)}: {clusters}"
    cluster = clusters[0]

    survivor = next(m for m in cluster["members"] if m["title"] == SEALED_TITLE)
    loser = next(m for m in cluster["members"] if m["title"] == MERGED_TITLE)

    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "corpus_version_id": corpus_version_id,
        "cluster_id": cluster["cluster_id"],
        "survivor": survivor,
        "loser": loser,
    }


def _queue(client, review_id: str, stage_id: str) -> list[dict]:  # noqa: ANN001
    got = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()["items"]


# --------------------------------------------------------------------------
# The gate.
# --------------------------------------------------------------------------


def test_the_queue_serves_the_version_the_corpus_sealed(client, assigned_before_the_merge) -> None:
    """A survivorship merge must not change what an already-assigned reviewer sees.

    The reviewer is assigned from corpus v1, which holds `SEALED_TITLE`. The lead
    then merges the duplicate pair and chooses the OTHER title, which writes a v2
    on the survivor. The queue must still serve v1 — the reviewer is screening a
    sealed set, and the set did not change.
    """
    fixture = assigned_before_the_merge
    review_id = fixture["review_id"]
    survivor_record_id = fixture["survivor"]["review_record_id"]

    before = {item["review_record_id"]: item for item in _queue(client, review_id, fixture["stage_id"])}
    assert before[survivor_record_id]["title"] == SEALED_TITLE, (
        "the fixture is wrong before the merge has even happened"
    )
    sealed_version_id = before[survivor_record_id]["review_record_version_id"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor_record_id,
            "rationale": "same trial reported twice",
            "survivorship": {"title": fixture["loser"]["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text

    # The merge really did write a new version — otherwise this test proves
    # nothing, and would pass against a survivorship that silently did nothing.
    record = client.get(f"/api/reviews/{review_id}/records/{survivor_record_id}", headers=headers()).json()
    assert record["version_count"] == 2, record
    assert record["versions"][-1]["title"] == MERGED_TITLE, record

    after = {item["review_record_id"]: item for item in _queue(client, review_id, fixture["stage_id"])}
    served = after[survivor_record_id]

    assert served["title"] == SEALED_TITLE, (
        "the queue served the post-merge title to a reviewer assigned from a corpus that does "
        f"not contain it. The reviewer would decide on {served['title']!r} and the decision "
        f"would be recorded against a corpus holding {SEALED_TITLE!r}."
    )
    assert served["review_record_version_id"] == sealed_version_id, (
        "the queue moved the reviewer onto a different version of the record mid-stage, with no "
        "event and nothing in the payload to say so"
    )


def test_an_assignment_records_the_corpus_it_was_minted_from(client, assigned_before_the_merge) -> None:
    """The column that makes the gate above possible.

    Structural rather than behavioural: it fails with `KeyError` on the commit
    before `0009`, which proves the column is absent, not that anything read the
    wrong row. Kept as its own test so the gate's failure is never attributed to
    a missing column when it is really a wrong read.
    """
    import sqlalchemy as sa

    fixture = assigned_before_the_merge
    migration_url = os.environ.get("MIGRATION_DATABASE_URL") or DB_URL
    engine = sa.create_engine(migration_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            # `screening_assignments` is `FORCE ROW LEVEL SECURITY`, so the
            # OWNER is filtered by the tenant policy too. Without this the query
            # returns zero rows and the assertion below fails with "the fixture
            # created no assignments" — a false negative that looks exactly like
            # a broken fixture. The same trap made `0009`'s first backfill a
            # silent no-op; it is worth paying for twice rather than working
            # around with a superuser connection, which would prove less.
            conn.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": TENANT})
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT corpus_version_id FROM screening_assignments "
                        "WHERE review_id = :rid AND reviewer_subject_id = :sub"
                    ),
                    {"rid": fixture["review_id"], "sub": REVIEWER},
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()

    assert rows, "the fixture created no assignments"
    stored = {str(row["corpus_version_id"]) for row in rows}
    assert stored == {fixture["corpus_version_id"]}, (
        f"assignments name {stored}, but they were minted from {fixture['corpus_version_id']}"
    )
