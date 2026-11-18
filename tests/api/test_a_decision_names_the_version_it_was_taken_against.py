"""A decision must cite the content the reviewer actually read.

`ER-M009` made the queue serve the version sealed into the assignment's corpus,
so a survivorship merge can no longer change what an already-assigned reviewer
sees. `record_decision` was not moved with it. It kept taking

    ORDER BY version_number DESC LIMIT 1

— the review's TAIL — so the reviewer reads the corpus member and the decision is
stamped with a version minted after they were assigned, which they never saw.

Two consequences, and they pull in opposite directions, which is why both are
tested here.

## 1. An export refused for a re-seal that never happened

`create_export` compares the version each included record was decided against
with the version the cited corpus contains, and refuses:

    "... were screened against a different version than corpus version X
     contains — the corpus was re-sealed after screening."

Nothing was re-sealed. The corpus is the one the reviewer was assigned from and
the one the export cites; only the stamp was wrong. The message names a cause
that did not occur, and its two suggested remedies — "export before re-sealing"
and "re-run the stage against the new corpus" — are respectively impossible and
a request to re-screen against content nobody has read.

## 2. The gate goes silent in exactly the case it exists to catch

`_decided_versions` omitted a record whose reviewers named different versions,
and the caller reads an absent entry as "nothing to contradict". So one reviewer
citing content nobody read was refused, and TWO reviewers citing two different
things — strictly worse — sailed through. The fix reports the whole set at the
settling position and the caller refuses anything that is not exactly the
corpus's version.
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

TENANT = tenant("tenant-decision-version")
LEAD = "decision-version-lead"
REVIEWER = "decision-version-reviewer"

SHARED_DOI = "10.1000/decision-version"
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
def assigned_then_merged(client):  # noqa: ANN001, ANN201
    """Seal, assign, THEN merge with survivorship, so tail != corpus member.

    The order is the fixture. Merging before the assignment would make the tail
    and the corpus agree again and every assertion below would pass against the
    broken code.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "decision-version", "external_key": f"dv-{uuid.uuid4()}", "title": "Decision version"},
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
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run.json()["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == 1, clusters
    cluster = clusters[0]
    survivor = next(m for m in cluster["members"] if m["title"] == SEALED_TITLE)
    loser = next(m for m in cluster["members"] if m["title"] == MERGED_TITLE)

    # Mints v2 on the survivor. The reviewer was assigned from a corpus that
    # holds v1 and the queue still serves them v1.
    merged = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "same DOI",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert merged.status_code in (200, 201), merged.text
    assert merged.json()["applied_version_id"], "the fixture needs a v2; survivorship applied nothing"

    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "corpus_version_id": corpus_version_id,
        "survivor": survivor,
        "sealed_version_id": survivor["review_record_version_id"],
        "tail_version_id": merged.json()["applied_version_id"],
    }


def _queue(client, review_id: str, stage_id: str, subject: str = REVIEWER) -> list[dict]:  # noqa: ANN001
    got = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(subject, "reviewer"),
    )
    assert got.status_code == 200, got.text
    return got.json()["items"]


def _decide(client, fixture, subject: str = REVIEWER, disposition: str = "include"):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{fixture['review_id']}/decisions",
        json={
            "stage_id": fixture["stage_id"],
            "round_number": 1,
            "review_record_id": fixture["survivor"]["review_record_id"],
            "disposition": disposition,
            "rationale": "meets the criteria",
        },
        headers=headers(subject, "reviewer"),
    )


# --------------------------------------------------------------------------


def test_the_decision_cites_the_version_the_queue_served(client, assigned_then_merged) -> None:
    """THE GATE. The reviewer read v1; the decision must say v1.

    Asserted against what the queue ACTUALLY served on this request rather than
    against the fixture's recollection, so the two cannot drift apart and quietly
    make this test about nothing.
    """
    fixture = assigned_then_merged
    served = next(
        item
        for item in _queue(client, fixture["review_id"], fixture["stage_id"])
        if item["review_record_id"] == fixture["survivor"]["review_record_id"]
    )
    assert served["title"] == SEALED_TITLE, "the queue no longer serves the sealed corpus; ER-M009 has regressed"
    assert served["review_record_version_id"] == fixture["sealed_version_id"]
    assert fixture["tail_version_id"] != fixture["sealed_version_id"], "the fixture minted no second version"

    decided = _decide(client, fixture)
    assert decided.status_code == 201, decided.text

    assert decided.json()["review_record_version_id"] == served["review_record_version_id"], (
        "the decision names a version the reviewer was never shown. They read "
        f"{served['review_record_version_id']} and the service recorded "
        f"{decided.json()['review_record_version_id']}, which is the review's tail."
    )


def test_the_export_is_not_refused_for_a_reseal_that_never_happened(client, assigned_then_merged) -> None:
    """The consequence, end to end. Nothing was re-sealed, so nothing may refuse.

    This is the assertion that fails loudly on unmodified source: the export
    blames a re-seal, and its own suggested remedies are impossible.
    """
    fixture = assigned_then_merged
    assert _decide(client, fixture).status_code == 201

    exported = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers())
    assert exported.status_code == 201, (
        "the export was refused although the corpus it cites is the one the reviewer was assigned from "
        f"and screened against: {exported.text}"
    )
    assert exported.json()["included_count"] == 1, exported.text


def test_two_reviewers_naming_different_versions_are_refused_not_dropped(client) -> None:
    """The silent-pass half. Disagreement must refuse, not vanish.

    `_decided_versions` returned nothing for a record whose reviewers named
    different versions, and an absent entry reads to the caller as "nothing to
    contradict". One reviewer citing unread content was refused; two reviewers
    citing two different things passed. This drives the second decision through
    the service and then rewrites its version stamp directly, because after the
    fix the service cannot produce the disagreement at all — which is the point.
    """
    import sqlalchemy

    review_id = client.post(
        "/api/reviews",
        json={"project_id": "decision-version", "external_key": f"dv2-{uuid.uuid4()}", "title": "Two reviewers"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
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
        content=json.dumps({"records": [{"title": SEALED_TITLE, "year": 2021, "doi": f"10.1000/two.{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["rv-one", "rv-two"]},
        headers=headers(),
    )

    served = _queue(client, review_id, stage_id, "rv-one")[0]
    record_id = served["review_record_id"]
    ids = []
    for who in ("rv-one", "rv-two"):
        got = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": "include",
                "rationale": "meets the criteria",
            },
            headers=headers(who, "reviewer"),
        )
        assert got.status_code == 201, got.text
        ids.append(got.json()["decision_id"])

    # Make the two disagree about the version — the state the tail read produced
    # on its own and the fixed code cannot.
    #
    # Written as rv-two SUPERSEDING their own decision, not as an UPDATE: this
    # table is append-only at the database (`trg_screening_decisions_append_only`
    # refuses UPDATE and DELETE) and its `WITH CHECK` demands
    # `reviewer_subject_id = app.actor_subject_id`, so an UPDATE as the owner
    # silently affects zero rows and the test would pass having changed nothing.
    # Both GUCs are bound and the row count is asserted for the same reason.
    #
    # The version named is a real row from another record: a fabricated uuid
    # would be caught by the foreign key rather than by the gate under test.
    other_assignment = _queue(client, review_id, stage_id, "rv-two")[0]["assignment_id"]
    engine = sqlalchemy.create_engine(os.environ["MIGRATION_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(sqlalchemy.text("SELECT set_config('app.actor_subject_id', 'rv-two', true)"))
        conn.execute(sqlalchemy.text("SELECT set_config('app.actor_role', 'reviewer', true)"))
        other = conn.execute(
            sqlalchemy.text(
                "SELECT review_record_version_id FROM review_record_versions "
                "WHERE review_id <> :r AND tenant_id = :t LIMIT 1"
            ),
            {"r": review_id, "t": TENANT},
        ).scalar()
        assert other is not None, "no second version exists in this tenant to point the decision at"
        assert str(other) != served["review_record_version_id"], "the corrupted row names the corpus's own version"
        written = conn.execute(
            sqlalchemy.text(
                "INSERT INTO screening_decisions "
                "(decision_id, review_id, tenant_id, stage_id, round_number, review_record_id, "
                " review_record_version_id, assignment_id, reviewer_subject_id, disposition, "
                " exclusion_reason_code, rationale, supersedes_decision_id) "
                "VALUES (:did, :review, :tenant, :stage, 1, :record, :version, :assignment, 'rv-two', "
                "        'include', NULL, 'a correction', :supersedes)"
            ),
            {
                "did": str(uuid.uuid4()),
                "review": review_id,
                "tenant": TENANT,
                "stage": stage_id,
                "record": record_id,
                "version": other,
                "assignment": other_assignment,
                "supersedes": ids[1],
            },
        ).rowcount
    engine.dispose()
    assert written == 1, "the corrupting insert affected no rows, so this test would prove nothing"

    exported = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert exported.status_code == 422, (
        "two reviewers named two different versions and the export sealed anyway "
        f"(HTTP {exported.status_code}). One reviewer citing unread content is refused; two is worse."
    )
    assert record_id in json.dumps(exported.json()), exported.text
