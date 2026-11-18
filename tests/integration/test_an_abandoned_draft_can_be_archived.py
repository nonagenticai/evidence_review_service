"""An abandoned draft review can be archived (`0028`), and nothing else loosened.

The database is append-only — every table carries a DELETE trigger — so before
`0028` a review created by mistake had no exit at all: `draft` led only to
`configured`, and `archived` was reachable only through `active -> closed`.

Driven through the real API against real Postgres:

* `POST /api/reviews/{id}/archive` (and the colon form `:archive`) from `draft`
  and from `closed` answer 200 and land on `archived`;
* the transition is recorded exactly like every other one — a contiguous
  history row naming the actor and the rationale, and a `review.archived.v1`
  outbox envelope at the same revision;
* `active` is a 409 naming the states archive applies to;
* another tenant's review is a 404, the same as GET;
* a missing or empty rationale is a 422;
* the list hides archived reviews unless asked;
* the trigger still refuses every pair it refused before — `draft -> active`
  and `draft -> closed` by direct INSERT, past the API.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlalchemy import text  # noqa: E402

from tests.tenants import tenant  # noqa: E402

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-archive-draft")
OTHER_TENANT = tenant("tenant-archive-draft-other")
LEAD = "archive-draft-lead"


def headers(tenant_id: str = TENANT, subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": tenant_id, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _create(client: TestClient, project: str = "archive-draft") -> str:
    created = client.post(
        "/api/reviews",
        json={"project_id": project, "external_key": f"ad-{uuid.uuid4()}", "title": "Abandoned"},
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["review_id"]


def _activate(client: TestClient, review_id: str) -> None:
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
    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers())
    assert locked.status_code == 200, locked.text
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )
    assert activated.status_code == 200, activated.text


def _state(client: TestClient, review_id: str) -> str:
    return client.get(f"/api/reviews/{review_id}", headers=headers()).json()["state"]


def _outbox(review_id: str) -> list[dict[str, Any]]:
    from actor import Actor, ActorRole
    from database import actor_scope

    actor = Actor(tenant_id=uuid.UUID(TENANT), subject_id=LEAD, role=ActorRole("lead"))
    with actor_scope(actor) as session:
        rows = session.execute(
            text(
                "SELECT event_type, aggregate_version, payload FROM domain_outbox "
                " WHERE aggregate_type = 'review' AND aggregate_id = :rid ORDER BY aggregate_version"
            ),
            {"rid": review_id},
        ).mappings()
        return [dict(r) for r in rows]


def test_a_draft_is_archived_with_a_history_row_and_an_event(client: TestClient) -> None:
    review_id = _create(client)

    archived = client.post(
        f"/api/reviews/{review_id}/archive", json={"rationale": "created by mistake"}, headers=headers()
    )
    assert archived.status_code == 200, archived.text
    body = archived.json()
    assert body["state"] == "archived"
    assert body["revision"] == 2
    assert archived.headers["ETag"] == '"2"'
    assert _state(client, review_id) == "archived"

    history = client.get(f"/api/reviews/{review_id}/state-history", headers=headers()).json()["items"]
    assert [(h["revision_number"], h["from_state"], h["to_state"]) for h in history] == [
        (1, None, "draft"),
        (2, "draft", "archived"),
    ]
    assert history[1]["rationale"] == "created by mistake"
    assert history[1]["actor_subject_id"] == LEAD

    events = _outbox(review_id)
    assert [(e["event_type"], e["aggregate_version"]) for e in events] == [
        ("review.created.v1", 1),
        ("review.archived.v1", 2),
    ]
    payload = events[1]["payload"]
    assert payload["from_state"] == "draft"
    assert payload["to_state"] == "archived"
    assert payload["state_event_id"] == history[1]["state_event_id"]


def test_the_colon_form_archives_a_draft_too(client: TestClient) -> None:
    """`:archive` and `/archive` are one handler under two URLs."""
    review_id = _create(client)
    archived = client.post(f"/api/reviews/{review_id}:archive", json={"rationale": "abandoned"}, headers=headers())
    assert archived.status_code == 200, archived.text
    assert archived.json()["state"] == "archived"


def test_a_closed_review_is_archived(client: TestClient) -> None:
    review_id = _create(client)
    _activate(client, review_id)
    closed = client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=headers())
    assert closed.status_code == 200, closed.text

    archived = client.post(f"/api/reviews/{review_id}/archive", json={"rationale": "filed"}, headers=headers())
    assert archived.status_code == 200, archived.text
    assert archived.json()["state"] == "archived"
    assert _outbox(review_id)[-1]["event_type"] == "review.archived.v1"
    assert _outbox(review_id)[-1]["payload"]["from_state"] == "closed"


def test_an_active_review_is_a_409_and_does_not_move(client: TestClient) -> None:
    review_id = _create(client)
    _activate(client, review_id)
    before = len(_outbox(review_id))

    refused = client.post(f"/api/reviews/{review_id}/archive", json={"rationale": "skip close"}, headers=headers())
    assert refused.status_code == 409, refused.text
    problem = refused.json()
    assert problem["code"] == "state_conflict"
    assert problem["current_state"] == "active"
    assert problem["allowed_from"] == ["draft", "closed"]
    assert _state(client, review_id) == "active"
    assert len(_outbox(review_id)) == before, "a refused archive must not emit"


def test_an_archived_review_cannot_be_archived_again(client: TestClient) -> None:
    review_id = _create(client)
    assert (
        client.post(f"/api/reviews/{review_id}/archive", json={"rationale": "a"}, headers=headers()).status_code == 200
    )
    again = client.post(f"/api/reviews/{review_id}/archive", json={"rationale": "b"}, headers=headers())
    assert again.status_code == 409
    assert again.json()["current_state"] == "archived"


def test_another_tenants_review_is_a_404(client: TestClient) -> None:
    review_id = _create(client)
    foreign = client.post(
        f"/api/reviews/{review_id}/archive", json={"rationale": "not mine"}, headers=headers(OTHER_TENANT)
    )
    assert foreign.status_code == 404, foreign.text
    assert client.get(f"/api/reviews/{review_id}", headers=headers(OTHER_TENANT)).status_code == 404
    assert _state(client, review_id) == "draft"


def test_an_unknown_review_is_a_404(client: TestClient) -> None:
    missing = client.post(f"/api/reviews/{uuid.uuid4()}/archive", json={"rationale": "x"}, headers=headers())
    assert missing.status_code == 404


@pytest.mark.parametrize("body", [{}, {"rationale": ""}, {"rationale": None}])
def test_a_rationale_is_required(client: TestClient, body: dict[str, Any]) -> None:
    review_id = _create(client)
    refused = client.post(f"/api/reviews/{review_id}/archive", json=body, headers=headers())
    assert refused.status_code == 422, refused.text
    assert _state(client, review_id) == "draft"


def test_the_list_hides_archived_reviews_unless_asked(client: TestClient) -> None:
    project = f"archive-list-{uuid.uuid4()}"
    kept = _create(client, project)
    gone = _create(client, project)
    assert client.post(f"/api/reviews/{gone}/archive", json={"rationale": "x"}, headers=headers()).status_code == 200

    default = client.get("/api/reviews", params={"project_id": project}, headers=headers()).json()
    assert [r["review_id"] for r in default["items"]] == [kept]
    assert default["count"] == 1

    everything = client.get(
        "/api/reviews", params={"project_id": project, "include_archived": "true"}, headers=headers()
    ).json()
    assert {r["review_id"] for r in everything["items"]} == {kept, gone}
    assert {r["review_id"]: r["state"] for r in everything["items"]}[gone] == "archived"


@pytest.mark.parametrize("to_state", ["active", "closed", "paused"])
def test_the_trigger_still_refuses_a_draft_skipping_the_lifecycle(client: TestClient, to_state: str) -> None:
    """Past the API: a direct INSERT is what the trigger exists to stop."""
    from sqlalchemy.exc import DBAPIError

    from actor import Actor, ActorRole
    from database import actor_scope

    review_id = _create(client)
    actor = Actor(tenant_id=uuid.UUID(TENANT), subject_id=LEAD, role=ActorRole("lead"))
    with pytest.raises(DBAPIError, match=f"illegal lifecycle transition draft -> {to_state}"):
        with actor_scope(actor) as session:
            session.execute(
                text(
                    "INSERT INTO review_state_history "
                    "  (state_event_id, review_id, tenant_id, revision_number, from_state, to_state, "
                    "   rationale, actor_subject_id) "
                    "VALUES (:id, :rid, :tid, 2, 'draft', :to, 'direct insert', :who)"
                ),
                {"id": str(uuid.uuid4()), "rid": review_id, "tid": TENANT, "to": to_state, "who": LEAD},
            )
    assert _state(client, review_id) == "draft"
