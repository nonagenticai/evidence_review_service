"""`GET /api/review-jobs/{id}` and `POST /api/review-jobs/{id}:cancel` (`FND-006`).

`14-…`: "All non-terminal import, deduplication, prioritization, snapshot and
export jobs expose `GET /review-jobs/{job_id}` and
`POST /review-jobs/{job_id}:cancel`. Cancellation is cooperative,
input-hash/idempotency scoped, and cannot roll back an already committed domain
result."

The service-level semantics are proven in
`tests/integration/test_two_workers_and_a_reaper.py`. What is asserted HERE is
only what the HTTP layer adds, because that is what a service-level test cannot
see:

* the `:cancel` route is reachable at all. It is the first colon-suffixed action
  in this service, and `/{job_id}:cancel` relies on Starlette's `[^/]+` path
  converter backtracking so the literal suffix can match. If that assumption were
  wrong the route would 404 or swallow `:cancel` into the uuid, and every
  service-level test would still pass;
* `state` on the wire is always one of the six, with cancellation reported as a
  separate boolean — `13-…:§7.1`, "`cancel_requested` is not an extra public
  state";
* a terminal job answers `:cancel` with `200` and its outcome rather than an
  error (`§7.3`);
* the authorisation is the review's: a reviewer who did not submit the job is
  refused, and the same reviewer reading their OWN job is not.

## The last one is the point of the file

`_authorize` in the router grants access to the submitting subject as well as to
the lead. That is one `or` away from granting it to everyone, and a test that
only proved "a stranger is refused" would pass against a router that refused
the submitter too — leaving a reviewer able to start an import and unable to ask
whether it finished, which reads as a hang rather than as a permission error.
So both arms are asserted.
"""

from __future__ import annotations

import os
import sys
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

#: A tenant per test, and per run — the same lesson
#: `tests/integration/test_two_workers_and_a_reaper.py` records at length.
#: `claim_next` takes the OLDEST queued row in the tenant, so with one shared
#: tenant the two tests below that claim a job both claimed the FIRST test's job
#: instead of their own. Nothing truncates between tests, so a stable label has
#: the same effect across runs.
_RUN = uuid.uuid4().hex[:12]


def _t(label: str) -> str:
    return tenant(f"tenant-review-jobs-api-{label}-{_RUN}")


LEAD = "review-jobs-lead"
SUBMITTER = "review-jobs-submitter"
BYSTANDER = "review-jobs-bystander"

HASH = "sha256:" + "c" * 64


def headers(tenant_id: str, subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": tenant_id, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _actor(tenant_id: str, subject: str, role: str = "lead"):  # noqa: ANN202
    from actor import Actor, ActorRole

    return Actor(tenant_id=uuid.UUID(tenant_id), subject_id=subject, role=ActorRole(role))


def _review(client: TestClient, tenant_id: str) -> str:
    response = client.post(
        "/api/reviews",
        json={
            "project_id": "review-jobs-api",
            "external_key": f"jobs-api-{uuid.uuid4()}",
            "title": "A job can be asked about",
        },
        headers=headers(tenant_id),
    )
    assert response.status_code == 201, response.text
    return response.json()["review_id"]


def _enqueue(review_id: str, tenant_id: str, *, submitter: str = SUBMITTER, job_type: str = "export") -> str:
    """Enqueue through the SERVICE, not through HTTP.

    There is deliberately no route that creates a job: `FND-006` builds the
    substrate and each producer package (`ER-002/003/007/008/009`) adds its own
    `202`-returning endpoint with its conversion. A test that invented one here
    would be testing a route this service does not have.
    """
    from application.services import jobs as svc
    from database import actor_scope

    actor = _actor(tenant_id, submitter, role="reviewer")
    with actor_scope(actor) as session:
        job = svc.enqueue(
            session,
            actor,
            review_id=uuid.UUID(review_id),
            job_type=job_type,
            command_hash=HASH,
        )
        return str(job.job_id)


def test_the_lead_reads_a_job_and_its_wire_shape_is_the_six_states(client) -> None:  # noqa: ANN001
    tid = _t("read")
    review_id = _review(client, tid)
    job_id = _enqueue(review_id, tid)

    response = client.get(f"/api/review-jobs/{job_id}", headers=headers(tid))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["job_id"] == job_id
    assert body["review_id"] == review_id
    assert body["job_type"] == "export"
    assert body["state"] == "queued"
    assert body["cancel_requested"] is False
    assert body["cancel_requested_at"] is None
    assert body["attempt"] == 0
    assert body["command_hash"] == HASH
    assert body["waiting"] is None
    assert body["attempts"] == []


def test_cancelling_a_running_job_leaves_its_state_alone(client) -> None:  # noqa: ANN001
    """`§7.1`: the flag is reported beside `state`, and does not become one."""
    from application.services import jobs as svc
    from database import actor_scope

    tid = _t("cancel-running")
    review_id = _review(client, tid)
    job_id = _enqueue(review_id, tid)

    actor = _actor(tid, LEAD)
    with actor_scope(actor) as session:
        claimed = svc.claim_next(session, worker_id="api-worker", tenant_id=actor.tenant_id)
        assert claimed is not None and str(claimed.job_id) == job_id

    response = client.post(f"/api/review-jobs/{job_id}:cancel", headers=headers(tid))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["state"] == "running", "cancellation intent is not a seventh state"
    assert body["cancel_requested"] is True
    assert body["cancel_requested_at"] is not None
    assert [a["attempt_number"] for a in body["attempts"]] == [1]
    assert body["attempts"][0]["outcome"] is None

    # The route is idempotent: asking twice does not move the timestamp, because
    # a caller retrying a cancel must not appear to have cancelled twice.
    again = client.post(f"/api/review-jobs/{job_id}:cancel", headers=headers(tid))
    assert again.status_code == 200
    assert again.json()["cancel_requested_at"] == body["cancel_requested_at"]

    # ⚠️ That equality is a STRING comparison, and it is what caught the defect
    # `_iso` now fixes: the first response rendered the instant the writing
    # session held (UTC) and the second rendered the same instant as Postgres
    # returned it (the session TimeZone, `+02:00` locally). Both were valid
    # ISO-8601 and unequal, so a client caching on this field would have seen a
    # change that never happened. Pinned directly rather than left implicit,
    # because the two-call shape above only catches it by accident.
    for field in ("cancel_requested_at", "queued_at", "started_at"):
        assert body[field] is None or body[field].endswith("Z"), (
            f"{field} must be trailing-Z UTC, got {body[field]!r}. "
            "`urn:datacompany:stable-identity:1`'s timestamp primitive admits only that form, "
            "and this router used to emit `+00:00` — a real fix in the wrong direction."
        )


def test_cancelling_a_finished_job_returns_its_outcome_not_an_error(client) -> None:  # noqa: ANN001
    """`§7.3`: "cancellation that arrives after terminal commit returns the terminal outcome"."""
    from application.services import jobs as svc
    from database import actor_scope

    tid = _t("cancel-finished")
    review_id = _review(client, tid)
    job_id = _enqueue(review_id, tid)

    actor = _actor(tid, LEAD)
    with actor_scope(actor) as session:
        svc.claim_next(session, worker_id="api-worker", tenant_id=actor.tenant_id)
    with actor_scope(actor) as session:
        svc.finish(
            session,
            uuid.UUID(job_id),
            worker_id="api-worker",
            outcome="succeeded",
            result_summary={"records": 7},
        )

    response = client.post(f"/api/review-jobs/{job_id}:cancel", headers=headers(tid))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    assert body["cancel_requested"] is False
    assert body["result"]["summary"] == {"records": 7}
    assert body["attempts"][0]["outcome"] == "succeeded"


def test_the_submitter_may_read_their_own_job_and_a_bystander_may_not(client) -> None:  # noqa: ANN001
    """Both arms. See the module docstring: one of them alone proves nothing."""
    tid = _t("authz")
    review_id = _review(client, tid)
    job_id = _enqueue(review_id, tid, submitter=SUBMITTER)

    mine = client.get(f"/api/review-jobs/{job_id}", headers=headers(tid, SUBMITTER, role="reviewer"))
    assert mine.status_code == 200, mine.text
    assert mine.json()["job_id"] == job_id

    theirs = client.get(f"/api/review-jobs/{job_id}", headers=headers(tid, BYSTANDER, role="reviewer"))
    assert theirs.status_code == 403, theirs.text
    assert theirs.json()["title"] == "Forbidden"

    stopping = client.post(f"/api/review-jobs/{job_id}:cancel", headers=headers(tid, BYSTANDER, role="reviewer"))
    assert stopping.status_code == 403, stopping.text


def test_another_tenants_job_is_not_found_rather_than_forbidden(client) -> None:  # noqa: ANN001
    """404, not 403 — see `jobs.load_job`.

    A 403 would confirm that a job with that id exists somewhere, which over a
    guessable identifier space is a cross-tenant existence oracle.
    """
    tid = _t("isolation")
    review_id = _review(client, tid)
    job_id = _enqueue(review_id, tid)

    response = client.get(f"/api/review-jobs/{job_id}", headers=headers(_t("isolation-stranger"), LEAD))
    assert response.status_code == 404, response.text

    # The control: the same id IS 200 for its own tenant, so the 404 above is
    # about isolation and not about a route that never works.
    assert client.get(f"/api/review-jobs/{job_id}", headers=headers(tid)).status_code == 200


def test_an_unknown_job_id_is_a_typed_404(client) -> None:  # noqa: ANN001
    response = client.get(f"/api/review-jobs/{uuid.uuid4()}", headers=headers(_t("unknown")))
    assert response.status_code == 404, response.text
    assert response.json()["title"] == "Not Found"
