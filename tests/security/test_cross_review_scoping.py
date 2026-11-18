"""A cluster id from another review, presented under a review the caller holds.

Row-level security in this service is scoped to the TENANT. It is the right
boundary for the threat it addresses, and `test_another_tenant_sees_nothing`
covers it — but it is silent about two reviews inside one tenant, which is the
ordinary case: one organisation running several reviews with different reviewer
sets.

`GET /api/reviews/{A}/duplicate-clusters/{cluster_of_B}/resolutions` used to
filter on `cluster_id` alone. `load_review` confirmed the caller could see review
A, the cluster was never checked against it, and the response carried review B's
rationales and `decided_by_subject_id`. The refusal is `resolve_cluster`'s: a 404
with the message it uses, not a 403.

## The claim that every sibling already made this check was wrong

This docstring used to assert it — "every sibling route (`resolve_cluster`,
`list_clusters`, `get_export`, `unblind_conflict`) already made that check, so
this was an omission rather than a design decision". Those four do. The list was
not the whole family: `POST /api/reviews/{A}/assignments` takes a
`corpus_version_id` off the request body and never checked it against `{A}`
either, and it is the worse of the two — the chain leak is a read, whereas the
assignment leak WRITES append-only rows in A that point at B's records, after
which A's own screening queue serves B's titles, abstracts and DOIs to A's
reviewers. Measured on `393ae61`: `201`, and the foreign title in the queue.

So the pattern was never "one omission"; it was a per-route habit with two
misses in it, and the second one was found only by re-deriving the list instead
of trusting this file's own summary of it. Both are pinned below.

Both halves of each case are asserted. A test that only shows the refusal cannot
distinguish "correctly scoped" from "this route is broken for everyone", so the
caller's own read — and the caller's own corpus — are exercised first and must
succeed.
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

# Not marked `integration`: this repository has no deselected lane, because a
# lane CI never runs is advertised coverage rather than real coverage.
pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

#: One tenant, deliberately. Two tenants would be caught by RLS and would prove
#: nothing about the missing review scope.
TENANT = tenant("tenant-scoping")
#: Two leads, because `decided_by_subject_id` is one of the fields that leaked.
#: If both reviews were resolved by the same subject, asserting that subject is
#: absent from the refusal would assert nothing.
LEAD_A = "lead-erin"
LEAD_B = "lead-frank"

#: Two records sharing a DOI, so the dedup baseline produces exactly one cluster
#: to resolve.
CORPUS = {
    "records": [
        {"title": "A trial of something", "doi": "10.1000/scoping-1", "year": 2021},
        {"title": "A Trial Of Something", "doi": "https://doi.org/10.1000/scoping-1", "year": 2021},
    ]
}


def headers(subject: str = LEAD_A, role: str = "lead") -> dict[str, str]:
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


def _review_with_a_resolved_cluster(client, label: str, subject: str, rationale: str) -> dict[str, str]:
    """An active review, deduped, with its one cluster merged.

    `subject` and `rationale` differ per review so the leak assertion can name
    the exact strings that must not cross.
    """
    created = client.post(
        "/api/reviews",
        json={"project_id": "scoping", "external_key": f"scope-{uuid.uuid4()}", "title": label},
        headers=headers(subject),
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
        headers=headers(subject),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]

    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers(subject))
    assert locked.status_code == 200, locked.text
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "scoping fixture"},
        headers=headers(subject),
    )
    assert activated.status_code == 200, activated.text

    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**headers(subject), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(subject))
    assert sealed.status_code == 201, sealed.text

    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", json={}, headers=headers(subject))
    assert run.status_code == 201, run.text
    clusters = client.get(f"/api/reviews/{review_id}/duplicate-clusters", headers=headers(subject))
    assert clusters.status_code == 200, clusters.text
    cluster = clusters.json()["items"][0]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "rationale": rationale,
            "survivor_record_id": cluster["members"][0]["review_record_id"],
        },
        headers=headers(subject),
    )
    assert resolved.status_code == 200, resolved.text
    return {"review_id": review_id, "cluster_id": cluster["cluster_id"], "rationale": rationale}


@pytest.fixture(scope="module")
def two_reviews(client):  # noqa: ANN201, ANN001
    a = _review_with_a_resolved_cluster(client, "Scoping review A", LEAD_A, "review A: same DOI, same trial")
    b = _review_with_a_resolved_cluster(client, "Scoping review B", LEAD_B, "review B: confidential rationale")
    return a, b


def test_the_caller_can_read_the_chain_of_their_own_cluster(client, two_reviews) -> None:
    """The positive control. Without it a broken route would look like a fix."""
    a, _ = two_reviews
    response = client.get(
        f"/api/reviews/{a['review_id']}/duplicate-clusters/{a['cluster_id']}/resolutions", headers=headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["rationale"] == a["rationale"]


def test_a_cluster_from_another_review_is_refused(client, two_reviews) -> None:
    """The defect: B's chain, fetched under A's id, by a caller entitled to A."""
    a, b = two_reviews
    response = client.get(
        f"/api/reviews/{a['review_id']}/duplicate-clusters/{b['cluster_id']}/resolutions", headers=headers()
    )
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


def test_the_refusal_carries_none_of_the_other_review_s_content(client, two_reviews) -> None:
    """A 404 that still ships the rationale is not a refusal."""
    a, b = two_reviews
    response = client.get(
        f"/api/reviews/{a['review_id']}/duplicate-clusters/{b['cluster_id']}/resolutions", headers=headers()
    )
    body = response.json()
    # `instance` echoes the path the caller supplied, cluster id included. That
    # is RFC 9457 and not a disclosure — what must be absent is B's DATA.
    body.pop("instance", None)
    leaked = json.dumps(body)
    for secret in (b["rationale"], LEAD_B, b["review_id"]):
        assert secret not in leaked, f"the refusal leaked {secret!r} from review B"


# ---------------------------------------------------------------------------
# The same hole, on a WRITE: `POST /assignments` and its body `corpus_version_id`
# ---------------------------------------------------------------------------

#: Titles that exist in exactly one of the two corpora each, so "whose record is
#: in this queue?" is answerable without joining anything — and so the absence of
#: one can be asserted alongside the presence of the other.
A_ONLY_TITLE = "Ordinary study that belongs to review A"
B_ONLY_TITLE = "Confidential trial that belongs to review B"


def _active_review_with_a_sealed_corpus(client, label: str, subject: str, records: list[dict]) -> dict[str, str]:
    """A review, activated against a locked protocol, with `records` sealed into it.

    Deliberately not reusing `_review_with_a_resolved_cluster`: that fixture
    seals the same two-record `CORPUS` into both reviews, so nothing in B's
    corpus is distinguishable from A's, and the leak assertion below would be
    unable to tell a served foreign record from a served local one.
    """
    created = client.post(
        "/api/reviews",
        json={"project_id": "scoping", "external_key": f"assign-{uuid.uuid4()}", "title": label},
        headers=headers(subject),
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
        headers=headers(subject),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]
    assert (
        client.post(
            f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers(subject)
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol_id, "rationale": "assignment scoping fixture"},
            headers=headers(subject),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps({"records": records}),
            headers={**headers(subject), "Content-Type": "application/octet-stream"},
        ).status_code
        == 201
    )
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(subject))
    assert sealed.status_code == 201, sealed.text
    stages = client.get(f"/api/reviews/{review_id}/stages", headers=headers(subject)).json()
    return {
        "review_id": review_id,
        "corpus_version_id": sealed.json()["corpus_version_id"],
        "stage_id": stages["items"][0]["stage_id"],
    }


@pytest.fixture(scope="module")
def two_corpora(client):  # noqa: ANN201, ANN001
    a = _active_review_with_a_sealed_corpus(
        client, "Assignment review A", LEAD_A, [{"title": A_ONLY_TITLE, "year": 2020}]
    )
    b = _active_review_with_a_sealed_corpus(
        client, "Assignment review B", LEAD_B, [{"title": B_ONLY_TITLE, "year": 2022}]
    )
    return a, b


def test_the_caller_can_seed_assignments_from_their_own_corpus_version(client, two_corpora) -> None:
    """The positive control. `corpus_version_id` is a supported parameter."""
    a, _ = two_corpora
    response = client.post(
        f"/api/reviews/{a['review_id']}/assignments",
        json={
            "stage_id": a["stage_id"],
            "reviewer_subject_ids": ["scoping-rev-1", "scoping-rev-2"],
            "corpus_version_id": a["corpus_version_id"],
        },
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["assignments_created"] == 2


def test_a_corpus_version_from_another_review_cannot_seed_assignments(client, two_corpora) -> None:
    """The defect: B's corpus, assigned inside A, by a lead entitled to A.

    Worse than the read above, because it is append-only: `screening_assignments`
    has no DELETE route, so once A holds assignments pointing at B's records the
    only thing that can be done about them is to notice.
    """
    a, b = two_corpora
    response = client.post(
        f"/api/reviews/{a['review_id']}/assignments",
        json={
            "stage_id": a["stage_id"],
            "reviewer_subject_ids": ["scoping-rev-3", "scoping-rev-4"],
            "corpus_version_id": b["corpus_version_id"],
        },
        headers=headers(),
    )
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


def test_the_assignment_refusal_names_neither_the_corpus_nor_the_other_review(client, two_corpora) -> None:
    """`instance` cannot help here — the corpus id travelled in the BODY.

    So unlike the resolution-chain refusal, there is no RFC 9457 echo to allow
    for: nothing about B may appear in this response at all.
    """
    a, b = two_corpora
    response = client.post(
        f"/api/reviews/{a['review_id']}/assignments",
        json={
            "stage_id": a["stage_id"],
            "reviewer_subject_ids": ["scoping-rev-5", "scoping-rev-6"],
            "corpus_version_id": b["corpus_version_id"],
        },
        headers=headers(),
    )
    rendered = response.text
    for secret in (b["corpus_version_id"], b["review_id"], LEAD_B):
        assert secret not in rendered, f"the refusal leaked {secret!r} from review B"


def test_review_bs_records_never_reach_review_as_screening_queue(client, two_corpora) -> None:
    """The consequence, asserted where a reviewer would actually see it.

    A status code says the write was refused; this says the leak is gone. On
    `393ae61` the assignment succeeded and `GET /screening/queue` in review A
    returned an item whose `title` was B's — `screening_queue` reads the record
    version straight off the assignment, so a foreign `review_record_id` is
    rendered with its title, abstract and DOI exactly like a local one.

    ## The absence assertion needs something to be present

    The first version of this test asserted only that B's title was missing from
    the queue — from a queue that is EMPTY BY CONSTRUCTION once the write is
    refused. It goes red on a revert, so it was a real gate, but it would have
    stayed green just as happily if `POST /assignments` had broken for everyone,
    or if the queue route had started returning `[]`. "The bad thing is absent"
    is not a finding when nothing at all is present.

    So the same reviewer is also assigned review A's OWN corpus, and A's record
    must appear in the same queue read. The queue then has to distinguish the two
    corpora rather than merely be empty.
    """
    a, b = two_corpora
    reviewers = ["scoping-rev-7", "scoping-rev-8"]

    refused = client.post(
        f"/api/reviews/{a['review_id']}/assignments",
        json={
            "stage_id": a["stage_id"],
            "reviewer_subject_ids": reviewers,
            "corpus_version_id": b["corpus_version_id"],
        },
        headers=headers(),
    )
    assert refused.status_code == 404, refused.text

    # The positive control, to the SAME reviewer on the SAME stage, so the
    # assertion below reads one queue containing exactly one of the two corpora.
    allowed = client.post(
        f"/api/reviews/{a['review_id']}/assignments",
        json={
            "stage_id": a["stage_id"],
            "reviewer_subject_ids": reviewers,
            "corpus_version_id": a["corpus_version_id"],
        },
        headers=headers(),
    )
    assert allowed.status_code == 201, allowed.text

    queue = client.get(
        f"/api/reviews/{a['review_id']}/screening/queue?stage_id={a['stage_id']}",
        headers=headers("scoping-rev-7", role="reviewer"),
    )
    assert queue.status_code == 200, queue.text
    titles = [item["title"] for item in queue.json()["items"]]
    assert A_ONLY_TITLE in titles, (
        f"review A's own record is missing from its own queue, so the absence of B's proves nothing: {titles}"
    )
    assert B_ONLY_TITLE not in titles, f"review B's record is being screened inside review A: {titles}"
