"""A forged token must be refused — `01-…:687`, `ER-004.3`.

This is the negative control for the defect the shadow-smoke run exposed:
`get_actor` used to base64-decode a bearer token's payload and trust it, so a
self-made JWT naming any `sub` and any `tenant_id` was accepted. Reviewer
blinding survived that (RLS isolates by whatever subject is bound), but the
decision ledger did not: a decision could be recorded under a reviewer who never
made it.

The tests mint real keys and real tokens rather than mocking the verifier, so
what is proven is that a token signed by the WRONG key is rejected while one
signed by the RIGHT key is accepted — a mock of `verify_token` would prove only
that the mock was called.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from auth.verifier import TokenVerificationError, verify_token  # noqa: E402
from config import Settings  # noqa: E402
from tests.tenants import tenant  # noqa: E402

REALM_URL = "https://auth.test.invalid"
REALM = "portal"
ISSUER = f"{REALM_URL}/realms/{REALM}"


@pytest.fixture(scope="module")
def keys():  # noqa: ANN201
    """Two independent RSA keys: the realm's, and an attacker's."""
    return {
        "realm": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "attacker": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }


@pytest.fixture()
def settings():  # noqa: ANN201
    return Settings(keycloak_url=REALM_URL, keycloak_realm=REALM)


@pytest.fixture(autouse=True)
def realm_jwks(monkeypatch, keys):  # noqa: ANN201, ANN001
    """Serve the realm's public key as the JWKS the verifier resolves against.

    Only the key LOOKUP is stubbed. `jwt.decode` — signature, expiry and issuer
    — runs for real, which is the part under test.
    """
    from auth import verifier

    monkeypatch.setattr(verifier._CACHE, "signing_key", lambda _url, _token: keys["realm"].public_key())


def mint(key, *, sub: str = "reviewer-alice", tenant: str = "tenant-A", **overrides) -> str:  # noqa: ANN001
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "tenant_id": tenant,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "realm_access": {"roles": ["reviewer"]},
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def test_a_token_signed_by_the_realm_is_accepted(keys, settings):
    claims = verify_token(mint(keys["realm"]), settings)
    assert claims["sub"] == "reviewer-alice"
    assert claims["tenant_id"] == "tenant-A"


def test_a_token_signed_by_another_key_is_refused(keys, settings):
    """The core of it: a forged signature must not authenticate anyone."""
    with pytest.raises(TokenVerificationError):
        verify_token(mint(keys["attacker"], sub="reviewer-alice"), settings)


def test_an_unsigned_token_is_refused(settings):
    """`alg: none` — the form the old code accepted without noticing."""
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "reviewer-alice", "tenant_id": "tenant-A", "exp": int(time.time()) + 300},
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenVerificationError):
        verify_token(forged, settings)


def test_an_hmac_token_signed_with_the_public_key_is_refused(keys, settings):
    """Algorithm confusion: the RSA public key presented as an HMAC secret.

    `ALLOWED_ALGORITHMS` excludes the symmetric family precisely so this cannot
    work; without that exclusion the realm's own published key becomes a
    signing secret anybody can use.
    """
    import base64
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    public_pem = (
        keys["realm"]
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    # Assembled by hand, because `jwt.encode` REFUSES to mint this — PyJWT
    # rejects a PEM public key as an HMAC secret. That refusal is a good defence
    # in the signing path and useless here: an attacker does not use our library.
    # Building the token byte by byte is what makes this a test of the VERIFIER
    # rather than a test of PyJWT's encoder.
    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "iss": ISSUER,
                "sub": "reviewer-alice",
                "tenant_id": "tenant-A",
                "exp": int(time.time()) + 300,
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(TokenVerificationError):
        verify_token(forged, settings)


def test_an_expired_token_is_refused(keys, settings):
    with pytest.raises(TokenVerificationError):
        verify_token(mint(keys["realm"], exp=int(time.time()) - 3600), settings)


def test_a_token_from_another_realm_is_refused(keys, settings):
    """A valid token for a different issuer is not a credential here."""
    with pytest.raises(TokenVerificationError):
        verify_token(mint(keys["realm"], iss="https://auth.test.invalid/realms/other"), settings)


def test_a_token_with_no_subject_is_refused(keys, settings):
    """`sub` is required, because a decision must name a person.

    Without it the request would reach `require_human`, which would also refuse
    it — but refusing at the credential layer means an unattributable token
    never becomes an `Actor` at all.
    """
    claims = {
        "iss": ISSUER,
        "tenant_id": "tenant-A",
        "exp": int(time.time()) + 300,
    }
    with pytest.raises(TokenVerificationError):
        verify_token(jwt.encode(claims, keys["realm"], algorithm="RS256"), settings)


def test_the_api_refuses_a_forged_token_end_to_end(keys, monkeypatch):
    """The same forgery, through the actual dependency and a real route."""
    from fastapi.testclient import TestClient

    os.environ["ENABLE_AUTH"] = "true"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["KEYCLOAK_URL"] = REALM_URL
    os.environ["KEYCLOAK_REALM"] = REALM

    import config

    config.get_settings.cache_clear()
    from app import create_app

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get(
                "/api/reviews",
                headers={"Authorization": f"Bearer {mint(keys['attacker'])}"},
            )
        assert response.status_code == 401, response.text
        body = json.loads(response.text)
        assert body["code"] == "unauthenticated"
        # The reason is not disclosed — "expired", "wrong key" and "wrong realm"
        # are the same answer to a caller, because distinguishing them is a
        # probing oracle.
        assert "signature" not in body["detail"].lower()
    finally:
        os.environ["ENABLE_AUTH"] = "false"
        config.get_settings.cache_clear()


def test_header_identity_is_refused_in_a_production_environment(monkeypatch):
    """`ENABLE_AUTH=false` must be unreachable in production.

    The flag exists for the test suite. A misapplied ConfigMap that set it in
    production would otherwise let any caller name themselves as any reviewer —
    the same forgery, by a different route.
    """
    from fastapi.testclient import TestClient

    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "production"

    import config

    config.get_settings.cache_clear()
    from app import create_app

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/reviews", headers={"X-Tenant-Id": tenant("tenant-A"), "X-Subject-Id": "anyone"})
        assert response.status_code == 403, response.text
        assert "production" in response.text
    finally:
        os.environ["ENVIRONMENT"] = "test"
        config.get_settings.cache_clear()


# --------------------------------------------------------------------------
# A service account HAS a subject. That is the whole problem.
# --------------------------------------------------------------------------

#: The claim shape Keycloak actually emits for a client-credentials grant
#: against the `portal` realm. `sub` is present and well-formed — it is the
#: service account user's UUID — which is why "has a subject" was never a test
#: for humanity.
KEYCLOAK_SERVICE_ACCOUNT_CLAIMS = {
    "iss": "https://auth.nonagentic.ai/realms/portal",
    "sub": "11111111-2222-3333-4444-555555555555",
    "typ": "Bearer",
    "azp": "fr-backend",
    "clientId": "fr-backend",
    "clientHost": "10.42.0.1",
    "clientAddress": "10.42.0.1",
    "preferred_username": "service-account-fr-backend",
    "tenant_id": "0195f48b-d000-7000-8000-000000000001",
    "realm_access": {"roles": ["default-roles-portal", "uma_authorization"]},
}

#: A human reviewer's token from the same realm, for contrast. No `clientId`,
#: and a username that is a person.
KEYCLOAK_HUMAN_CLAIMS = {
    "iss": "https://auth.nonagentic.ai/realms/portal",
    "sub": "99999999-8888-7777-6666-555555555555",
    "typ": "Bearer",
    "azp": "portal",
    "sid": "e3a1b2c3-0000-1111-2222-333344445555",
    "preferred_username": "reviewer.b",
    "email": "reviewer.b@example.org",
    "tenant_id": "0195f48b-d000-7000-8000-000000000001",
    "realm_access": {"roles": ["default-roles-portal"]},
}


def test_a_keycloak_service_account_is_not_a_human() -> None:
    """`ER-004`'s guard, against the principal it actually has to stop.

    Every prior test modelled a service principal as "a tenant with no
    subject", which is the shape the local `AUTH_BYPASS` header path produces.
    Under real auth that shape cannot occur: a token that verifies always has
    a `sub`. So the guard was green in CI and absent in production.
    """
    from actor import NotAHumanPrincipalError, require_human
    from api.dependencies import _actor_from_claims

    actor = _actor_from_claims(KEYCLOAK_SERVICE_ACCOUNT_CLAIMS)

    assert actor.subject_id, "the service account does have a subject; that is the point"
    assert actor.is_service_principal
    assert not actor.is_human
    with pytest.raises(NotAHumanPrincipalError):
        require_human(actor)


def test_a_human_reviewer_from_the_same_realm_is_still_accepted() -> None:
    """The falsifiable control.

    Without this, the fix above could be "refuse everyone", which passes the
    negative test and makes the product unusable.
    """
    from actor import require_human
    from api.dependencies import _actor_from_claims

    actor = _actor_from_claims(KEYCLOAK_HUMAN_CLAIMS)

    assert not actor.is_service_principal
    assert actor.is_human
    assert require_human(actor) == KEYCLOAK_HUMAN_CLAIMS["sub"]


# --------------------------------------------------------------------------
# What to CALL a principal
#
# A lead's ruling is permanent and attributable, and it was attributed to a
# 36-character UUID. `display_name` is a label placed on top of `subject_id`,
# never a replacement for it: the id stays the evidentiary attribution and is
# what every fallback lands on.
# --------------------------------------------------------------------------


def test_a_full_name_is_preferred_over_the_username() -> None:
    """`name` is the Keycloak `profile` full-name mapper.

    A username is an identifier a person recognises; it is not the name they go
    by, so it is the fallback rather than the first choice.
    """
    from api.dependencies import _actor_from_claims

    claims = {**KEYCLOAK_HUMAN_CLAIMS, "name": "Reviewer B"}
    assert _actor_from_claims(claims).display_name == "Reviewer B"


def test_a_token_without_a_full_name_falls_back_to_the_username() -> None:
    """The shape of a realm user whose profile carries no first/last name."""
    from api.dependencies import _actor_from_claims

    assert "name" not in KEYCLOAK_HUMAN_CLAIMS
    assert _actor_from_claims(KEYCLOAK_HUMAN_CLAIMS).display_name == "reviewer.b"


def test_a_token_carrying_neither_claim_yields_no_name_rather_than_a_wrong_one() -> None:
    """The case that decides whether this feature does anything at all.

    Both claims come from the `profile` client scope. If the client does not
    carry it, NEITHER is present — and the honest answer is `None`, which sends
    every reader back to the subject id, exactly what was rendered before this
    column existed. Inventing a name from `sub` or `email` here would attribute
    a permanent ruling to something the credential never asserted.
    """
    from api.dependencies import _actor_from_claims

    claims = {k: v for k, v in KEYCLOAK_HUMAN_CLAIMS.items() if k != "preferred_username"}
    actor = _actor_from_claims(claims)

    assert actor.display_name is None
    assert actor.subject_id == KEYCLOAK_HUMAN_CLAIMS["sub"], "the attribution itself survives"


def test_a_service_principal_is_never_given_a_display_name() -> None:
    """`service-account-fr-backend` is not a person's name.

    The service-account username IS present on these tokens, so a naive
    `preferred_username` read would write a machine's account name into a
    column that a studio renders as the human who ruled.
    """
    from api.dependencies import _actor_from_claims

    actor = _actor_from_claims(KEYCLOAK_SERVICE_ACCOUNT_CLAIMS)

    assert actor.is_service_principal
    assert actor.display_name is None


@pytest.mark.parametrize(
    "marker",
    [
        {"preferred_username": "service-account-n8n"},
        {"preferred_username": "SERVICE-ACCOUNT-KAG"},  # case is not a bypass
        {"clientId": "kag"},
        {"clientHost": "10.42.0.9"},
        {"clientAddress": "10.42.0.9"},
    ],
)
def test_each_service_account_marker_is_load_bearing(marker: dict[str, str]) -> None:
    """Any one marker alone is enough; none of them is decoration."""
    from api.dependencies import _actor_from_claims

    claims = {**KEYCLOAK_HUMAN_CLAIMS, **marker}
    assert _actor_from_claims(claims).is_service_principal


def test_the_service_account_refusal_reaches_the_caller_as_a_typed_problem() -> None:
    """A 403 `human_principal_required` on the wire — not a 500, not a silent write.

    Asserted against the serialized body rather than an attribute name, because
    what a caller can act on is the `code` slug in the response.
    """
    import json as _json

    from api.problem_details import HumanPrincipalRequiredError

    response = HumanPrincipalRequiredError("the credential is a service account").to_response(
        instance="/api/reviews/r1/decisions"
    )
    body = _json.loads(bytes(response.body))

    assert response.status_code == 403
    assert body["code"] == "human_principal_required"
    assert body["instance"] == "/api/reviews/r1/decisions"


# --------------------------------------------------------------------------
# A tenant that is not a uuid is a malformed credential, refused as one.
# --------------------------------------------------------------------------

#: What a caller sends when it has not read `0014` — a readable tenant slug, the
#: spelling every test in this repo used before the columns became `uuid`.
#: ⛔ NOT a uuid, and it must stay that way. The mechanical conversion that
#: turned every readable tenant label in this suite into a derived uuid matched
#: this name too — and a "non-uuid tenant" that is a valid uuid means the
#: boundary accepts it, so the refusal test below passed nothing and CI caught
#: it as a 200 where a 401 was asserted. A fixture that is deliberately invalid
#: has to be exempt from the sweep that makes everything else valid.
NON_UUID_TENANT = "tenant-A"


def test_a_tenant_header_that_is_not_a_uuid_is_refused_at_the_boundary():
    """`401 unauthenticated`, in problem+json, and NOT a 500 naming the database.

    `0014` made all 25 `tenant_id` columns `uuid`. Before
    `api/dependencies._tenant_uuid`, an unparseable tenant was carried inward and
    died at the first `set_config('app.current_tenant', …)` inside the request's
    transaction, as a psycopg `InvalidTextRepresentation` this service has no
    handler for — measured on the migration fixture as
    `invalid input syntax for type uuid: "tenant-downgrade-guard"`. On the wire
    that is a `500` blaming the server for the caller's malformed credential, and
    a `500` is not something a client can branch on.

    All three facts are asserted, for the reason `test_request_body_bounds.py`
    spells out: the `code` alone does not pin the status, and the status alone
    does not pin the media type. A `401 text/plain` would satisfy a
    status-only assertion and would still fail the gateway contract.

    The junk value must not come back. It arrives on an unauthenticated path and
    lands in a problem-details body that is logged downstream; naming the HEADER
    is the whole of what the caller needs in order to fix it.

    `_actor_from_claims` is the control, and it is the second call site of the
    same helper rather than decoration: a bearer token whose `tenant_id` claim is
    a slug is refused identically, while the well-formed one is accepted and
    arrives as a `uuid.UUID` — which is the type `Actor.tenant_id` now declares
    and which every `row.tenant_id == actor.tenant_id` in the service depends on.
    Without that half, "refuse everything" would pass.
    """
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from api.dependencies import UnauthenticatedError, _actor_from_claims

    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"

    import config

    config.get_settings.cache_clear()
    from app import create_app

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get(
                "/api/reviews",
                headers={"X-Tenant-Id": NON_UUID_TENANT, "X-Subject-Id": "anyone"},
            )
    finally:
        config.get_settings.cache_clear()

    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), (
        f"the refusal is not problem+json: {response.headers.get('content-type')!r} / {response.text[:200]!r}"
    )
    body = json.loads(response.text)
    assert body["code"] == "unauthenticated", response.text
    assert "X-Tenant-Id" in body["detail"], (
        f"the refusal does not name the field the caller has to fix: {body['detail']!r}"
    )
    assert NON_UUID_TENANT not in response.text, f"the rejected credential was echoed back: {response.text}"

    # The claim path, same helper, and the accepting control beside it.
    with pytest.raises(UnauthenticatedError):
        _actor_from_claims({**KEYCLOAK_HUMAN_CLAIMS, "tenant_id": NON_UUID_TENANT})
    accepted = _actor_from_claims(KEYCLOAK_HUMAN_CLAIMS)
    assert accepted.tenant_id == _uuid.UUID(KEYCLOAK_HUMAN_CLAIMS["tenant_id"])
    assert isinstance(accepted.tenant_id, _uuid.UUID), (
        "a str that merely looks like a uuid compares False against the UUID psycopg returns "
        "from a uuid column, silently, on every tenant check in the service"
    )


# --------------------------------------------------------------------------
# Which realm role actually grants LEAD
# --------------------------------------------------------------------------


def test_the_realm_role_names_that_grant_lead_are_pinned() -> None:
    """`LEAD` is reachable only if the realm mints one of these exact names.

    This is pinned because the role is invisible in CI. Every adjudication and
    unblinding test obtains `lead` from the `X-Actor-Role` **header**, which
    only exists on the `AUTH_BYPASS` path. Under real auth the role comes from
    `realm_access.roles`, and if the realm defines none of these strings then
    no caller can ever hold `LEAD` in production — however green the suite is.

    Consequence, if that happens: only a review's creator governs it. A
    conflict whose reviewers include the creator then has **no eligible
    adjudicator** — the database's separation-of-duty trigger refuses a
    reviewer as adjudicator of their own conflict — so the conflict cannot be
    resolved and the export, which requires no open conflicts, is refused
    permanently.

    Whoever creates the realm role needs the exact string; that is what this
    test is for. It is `review-lead`, and it now EXISTS in the `portal` realm.

    ## `"lead"` was removed from the accepted set, deliberately

    `_role_from` also scans `resource_access`, i.e. CLIENT roles, and it scans
    every client's grant rather than one named client. Measured live: the
    `portal` realm has 35 clients and 30 are `fullScopeAllowed`, so a token
    minted for one carries the user's client roles for the others. A role called
    `lead` on a kanban board or a CRM client is an ordinary name for somebody to
    add, and it would have silently conferred evidence-review LEAD: adjudicate,
    unblind, seal a corpus, export an inclusion set.

    No such client role exists in either realm today — every client's roles were
    enumerated — so this was latent rather than live. The two surviving spellings
    are specific enough that nobody adds one by accident.
    """
    from actor import ActorRole
    from api.dependencies import _role_from

    for name in ("review-lead", "evidence-review-lead"):
        claims = {"realm_access": {"roles": ["default-roles-portal", name]}}
        assert _role_from(claims) is ActorRole.LEAD, name

    # And the removed spelling stays removed, in BOTH claim shapes — the client
    # one is where the cross-client exposure lives.
    assert _role_from({"realm_access": {"roles": ["lead"]}}) is ActorRole.REVIEWER
    assert _role_from({"resource_access": {"kanban": {"roles": ["lead"]}}}) is ActorRole.REVIEWER


def test_a_portal_token_carrying_only_default_roles_is_a_reviewer() -> None:
    """The default, and the safe direction.

    An unrecognised or absent role must never widen visibility — a typo in a
    realm role mapping would otherwise become a blinding bypass.
    """
    from actor import ActorRole
    from api.dependencies import _role_from

    assert _role_from(KEYCLOAK_HUMAN_CLAIMS) is ActorRole.REVIEWER
    assert _role_from({"realm_access": {"roles": ["review-leed"]}}) is ActorRole.REVIEWER
    assert _role_from({}) is ActorRole.REVIEWER
