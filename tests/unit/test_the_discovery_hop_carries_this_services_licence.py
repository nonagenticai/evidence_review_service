"""The discovery-engine hop carries THIS service's licence, and never loses the tenant.

discovery-engine (`auth/middleware.py`, `AUTH_TOKENLESS_IDENTITY_MODE=enforce`)
will refuse a bare `X-Tenant-Id` with no licence. This service acts for many
customer tenants, so its credential is a service licence presented on-behalf:
`Authorization: Bearer <licence>` PLUS the unchanged plain `X-Tenant-Id: <tenant>`.

Asserted over a REAL socket, not a patched client: what matters is the headers
that actually leave the process.

* unset  -> byte-identical to the request this hop has always sent;
* set    -> the same request plus `Authorization`, the tenant untouched;
* the licence must never ride in `X-Tenant-Id: Bearer` (that carrier binds the
  request to the licence's OWN tenant, i.e. it would silently re-tenant every
  import to `svc-evidence-review`'s tenant).

No token literal appears here: each test mints a random one, so the
detect-secrets baseline has nothing to learn.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from infrastructure.clients import discovery_engine as de

TENANT = "8a6f1c2e-0d4b-4f7a-9c3e-2b1d5e6f7a80"
OTHER_TENANT = "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"
BODY = json.dumps({"records": []}).encode()


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[list[tuple[str, str]]] = []


@pytest.fixture()
def upstream() -> Iterator[tuple[str, _Recorder]]:
    state = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            # The RAW header list, in order and with duplicates, so a second
            # `X-Tenant-Id` or a reordered request is visible.
            state.requests.append(list(self.headers.items()))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


async def _fetch(base_url: str, **kwargs: object) -> None:
    await de.fetch_review_export(
        base_url=base_url,
        tenant_id=TENANT,
        session_id="s-1",
        batch_id="b-1",
        timeout_seconds=5,
        **kwargs,  # type: ignore[arg-type]
    )


def _named(raw: list[tuple[str, str]], name: str) -> list[str]:
    return [v for k, v in raw if k.lower() == name.lower()]


async def test_unset_sends_exactly_todays_request(upstream: tuple[str, _Recorder]) -> None:
    base_url, state = upstream
    await _fetch(base_url)  # no licence argument at all: every pre-existing caller
    await _fetch(base_url, license_token=None)
    await _fetch(base_url, license_token="")
    await _fetch(base_url, license_token="   ")

    assert len(state.requests) == 4
    baseline = state.requests[0]
    # Today's request, pinned in full: the client's own headers in order.
    assert [k.lower() for k, _ in baseline] == [
        "host",
        "accept-encoding",
        "connection",
        "user-agent",
        "x-tenant-id",
        "accept",
    ]
    assert _named(baseline, "X-Tenant-Id") == [TENANT]
    assert _named(baseline, "Authorization") == []
    # None / "" / whitespace are all "unset" and identical to omitting the arg.
    for other in state.requests[1:]:
        assert other == baseline


async def test_set_adds_only_the_bearer_and_keeps_the_tenant(upstream: tuple[str, _Recorder]) -> None:
    base_url, state = upstream
    token = secrets.token_urlsafe(32)
    await _fetch(base_url)
    await _fetch(base_url, license_token=f"  {token}\n")

    bare, licensed = state.requests
    assert _named(licensed, "Authorization") == [f"Bearer {token}"]
    # The tenant is the customer's, plain, exactly once — not the licence, not
    # `Bearer`-wrapped (that carrier would re-tenant the request to the licence).
    assert _named(licensed, "X-Tenant-Id") == [TENANT]
    assert not any(v.lower().startswith("bearer") for v in _named(licensed, "X-Tenant-Id"))
    # Everything else is the bare request unchanged.
    assert [h for h in licensed if h[0].lower() != "authorization"] == bare


def test_request_headers_precedence_and_tenant_preserved() -> None:
    token = secrets.token_urlsafe(24)
    for tenant in (TENANT, OTHER_TENANT):
        assert de.request_headers(tenant_id=tenant, license_token=None) == {
            "X-Tenant-Id": tenant,
            "Accept": "application/json",
        }
        assert de.request_headers(tenant_id=tenant, license_token=token) == {
            "X-Tenant-Id": tenant,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }


def test_setting_reads_the_env_strips_and_never_reprs(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import Settings

    monkeypatch.delenv("DISCOVERY_ENGINE_LICENSE_TOKEN", raising=False)
    assert Settings().discovery_engine_license_token == ""

    monkeypatch.setenv("DISCOVERY_ENGINE_LICENSE_TOKEN", "   ")
    assert Settings().discovery_engine_license_token == ""

    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("DISCOVERY_ENGINE_LICENSE_TOKEN", f" {token} ")
    settings = Settings()
    assert settings.discovery_engine_license_token == token
    # A logged Settings must never carry the credential.
    assert token not in repr(settings)
