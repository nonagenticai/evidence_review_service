"""The screening queue serves the abstract, and NOTHING asserted it.

`screening_queue` builds each item with `"abstract": version.abstract if version
else None` (`screening.py:609`). That is correct and it was completely
unprotected: measured on `4349131` by blanking that one expression to `None` and
running every suite that reads the queue —

    tests/api/test_the_queue_serves_the_sealed_corpus.py
    tests/api/test_the_queue_shows_the_current_decision.py
    tests/api/test_the_queue_counts_the_whole_stage.py
    tests/security/test_cross_review_scoping.py
    tests/integration/test_e2e_acceptance.py

    64 passed, 1 warning in 235.90s

Sixty-four tests, including the whole 40-test `ER-002` acceptance suite and a
cross-review scoping file whose own docstring says this route serves "titles,
abstracts and DOIs", and not one of them noticed that the queue had stopped
serving any abstract at all. The field could be deleted by an unrelated edit and
the suite would stay green.

That matters more than a missing assertion usually does, because the abstract is
not decoration on this route. A title/abstract stage is *named* for the two
fields a reviewer judges on, and a queue that serves only the title asks them to
screen on half the evidence — which produces a recorded, sealed, exported
judgement rather than an error anybody can see. `fr` renders this field.

## Why this asserts the title and the DOI as well

A regression that blanks the abstract and a regression that blanks the whole
record version — `corpus_member_versions` returning nothing, so `version` is
`None` and every content field falls to `null` together — are different defects
with different causes, and an abstract-only assertion reports them identically.
Asserting the neighbours is what separates "the abstract stopped being served"
from "the queue stopped resolving content".
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

TENANT = tenant("tenant-queue-abstract")
LEAD = "queue-abstract-lead"
REVIEWER = "queue-abstract-reviewer"
OTHER = "queue-abstract-other"

#: Distinctive on purpose. A substring check against a fixed sentence would pass
#: on a route that echoed the title, and this has to be a value only the abstract
#: column can be holding.
ABSTRACT = "Randomised, double-blind, placebo-controlled; 412 participants over 18 months; primary endpoint HbA1c."


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


@pytest.fixture(scope="module")
def queue(client):  # noqa: ANN001, ANN201
    """One review, two records — one WITH an abstract and one without.

    Both in the same sealed corpus and the same page, because "the queue serves
    the abstract" and "an absent abstract is `null` rather than a missing key"
    are two halves of one contract and a client renders them from one response.
    """
    marker = uuid.uuid4().hex[:8]
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "queue-abstract", "external_key": f"qa-{uuid.uuid4()}", "title": "Queue abstract"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(),
    ).json()["protocol_version_id"]
    assert (
        client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers()).status_code == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol, "rationale": "go"},
            headers=headers(),
        ).status_code
        == 200
    )
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {
                        "title": f"A trial {marker}",
                        "abstract": ABSTRACT,
                        "doi": f"10.1000/{marker}-with",
                        "year": 2021,
                    },
                    # No `abstract` key at all — a conference abstract or a
                    # citation-only export, which is ordinary input.
                    {"title": f"A poster {marker}", "doi": f"10.1000/{marker}-without", "year": 2022},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code == 201
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assert (
        client.post(
            f"/api/reviews/{review_id}/assignments",
            json={"stage_id": stage_id, "round_number": 1, "reviewer_subject_ids": [REVIEWER, OTHER]},
            headers=headers(),
        ).status_code
        == 201
    )
    got = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert len(body["items"]) == 2, body
    return {"review_id": review_id, "stage_id": stage_id, "body": body, "marker": marker}


def _item(queue: dict, suffix: str) -> dict:
    doi = f"10.1000/{queue['marker']}-{suffix}"
    matches = [i for i in queue["body"]["items"] if i["doi"] == doi]
    assert len(matches) == 1, f"the fixture record {doi} is not in the queue: {queue['body']}"
    return matches[0]


def test_the_queue_serves_the_abstract(client, queue) -> None:  # noqa: ARG001
    """`items[].abstract` — the field `fr` renders beside the title and the DOI.

    Asserted by EQUALITY against the imported text, not by presence of the key. A
    key whose value is `None` is exactly what the defect this file was written
    for produces, and `"abstract" in item` is true for it.
    """
    item = _item(queue, "with")

    assert item["abstract"] == ABSTRACT, (
        "the screening queue does not serve the abstract, so a reviewer screening a title/abstract "
        f"stage is judging on the title alone: {item}"
    )
    # The neighbours. See the module docstring: this is what separates a blanked
    # abstract from a queue that stopped resolving record content at all.
    assert item["title"] == f"A trial {queue['marker']}", item
    assert item["doi"] == f"10.1000/{queue['marker']}-with", item
    assert item["review_record_version_id"], (
        "the item carries no record version, so every content field above is null for a reason that "
        f"has nothing to do with the abstract: {item}"
    )


def test_a_record_with_no_abstract_serves_null_not_a_missing_key(client, queue) -> None:  # noqa: ARG001
    """An absent abstract is `null`, and the key is still there.

    Citation-only exports are ordinary input and roughly half of what a first
    pass imports. A client that reads `item.abstract` off a key that is sometimes
    absent gets `undefined` and renders it, which is how "undefined" reaches a
    reviewer's screen as the evidence they are judging.
    """
    item = _item(queue, "without")

    assert "abstract" in item, f"the key is omitted for a record with no abstract: {item}"
    assert item["abstract"] is None, item
    # Not a blank record: the rest of it came through.
    assert item["title"] == f"A poster {queue['marker']}", item


def test_the_abstract_carries_no_decision_fact_with_it(client, queue) -> None:  # noqa: ARG001
    """L3 is unchanged. Adding record CONTENT must not add a decision fact.

    `screening_queue`'s contract is that the only decision fact in the payload is
    the caller's own `my_decision`. This asserts the queue that serves the
    abstract is still that queue — deliberately by naming the fields L3 refuses
    (`screening.py`'s module docstring lists them), because those are the ones a
    well-meaning "show the reviewer more context" change adds.
    """
    for item in queue["body"]["items"]:
        for forbidden in ("decisions_so_far", "others_pending", "n_of_2", "reviewer_subject_id", "dispositions"):
            assert forbidden not in item, f"L3 field `{forbidden}` is being served on the queue: {item}"
        assert item["my_decision"] is None, f"this reviewer has decided nothing, so their own decision is null: {item}"
    body = queue["body"]
    assert "decided_by_me" in body and body["decided_by_me"] == 0, body
