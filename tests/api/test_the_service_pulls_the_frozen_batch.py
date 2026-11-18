"""The corpus arrives from discovery-engine, not from a browser.

Until this route existed, Screening's corpus was bytes the tab built:
`fr` posted `JSON.stringify({records: toScreeningRecords(sessionSnapshot)})` and
this service digested and sealed that. `import_batches.raw_payload_digest` — the
number the corpus manifest and `identified_total` rest on — therefore named a
document constructed outside the trust boundary.

These tests exercise the whole hop against a **real HTTP server** rather than a
patched `fetch_review_export`, because three of the four things that can go
wrong here are transport facts: a truncated response, a 404 that means
"visibility", and an upstream that is simply down. A monkeypatched client would
report green on all three.

`test_a_substituted_record_is_refused` is THE CONTROL. It serves a document with
one title rewritten while the caller declares the digest of the ORIGINAL — the
exact shape of "the studio showed the human batch A and something else arrived"
— and asserts both the refusal and that nothing was written.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

TENANT = tenant("tenant-pulled-batch")
LEAD = "pulled-batch-lead"
SESSION_ID = "session-under-test"


def headers() -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": LEAD, "X-Actor-Role": "lead"}


def _digest(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _document(*, session_id: str = SESSION_ID, title: str = "Tocilizumab in severe COVID-19") -> bytes:
    """The shape discovery-engine's `build_document` emits: records at TOP level."""
    return json.dumps(
        {
            "document_version": 2,
            "session_id": session_id,
            "session_name": "pulled batch",
            "records": [
                {
                    "id": "paper-1",
                    "title": title,
                    "abstract": "A randomised trial.",
                    "doi": "10.1000/pulled.1",
                    "publication_year": 2024,
                    "authors": ["Ada Lovelace"],
                    "external_ids": {"pubmed_id": "12345678"},
                }
            ],
            "record_count": 1,
            "search_strategy": [],
            "prisma": {"raw_total": 1, "deduped_total": None},
        },
        sort_keys=True,
    ).encode("utf-8")


class _FakeDiscovery:
    """A real socket. Behaviour is set per-test; every request is recorded."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b""
        #: What to put in `X-Document-Digest`. `None` omits the header entirely
        #: — the shape an older discovery-engine would have.
        self.digest_header: str | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []

    #: Sentinel for "send the digest of whatever body you were given". `None`
    #: has to mean OMIT THE HEADER — that is a real upstream shape and the one
    #: `test_an_upstream_that_asserts_nothing` exercises.
    AUTO = "__auto__"

    def serve(self, body: bytes, *, digest_header: str | None = AUTO, status: int = 200) -> None:
        self.body = body
        self.digest_header = _digest(body) if digest_header == self.AUTO else digest_header
        self.status = status


@pytest.fixture(scope="module")
def fake_discovery():  # noqa: ANN201
    state = _FakeDiscovery()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            state.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            if state.digest_header is not None:
                self.send_header("X-Document-Digest", state.digest_header)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *args: object) -> None:  # keep the suite output readable
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def client(fake_discovery):  # noqa: ANN001, ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DISCOVERY_ENGINE_URL"] = fake_discovery.base_url
    # `get_settings` is `lru_cache`d, so a value set after the first call would
    # be ignored — and the failure would look like "the client fetched the
    # production URL", which is a confusing way to learn about a cache.
    from config import get_settings

    get_settings.cache_clear()
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
    get_settings.cache_clear()


def _new_review(client) -> str:  # noqa: ANN001
    created = client.post(
        "/api/reviews",
        json={"project_id": "pulled", "external_key": f"pulled-{uuid.uuid4()}", "title": "Pulled batch"},
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    return created.json()["review_id"]


@pytest.fixture()
def review_id(client) -> str:  # noqa: ANN001
    return _new_review(client)


def _pull(client, review_id: str, *, digest: str, session_id: str = SESSION_ID, batch: str = "batch-1"):  # noqa: ANN001, ANN202
    return client.post(
        f"/api/reviews/{review_id}/imports/from-discovery",
        json={"session_id": session_id, "batch_id": batch, "expected_digest": digest},
        headers=headers(),
    )


def test_the_pulled_batch_is_sealed_under_discovery_engines_own_digest(client, fake_discovery, review_id):  # noqa: ANN001
    """The happy path, and the property that makes the chain checkable.

    `raw_payload_digest` on the import batch comes out EQUAL to the digest
    discovery-engine returned when the batch was frozen — one hash of one byte
    string on both sides. Anyone holding the receipt and the import can check
    that they name the same document without asking either service to vouch for
    itself.
    """
    body = _document()
    fake_discovery.serve(body)

    response = _pull(client, review_id, digest=_digest(body))

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["accepted_count"] == 1, payload
    assert payload["raw_payload_digest"] == _digest(body)

    listed = client.get(f"/api/reviews/{review_id}/imports", headers=headers()).json()
    (batch,) = listed["items"]
    source_ref = batch["source_ref"]
    assert source_ref["fetch_mode"] == "discovery_engine_pull"
    assert source_ref["review_export_batch_id"] == "batch-1"
    assert source_ref["fetched_document_digest"] == _digest(body)
    # The harvested provenance is merged over the declared one and must survive
    # alongside it — two facts, two keys.
    assert source_ref["discovery_session_id"] == SESSION_ID


def test_a_substituted_record_is_refused_and_nothing_is_written(client, fake_discovery, review_id):  # noqa: ANN001
    """THE CONTROL — the batch shown and the batch served are different documents."""
    shown = _document(title="Tocilizumab in severe COVID-19")
    served = _document(title="A study nobody screened")
    assert shown != served
    fake_discovery.serve(served)

    response = _pull(client, review_id, digest=_digest(shown))

    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["code"] == "upstream_digest_mismatch"
    assert problem["expected_digest"] == _digest(shown)
    assert problem["computed_digest"] == _digest(served)

    listed = client.get(f"/api/reviews/{review_id}/imports", headers=headers()).json()
    assert listed["count"] == 0, "a refused pull must write no import batch"
    records = client.get(f"/api/reviews/{review_id}/records", headers=headers()).json()
    assert records["total"] == 0


def test_a_truncated_response_is_blamed_on_the_transport_not_on_the_caller(client, fake_discovery, review_id):  # noqa: ANN001
    """Both digests disagree, and the upstream one is the precise diagnosis.

    The caller's `expected_digest` was RIGHT here. Reporting this as "you named
    the wrong batch" would send whoever reads it looking at the studio.
    """
    body = _document()
    fake_discovery.serve(body[: len(body) // 2], digest_header=_digest(body))

    response = _pull(client, review_id, digest=_digest(body))

    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["code"] == "upstream_digest_mismatch"
    assert problem["asserted_digest"] == _digest(body)
    assert "expected_digest" not in problem
    assert "truncated" in problem["detail"]


def test_a_batch_from_another_session_is_refused(client, fake_discovery, review_id):  # noqa: ANN001
    """The document says which session it is. A disagreement is not imported."""
    body = _document(session_id="some-other-session")
    fake_discovery.serve(body)

    response = _pull(client, review_id, digest=_digest(body))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unprocessable_state"
    listed = client.get(f"/api/reviews/{review_id}/imports", headers=headers()).json()
    assert listed["count"] == 0


def test_a_batch_the_tenant_cannot_see_is_a_404_that_names_visibility(client, fake_discovery, review_id):  # noqa: ANN001
    fake_discovery.serve(b'{"detail":"no such batch"}', status=404)

    response = _pull(client, review_id, digest=_digest(b"anything at all that is 64 hex long enough"))

    assert response.status_code == 404, response.text
    assert "visibility" in response.json()["detail"]


def test_an_unreachable_discovery_engine_is_503_and_says_retry(client, review_id, monkeypatch):  # noqa: ANN001
    """Down upstream, and the caller did nothing wrong.

    Pointed at a port nothing listens on, rather than at a stopped fixture
    server: the refusal has to come from a connect failure, which is what
    actually happens when the pod is rolling.
    """
    from config import get_settings

    monkeypatch.setenv("DISCOVERY_ENGINE_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    try:
        response = _pull(client, review_id, digest="sha256:" + "0" * 64)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "service_unavailable"
    finally:
        get_settings.cache_clear()


def test_the_tenant_on_the_hop_is_the_actors_and_never_the_bodys(client, fake_discovery, review_id):  # noqa: ANN001
    """A caller cannot name someone else's tenant and have us fetch it."""
    body = _document()
    fake_discovery.serve(body)
    before = len(fake_discovery.requests)

    _pull(client, review_id, digest=_digest(body))

    path, sent = fake_discovery.requests[before]
    assert path == f"/api/sessions/{SESSION_ID}/review-export/batch-1"
    assert sent["x-tenant-id"] == TENANT
    # No licence configured in this module: today's request, no Authorization.
    assert "authorization" not in sent


def test_the_service_licence_rides_beside_the_actors_tenant(client, fake_discovery, review_id, monkeypatch):  # noqa: ANN001
    """`DISCOVERY_ENGINE_LICENSE_TOKEN` reaches the wire through the ROUTE, on-behalf shaped.

    The licence authenticates this service; the plain `X-Tenant-Id` still names
    the ACTOR's tenant. The token is minted per run — no literal for
    detect-secrets to learn.
    """
    import secrets

    from config import get_settings

    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("DISCOVERY_ENGINE_LICENSE_TOKEN", token)
    get_settings.cache_clear()
    try:
        body = _document()
        fake_discovery.serve(body)
        before = len(fake_discovery.requests)

        response = _pull(client, review_id, digest=_digest(body))

        assert response.status_code == 201, response.text
        _, sent = fake_discovery.requests[before]
        assert sent["authorization"] == f"Bearer {token}"
        assert sent["x-tenant-id"] == TENANT
    finally:
        monkeypatch.delenv("DISCOVERY_ENGINE_LICENSE_TOKEN")
        get_settings.cache_clear()


def test_pulling_the_same_batch_twice_is_a_replay_not_a_second_corpus(client, fake_discovery, review_id):  # noqa: ANN001
    """Identical bytes into the same review are the same import.

    This is the property `discovery-engine` #58 had to remove `taken_at` to
    preserve: two takings of an unchanged corpus are the same document, so a
    re-import must not double every record.
    """
    body = _document()
    fake_discovery.serve(body)

    first = _pull(client, review_id, digest=_digest(body))
    second = _pull(client, review_id, digest=_digest(body), batch="batch-2")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is True
    records = client.get(f"/api/reviews/{review_id}/records", headers=headers()).json()
    assert records["total"] == 1

    # PINNED, not incidental: the provenance keeps naming the FIRST taking even
    # though the caller cited `batch-2`. Both batches are the same document by
    # construction, so there is one corpus and one import — but the citation the
    # second caller used is not recorded anywhere. Recording it needs a place to
    # put it (a batch has one `source_ref`), which is a schema change and not
    # this route's to make. If that is ever built, this assertion is the one
    # that goes red, and it should be updated rather than deleted.
    assert second.json()["source_ref"]["review_export_batch_id"] == "batch-1"


def test_an_upstream_that_asserts_nothing_is_still_checked_against_the_caller(client, fake_discovery, review_id):  # noqa: ANN001
    """No `X-Document-Digest` is a fact about the upstream, not evidence of tampering.

    An older discovery-engine sends no such header. Treating its absence as a
    mismatch would refuse every batch it ever froze; treating it as agreement
    would be a second, unearned claim. It is skipped, and the caller's declared
    digest is still the gate.
    """
    body = _document()
    fake_discovery.serve(body, digest_header=None)

    response = _pull(client, review_id, digest=_digest(body))
    assert response.status_code == 201, response.text

    fake_discovery.serve(_document(title="something else"), digest_header=None)
    refused = _pull(client, _new_review(client), digest=_digest(body))
    assert refused.status_code == 422
    assert refused.json()["code"] == "upstream_digest_mismatch"


def test_a_session_id_cannot_choose_the_path_on_the_engine(client, fake_discovery, review_id):  # noqa: ANN001
    """An identifier is an identifier, not a path fragment.

    `session_id` and `batch_id` come off the request body and used to be
    interpolated raw. Measured before the fix: `"x/../../../health"` builds
    `.../api/sessions/x/../../../health/review-export/y`, and **httpx normalises
    the dot segments before sending**, so what actually left this pod was
    `GET /health/review-export/y` carrying the caller's `X-Tenant-Id`. The
    caller chose the path on discovery-engine's host.

    The damage was bounded — one host, GET only, and a response that is not a
    session export is refused rather than imported — but that bound is a
    property of discovery-engine's OTHER endpoints, not of this code, and it
    would have to be re-established every time one of them changed.

    Asserted on the path the fake server actually received, not on a helper's
    return value: the normalisation happened inside httpx, so anything that
    checks the string we built would have passed while the wire disagreed.
    """
    body = _document()
    fake_discovery.serve(body)
    before = len(fake_discovery.requests)

    _pull(client, review_id, digest=_digest(body), session_id="x/../../../health")

    path, _ = fake_discovery.requests[before]
    assert path == "/api/sessions/x%2F..%2F..%2F..%2Fhealth/review-export/batch-1"
    assert not path.startswith("/health")


def test_a_caller_with_no_authority_never_reaches_the_engine(client, fake_discovery, review_id):  # noqa: ANN001
    """The gate is held BEFORE the request leaves this pod, not after.

    `import_payload` holds `require_review_lead_for`, and it runs after the
    fetch. So every reviewer in the tenant — everyone `load_review` lets
    through — could make this service call discovery-engine for any
    (session, batch) pair they named, and read the answer off the status code:
    `404` no such batch on that session, `422` it exists and hashes to something
    else, `201` it exists and they guessed the digest. An oracle over other
    people's frozen corpora, plus a request amplifier, both reachable with no
    authority over the review at all.

    Asserted on the REQUEST COUNT, not only on the 403. A version that refused
    the caller after fetching would return the same 403 and still have made the
    call — which is the whole defect.
    """
    body = _document()
    fake_discovery.serve(body)
    before = len(fake_discovery.requests)

    response = client.post(
        f"/api/reviews/{review_id}/imports/from-discovery",
        json={"session_id": SESSION_ID, "batch_id": "batch-1", "expected_digest": _digest(body)},
        headers={"X-Tenant-Id": TENANT, "X-Subject-Id": "not-the-lead", "X-Actor-Role": "reviewer"},
    )

    assert response.status_code == 403, response.text
    assert len(fake_discovery.requests) == before, "a refused caller must not reach discovery-engine"


def test_a_redirect_is_not_reported_as_the_callers_digest_being_wrong(client, fake_discovery, review_id):  # noqa: ANN001
    """A 3xx used to fall through to the read and be blamed on the studio.

    `follow_redirects` is off on purpose — a redirect is a chance to be handed
    bytes from an address this code never named. But only 4xx and 5xx were
    recognised, so a 3xx reached the body read, httpx handed back an empty body,
    `b""` got digested, and the caller was told *"the batch fetched does not
    hash to the expected_digest you declared"* — an accusation pointed at the
    studio for something an ingress did.
    """
    body = _document()
    fake_discovery.serve(b"", status=302)

    response = _pull(client, review_id, digest=_digest(body))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unprocessable_state"
    assert "Redirects are not followed" in response.json()["detail"]


def test_a_bare_hex_expected_digest_is_named_as_a_mistake(client, review_id):  # noqa: ANN001
    """`sha256:`-prefixed is the only spelling either service writes."""
    response = client.post(
        f"/api/reviews/{review_id}/imports/from-discovery",
        json={"session_id": SESSION_ID, "batch_id": "batch-1", "expected_digest": "0" * 64},
        headers=headers(),
    )
    assert response.status_code == 422, response.text
    # problem+json with a branchable code, NOT FastAPI's `{"detail": [...]}`.
    # The kag gateway contract is that the body survives the hop intact.
    assert response.json()["code"] == "validation_failed"
