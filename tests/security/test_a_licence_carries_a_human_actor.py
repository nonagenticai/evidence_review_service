"""A licence names the tenant; `X-Actor-Token` names the human who decided.

kag-frontend owns tenants that are not in Keycloak's `tenant_id` claim. It calls
this service with that tenant's own `evidence-review` licence as the bearer and
its user's Keycloak JWT (azp `kag-frontend`) as `X-Actor-Token`, because a
screening decision needs a person (`require_human`).

What this file pins:

* licence + valid actor token -> a HUMAN actor whose tenant is the LICENCE's,
  whatever tenant the JWT claims;
* a present-but-bad actor token (forged, expired, wrong azp, service account) is
  a 401, never a silent fall back to the licence's service principal;
* on the JWT rung the header is ignored, so a JWT caller cannot impersonate;
* `ER_ACCEPTED_LICENCE_APPS` refuses a licence for another app when armed, and
  is a no-op when empty (the measured default);
* a licence with no actor token stays a service principal.

No database: `get_actor` is driven as a function, like its sibling file.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from actor import ActorRole, require_human
from api.dependencies import UnauthenticatedError, get_actor

REALM_URL = "https://auth.test.invalid"
REALM = "portal"
ISSUER = f"{REALM_URL}/realms/{REALM}"
INTROSPECT_URL = "http://licenses.test.invalid/license/introspect"

OPAQUE_TOKEN = "opaque-licence-token-shape-not-a-credential"  # nosec B105

#: The kag tenant the licence was minted for. Not in any Keycloak claim.
LICENCE_TENANT = str(uuid.uuid4())
#: A DIFFERENT tenant the user's JWT claims (their portal tenant).
JWT_TENANT = str(uuid.uuid4())
LICENCE_SUBJECT = "svc-licence-subject"
HUMAN_SUBJECT = "0b8f1d2e-1111-4222-8333-444455556666"


def _settings(**overrides: Any):  # noqa: ANN201 - Settings is a plain dataclass
    from config import Settings

    fields: dict[str, Any] = {
        "enable_auth": True,
        "environment": "test",
        "keycloak_url": REALM_URL,
        "keycloak_realm": REALM,
        "license_introspect_url": INTROSPECT_URL,
        "er_actor_token_azp": ("kag-frontend",),
        "er_accepted_licence_apps": (),
    }
    fields.update(overrides)
    return dataclasses.replace(Settings(), **fields)


def _licence(app_key: str | None = "evidence-review") -> dict[str, Any]:
    return {
        "active": True,
        "tenant_id": LICENCE_TENANT,
        "subject_id": LICENCE_SUBJECT,
        "license_id": str(uuid.uuid4()),
        "app": {"id": str(uuid.uuid4()), "key": app_key} if app_key else None,
    }


class IntrospectSpy:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.tokens: list[str] = []

    async def __call__(self, token: str, *, url: str | None = None) -> dict[str, Any] | None:
        self.tokens.append(token)
        return self.answer


@pytest.fixture()
def spy(monkeypatch):  # noqa: ANN201, ANN001
    recorder = IntrospectSpy(_licence())
    monkeypatch.setattr("api.dependencies.introspect_license", recorder)
    return recorder


@pytest.fixture(scope="module")
def realm_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def attacker_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def jwks_returns_the_realm_key(monkeypatch, realm_key):  # noqa: ANN201, ANN001
    """Stub the key LOOKUP only; `jwt.decode` really runs, so forgeries really fail."""
    from auth import verifier

    monkeypatch.setattr(verifier._CACHE, "signing_key", lambda _url, _token: realm_key.public_key())


def mint(key, **claims: Any) -> str:  # noqa: ANN001
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": HUMAN_SUBJECT,
        # Keycloak's access-token `typ`, as in the realm fixture
        # `KEYCLOAK_HUMAN_CLAIMS` (tests/security/test_token_verification.py).
        "typ": "Bearer",
        "azp": "kag-frontend",
        "tenant_id": JWT_TENANT,
        "tenant": JWT_TENANT,
        "name": "Ada Reviewer",
        "realm_access": {"roles": ["review-lead"]},
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256")


async def _licence_actor(actor_token: str | None, settings=None):  # noqa: ANN001, ANN202
    return await get_actor(
        authorization=f"Bearer {OPAQUE_TOKEN}",
        x_actor_token=actor_token,
        settings=settings or _settings(),
    )


# --------------------------------------------------------------------------- #
# Happy path.
# --------------------------------------------------------------------------- #


async def test_a_licence_with_a_valid_actor_token_is_a_human_in_the_licence_tenant(spy, realm_key) -> None:  # noqa: ANN001
    actor = await _licence_actor(mint(realm_key))

    assert spy.tokens == [OPAQUE_TOKEN], "the licence must still be introspected"
    assert str(actor.tenant_id) == LICENCE_TENANT, "tenant must come from the LICENCE"
    assert str(actor.tenant_id) != JWT_TENANT
    assert actor.subject_id == HUMAN_SUBJECT, "subject must come from the actor JWT's sub"
    assert actor.is_service_principal is False
    assert actor.is_human is True
    assert actor.display_name == "Ada Reviewer"
    assert require_human(actor) == HUMAN_SUBJECT


async def test_the_actor_tokens_roles_do_not_confer_lead(spy, realm_key) -> None:  # noqa: ANN001
    """A portal `review-lead` is a lead in the portal tenant, not in a kag tenant."""
    actor = await _licence_actor(mint(realm_key))
    assert actor.role is ActorRole.REVIEWER


async def test_every_jwt_tenant_claim_is_ignored(spy, realm_key) -> None:  # noqa: ANN001
    """Even a JWT that names no tenant at all works: the tenant is not read from it."""
    token = mint(realm_key, tenant_id=None, tenant=None)
    actor = await _licence_actor(token)
    assert str(actor.tenant_id) == LICENCE_TENANT


# --------------------------------------------------------------------------- #
# A present header never degrades to the service principal.
# --------------------------------------------------------------------------- #


async def test_a_forged_actor_token_is_refused(spy, attacker_key) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(attacker_key))


async def test_an_expired_actor_token_is_refused(spy, realm_key) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key, exp=int(time.time()) - 3600))


@pytest.mark.parametrize("value", ["", "   ", "not-a-jwt", "two.segments"])
async def test_a_malformed_actor_token_is_refused(spy, value: str) -> None:
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(value)


@pytest.mark.parametrize("azp", ["portal-bff", "fr", None])
async def test_a_wrong_azp_is_refused(spy, realm_key, azp: str | None) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key, azp=azp))


async def test_an_empty_azp_allowlist_refuses_every_actor_token(spy, realm_key) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key), settings=_settings(er_actor_token_azp=()))


async def test_a_service_account_token_from_an_accepted_client_is_refused(spy, realm_key) -> None:  # noqa: ANN001
    """A client-credentials token minted for kag-frontend has azp kag-frontend too."""
    token = mint(realm_key, preferred_username="service-account-kag-frontend", clientId="kag-frontend")
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(token)


async def test_an_actor_token_with_no_subject_is_refused(spy, realm_key) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key, sub=None))


async def test_a_snake_case_client_id_token_is_refused(spy, realm_key) -> None:  # noqa: ANN001
    """Newer Keycloak emits `client_id`, not `clientId`, on a client-credentials token."""
    token = mint(realm_key, client_id="kag-frontend")
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(token)


@pytest.mark.parametrize("typ", ["ID", "Refresh", "Offline", None, ""])
async def test_a_non_access_token_is_refused(spy, realm_key, typ: str | None) -> None:  # noqa: ANN001
    """An ID token for kag-frontend is signed by the same realm key; it is not an access token."""
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key, typ=typ))


@pytest.mark.parametrize("typ", ["Bearer", "bearer", "BEARER"])
async def test_the_access_token_typ_is_case_insensitive(spy, realm_key, typ: str) -> None:  # noqa: ANN001
    actor = await _licence_actor(mint(realm_key, typ=typ))
    assert actor.subject_id == HUMAN_SUBJECT


# --------------------------------------------------------------------------- #
# X-Research-Context carries a licence for a SERVICE hop: the header is ignored.
# --------------------------------------------------------------------------- #


async def test_a_licence_on_research_context_ignores_a_valid_actor_token(spy, realm_key) -> None:  # noqa: ANN001
    actor = await get_actor(
        x_research_context=f"Bearer {OPAQUE_TOKEN}",
        x_actor_token=mint(realm_key),
        settings=_settings(),
    )

    assert spy.tokens == [OPAQUE_TOKEN]
    assert str(actor.tenant_id) == LICENCE_TENANT
    assert actor.subject_id == LICENCE_SUBJECT
    assert actor.is_service_principal is True


async def test_a_licence_on_research_context_ignores_a_garbage_actor_token(spy) -> None:  # noqa: ANN001
    actor = await get_actor(
        x_research_context=f"Bearer {OPAQUE_TOKEN}",
        x_actor_token="garbage",
        settings=_settings(),
    )
    assert actor.is_service_principal is True


async def test_research_context_wins_over_an_authorization_licence_and_drops_the_actor(spy, realm_key) -> None:  # noqa: ANN001
    """Precedence is unchanged (X-Research-Context first), and that rung never honours the actor."""
    actor = await get_actor(
        x_research_context=f"Bearer {OPAQUE_TOKEN}",
        authorization="Bearer another-opaque-licence-token",
        x_actor_token=mint(realm_key),
        settings=_settings(),
    )
    assert spy.tokens == [OPAQUE_TOKEN]
    assert actor.is_service_principal is True


# --------------------------------------------------------------------------- #
# The JWT rung ignores the header.
# --------------------------------------------------------------------------- #


async def test_a_jwt_caller_cannot_impersonate_with_an_actor_token(spy, realm_key) -> None:  # noqa: ANN001
    caller_subject = "jwt-caller-subject"
    caller = mint(realm_key, sub=caller_subject, azp="portal-bff", name="Caller")
    victim = mint(realm_key, sub="victim-subject", name="Victim")

    actor = await get_actor(authorization=f"Bearer {caller}", x_actor_token=victim, settings=_settings())

    assert actor.subject_id == caller_subject
    assert actor.display_name == "Caller"
    assert str(actor.tenant_id) == JWT_TENANT
    assert spy.tokens == [], "a JWT caller must not reach the licence rung"


async def test_a_jwt_caller_with_a_garbage_actor_token_is_unaffected(spy, realm_key) -> None:  # noqa: ANN001
    """Ignored means ignored: not validated, so it cannot fail the request either."""
    actor = await get_actor(
        authorization=f"Bearer {mint(realm_key)}",
        x_actor_token="garbage",
        settings=_settings(),
    )
    assert actor.subject_id == HUMAN_SUBJECT


async def test_the_header_mode_ignores_an_actor_token(realm_key) -> None:  # noqa: ANN001
    tenant = str(uuid.uuid4())
    actor = await get_actor(
        x_tenant_id=tenant,
        x_subject_id="header-subject",
        x_actor_token=mint(realm_key),
        settings=_settings(enable_auth=False),
    )
    assert str(actor.tenant_id) == tenant
    assert actor.subject_id == "header-subject"


# --------------------------------------------------------------------------- #
# The licence-app allowlist.
# --------------------------------------------------------------------------- #


async def test_an_armed_allowlist_refuses_another_apps_licence(spy, realm_key) -> None:  # noqa: ANN001
    spy.answer = _licence("kag")
    settings = _settings(er_accepted_licence_apps=("evidence-review",))
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(None, settings=settings)
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(mint(realm_key), settings=settings)


async def test_an_armed_allowlist_refuses_a_licence_with_no_app(spy) -> None:  # noqa: ANN001
    spy.answer = _licence(None)
    with pytest.raises(UnauthenticatedError):
        await _licence_actor(None, settings=_settings(er_accepted_licence_apps=("evidence-review",)))


async def test_an_armed_allowlist_admits_a_listed_app(spy) -> None:  # noqa: ANN001
    spy.answer = _licence("evidence-review")
    actor = await _licence_actor(None, settings=_settings(er_accepted_licence_apps=("evidence-review", "kag")))
    assert str(actor.tenant_id) == LICENCE_TENANT


async def test_an_empty_allowlist_checks_no_app(spy) -> None:  # noqa: ANN001
    """The measured default: no inbound caller's app could be determined."""
    spy.answer = _licence("kag")
    actor = await _licence_actor(None, settings=_settings(er_accepted_licence_apps=()))
    assert str(actor.tenant_id) == LICENCE_TENANT


def test_the_shipped_defaults(monkeypatch) -> None:  # noqa: ANN001
    from config import Settings

    monkeypatch.delenv("ER_ACCEPTED_LICENCE_APPS", raising=False)
    monkeypatch.delenv("ER_ACTOR_TOKEN_AZP", raising=False)
    defaults = Settings()
    assert defaults.er_accepted_licence_apps == ()
    assert defaults.er_actor_token_azp == ("kag-frontend",)

    monkeypatch.setenv("ER_ACCEPTED_LICENCE_APPS", " evidence-review , kag ,")
    monkeypatch.setenv("ER_ACTOR_TOKEN_AZP", "")
    parsed = Settings()
    assert parsed.er_accepted_licence_apps == ("evidence-review", "kag")
    assert parsed.er_actor_token_azp == ()


# --------------------------------------------------------------------------- #
# Existing behaviour, kept.
# --------------------------------------------------------------------------- #


async def test_a_licence_without_an_actor_token_stays_a_service_principal(spy) -> None:  # noqa: ANN001
    from actor import NotAHumanPrincipalError

    actor = await _licence_actor(None)

    assert str(actor.tenant_id) == LICENCE_TENANT
    assert actor.subject_id == LICENCE_SUBJECT
    assert actor.is_service_principal is True
    with pytest.raises(NotAHumanPrincipalError):
        require_human(actor)


async def test_no_token_is_logged(spy, realm_key, caplog) -> None:  # noqa: ANN001
    """Tenant, app, subject and azp are logged; neither credential is."""
    import logging

    actor_token = mint(realm_key)
    with caplog.at_level(logging.DEBUG):
        await _licence_actor(actor_token)
        with pytest.raises(UnauthenticatedError):
            await _licence_actor(mint(realm_key, azp="other"))

    rendered = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert OPAQUE_TOKEN not in rendered
    assert actor_token not in rendered
    assert actor_token.split(".")[2] not in rendered
