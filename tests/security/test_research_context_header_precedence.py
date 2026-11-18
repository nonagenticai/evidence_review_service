"""`X-Research-Context` outranks `Authorization`, and nothing said so.

Measured against the LIVE in-cluster service on 2026-08-11, with a real portal
token minted by a `kag-frontend` password grant:

    Authorization: Bearer <valid>                             -> 200
    Authorization: Bearer <valid> + X-Research-Context: junk   -> 200
    Authorization: Bearer <valid> + X-Research-Context: Bearer junk
                                                              -> 401

The third line is the finding. `get_actor` (`api/dependencies.py`) ends with:

    for header in (x_research_context, authorization):
        if header and header.lower().startswith("bearer "):
            ...authenticate this one and return...

so `X-Research-Context` is tried FIRST and, crucially, only when it looks like a
bearer. (What "authenticate" means inside that loop has since grown a second
rung — a JWT is verified, anything that is not a JWT is introspected as an opaque
licence token — but the loop, and therefore the precedence this file is about, is
untouched by that.) Two consequences that pull in opposite directions and were previously
conflated into one wrong belief ("any hop that sets this header breaks auth"):

* A value that is NOT bearer-shaped falls through harmlessly. That half is fine,
  and it is why the naive probe returned 200 and read as "no hazard here".
* A value that IS bearer-shaped WINS over the caller's own `Authorization`. If it
  fails verification the caller gets a flat 401 while holding a perfectly good
  credential. If it VERIFIES, the service acts as that identity instead — with no
  warning, no log line distinguishing it, and the caller's real bearer silently
  unread.

The second case is the one this file is really about, and it is not theoretical
in shape: both hops in front of evidence-review forward this header verbatim
(`kag`'s `FORWARDED_REQUEST_HEADERS` includes `x-research-context`), so any
future component that populates it — a gateway attaching a service credential, a
licence-token shim — substitutes identity on an append-only decision ledger where
`reviewer_subject_id` is the evidentiary record of who screened what.

This file does not change the precedence. Whether research-context-first is right
is a design question with callers on the other side of it. What it does is make
the precedence a DECLARED property with a behavioural test, so that changing it is
a decision rather than an accident, and so the substitution is visible to whoever
reads this next.

Both tokens are signed by the same realm key and both VERIFY. The test is
therefore about precedence, not about forgery — `test_token_verification.py`
covers forgery, and a test where one token is invalid could not tell "the header
won" from "the header was rejected".
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.tenants import tenant

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

REALM_URL = "https://auth.test.invalid"
REALM = "portal"
ISSUER = f"{REALM_URL}/realms/{REALM}"

#: Two tenants, because the proof is which one's rows come back. Same-tenant
#: subjects would return identical lists and the test would pass either way.
TENANT_CALLER = tenant("tenant-research-context-caller")
TENANT_OTHER = tenant("tenant-research-context-other")


@pytest.fixture(scope="module")
def realm_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(key, *, sub: str, tenant: str) -> str:  # noqa: ANN001
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": sub,
            "tenant_id": tenant,
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "realm_access": {"roles": ["review-lead"]},
        },
        key,
        algorithm="RS256",
    )


@pytest.fixture()
def client(monkeypatch, realm_key):  # noqa: ANN201, ANN001
    """The app with auth ON, resolving signatures against `realm_key`.

    Only the key LOOKUP is stubbed; `jwt.decode` runs for real, so both tokens
    below genuinely verify and the assertions are about which one was USED.
    """
    from auth import verifier

    monkeypatch.setattr(verifier._CACHE, "signing_key", lambda _url, _token: realm_key.public_key())

    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("KEYCLOAK_URL", REALM_URL)
    monkeypatch.setenv("KEYCLOAK_REALM", REALM)

    import config

    config.get_settings.cache_clear()
    from app import create_app

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as c:
            yield c
    finally:
        config.get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_non_bearer_research_context_falls_through_to_the_authorization_header(client, realm_key):
    """The harmless half, pinned so the dangerous half below is not over-read.

    A junk `X-Research-Context` does NOT break a good `Authorization`. This is
    the measurement that made the hazard look absent, and it is worth keeping
    exactly so the next reader does not re-derive "no hazard" from it.
    """
    caller = mint(realm_key, sub="caller-alice", tenant=TENANT_CALLER)
    response = client.get(
        "/api/reviews",
        headers={**_auth(caller), "X-Research-Context": "some-correlation-id-not-a-token"},
    )
    assert response.status_code == 200, response.text


def test_a_bearer_shaped_research_context_outranks_the_callers_own_authorization(client, realm_key):
    """The finding, asserted as WHICH IDENTITY ACTED — not as a status code.

    Alice authenticates with her own `Authorization` and creates a review, so her
    tenant has exactly one. She then lists reviews presenting BOTH her valid
    `Authorization` AND a valid `X-Research-Context` belonging to another tenant.

    If `Authorization` were authoritative she would see her review. She does not:
    the list comes back empty, because the service bound Mallory's tenant and
    Alice's credential was never read. That is identity substitution, and a status
    code alone could never have shown it — both requests are 200.
    """
    alice = mint(realm_key, sub="caller-alice", tenant=TENANT_CALLER)
    mallory = mint(realm_key, sub="other-mallory", tenant=TENANT_OTHER)

    created = client.post(
        "/api/reviews",
        json={
            "project_id": "research-context",
            "external_key": f"rc-{uuid.uuid4()}",
            "title": "Whose review is this",
        },
        headers=_auth(alice),
    )
    assert created.status_code == 201, created.text

    mine = client.get("/api/reviews", headers=_auth(alice))
    assert mine.status_code == 200, mine.text
    assert mine.json()["count"] >= 1, (
        "control failed: Alice cannot see her own review, so the test below proves nothing"
    )

    substituted = client.get(
        "/api/reviews",
        headers={**_auth(alice), "X-Research-Context": f"Bearer {mallory}"},
    )
    assert substituted.status_code == 200, substituted.text
    assert substituted.json()["count"] == 0, (
        "X-Research-Context no longer outranks Authorization. That may well be the "
        "right change — but it is a change to which identity a decision is recorded "
        "under, so update this test deliberately and say so in the commit, rather "
        "than deleting it because it went red."
    )


def test_an_unverifiable_research_context_401s_a_caller_holding_a_valid_credential(client, realm_key):
    """The cheaper symptom, and the one an operator will actually see first.

    A bearer-shaped `X-Research-Context` that does not verify does NOT fall
    through to the good `Authorization` beside it. The caller gets a flat 401 with
    a message about "the presented credential", which points the investigation at
    the credential they can see rather than the header they cannot.

    ⚠️ `not-a-real-token` has no `.` in it, so since the licence-token rung was
    added it is no longer refused as a bad JWT — it is treated as an opaque
    licence token, sent to the introspect rung, and refused there because this
    test's environment configures no `LICENSE_INTROSPECT_URL`. Same 401, same
    code, different reason, and the property under test (a bearer-shaped
    research context is not retried against `Authorization`) is unchanged. The
    fail-closed direction of the unset-URL case is what keeps this test honest;
    if that ever became "allow", this would go green for the wrong reason —
    which is why `test_an_opaque_licence_token_is_not_a_broken_jwt` asserts it
    directly.
    """
    alice = mint(realm_key, sub="caller-alice", tenant=TENANT_CALLER)
    response = client.get(
        "/api/reviews",
        headers={**_auth(alice), "X-Research-Context": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "unauthenticated"
