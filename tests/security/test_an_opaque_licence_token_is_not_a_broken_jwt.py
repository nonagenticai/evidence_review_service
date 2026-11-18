"""The credential the gateway actually uses could not authenticate at all.

`KAG_ONBEHALF_TOKEN` is a 43-character **opaque licence token with zero `.`
separators**. `get_actor` handed every bearer to `verify_token`, which handed it
to `PyJWKClient.get_signing_key_from_jwt`, which logged `Not enough segments` —
so `GET /reviews/*` returned 401 across the whole surface when driven with the
credential the whole surface is driven with. The service was up, healthy, and
unauthenticable.

An opaque licence token is not a malformed JWT. It is a different credential
shape, and the answer is to introspect it.

## What this file pins, and why it is not one test

The fix adds a rung, and a rung is exactly the kind of change that can be wrong
in the *safe-looking* direction. Two properties therefore have to hold at once
and they pull against each other:

* **A JWT that fails verification must never reach the licence rung.** A
  fall-through there is an authentication bypass: a forger fails the signature
  check and gets a second, weaker attempt. `test_a_forged_jwt_…` asserts on the
  introspection SPY, not on the status code — both paths end in 401, so a status
  code could not tell "refused" from "refused twice", and the bypass would be
  invisible to a test that only read the response.
* **A token that is not a JWT at all must reach it.** Otherwise the defect above
  is still live and the change is decoration.

`looks_like_jwt` is the only thing separating the two, so it is tested in both
directions with a positive control: a predicate that always returned False would
satisfy every opaque case here and is exactly the mistake that would re-open the
bypass.

## No database

Every test drives `get_actor` as a function. The ladder is a property of that
function, not of any route, and a test that needed a live postgres to assert it
would be skipped on the machines where an auth change is actually written.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.dependencies import UnauthenticatedError, get_actor
from auth.verifier import looks_like_jwt

REALM_URL = "https://auth.test.invalid"
REALM = "portal"
ISSUER = f"{REALM_URL}/realms/{REALM}"

INTROSPECT_URL = "http://licenses.test.invalid/license/introspect"

#: The SHAPE under test: 43 characters, not one `.`. Deliberately readable
#: English rather than 43 random characters — the shape is the whole of what
#: matters here, and a high-entropy literal in a repo with a secret scanner is a
#: false positive somebody has to triage forever.
OPAQUE_TOKEN = "opaque-licence-token-shape-not-a-credential"  # nosec B105

TENANT = str(uuid.uuid4())
SUBJECT = "019f2a06-6f0d-7000-8000-000000000001"


def _settings(**overrides: Any):  # noqa: ANN201 - Settings is a plain dataclass
    """Auth ON, pointed at a fake realm and a fake introspector.

    Built from a live `Settings()` so a field added later is inherited rather
    than silently defaulted to something this file invented.
    """
    from config import Settings

    # Merged into a dict first, not splatted after the defaults: `replace()`
    # raises `TypeError: got multiple values` if an override names a field that
    # is also passed literally, and `license_introspect_url` is exactly the field
    # every interesting test here overrides.
    fields: dict[str, Any] = {
        "enable_auth": True,
        "environment": "test",
        "keycloak_url": REALM_URL,
        "keycloak_realm": REALM,
        "license_introspect_url": INTROSPECT_URL,
    }
    fields.update(overrides)
    return dataclasses.replace(Settings(), **fields)


class IntrospectSpy:
    """Records every call, so "was this path even reached" is assertable.

    The whole safety property of the ladder is about which rung a credential
    reaches. Only a spy can see that; a 401 cannot.
    """

    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.tokens: list[str] = []
        self.urls: list[str | None] = []

    async def __call__(self, token: str, *, url: str | None = None) -> dict[str, Any] | None:
        self.tokens.append(token)
        self.urls.append(url)
        return self.answer


ACTIVE_LICENCE: dict[str, Any] = {
    "active": True,
    "tenant_id": TENANT,
    "subject_id": SUBJECT,
    "license_id": str(uuid.uuid4()),
}


@pytest.fixture()
def spy(monkeypatch):  # noqa: ANN201, ANN001
    """The default: an ACTIVE licence. Individual tests re-point `answer`."""
    recorder = IntrospectSpy(dict(ACTIVE_LICENCE))
    monkeypatch.setattr("api.dependencies.introspect_license", recorder)
    return recorder


@pytest.fixture(scope="module")
def realm_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def attacker_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def jwks_returns_the_realm_key(monkeypatch, realm_key):  # noqa: ANN201, ANN001
    """Stub the key LOOKUP only; `jwt.decode` runs for real.

    So a token minted by `attacker_key` genuinely fails the signature check
    rather than failing because a fetch was stubbed out — the difference between
    testing verification and testing a mock.
    """
    from auth import verifier

    monkeypatch.setattr(verifier._CACHE, "signing_key", lambda _url, _token: realm_key.public_key())


def mint(key, **claims: Any) -> str:  # noqa: ANN001
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "tenant_id": TENANT,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256")


# --------------------------------------------------------------------------- #
# The shape predicate, in both directions.
# --------------------------------------------------------------------------- #


def test_the_onbehalf_token_shape_is_not_a_jwt() -> None:
    assert not looks_like_jwt(OPAQUE_TOKEN)
    assert len(OPAQUE_TOKEN) == 43, "the shape under test is the 43-char opaque licence token"
    assert "." not in OPAQUE_TOKEN


def test_a_real_jwt_is_recognised_as_one(realm_key) -> None:  # noqa: ANN001
    """The positive control.

    Without this, `looks_like_jwt` could be `return False` and every other test
    in this file would still pass — while the forged-JWT bypass was wide open.
    """
    assert looks_like_jwt(mint(realm_key))


@pytest.mark.parametrize(
    "token",
    [
        "",
        "onesegment",
        "two.segments",
        "a..b",  # three segments, but no signature to check
        ".b.c",
        "a.b.",
        "four.seg.ments.here",
    ],
)
def test_anything_that_is_not_three_non_empty_segments_is_not_a_jwt(token: str) -> None:
    assert not looks_like_jwt(token)


# --------------------------------------------------------------------------- #
# The licence rung.
# --------------------------------------------------------------------------- #


async def test_an_opaque_licence_token_authenticates(spy) -> None:  # noqa: ANN001
    """The defect, closed. On `ddc5015` this raises `UnauthenticatedError`."""
    actor = await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())

    assert spy.tokens == [OPAQUE_TOKEN], "the opaque token must reach introspection"
    assert spy.urls == [INTROSPECT_URL], "the URL must come from Settings, not from the ambient env"
    assert str(actor.tenant_id) == TENANT
    assert actor.subject_id == SUBJECT


async def test_a_licence_actor_may_read_but_may_not_pass_for_a_human(spy) -> None:  # noqa: ANN001
    """The two defaults chosen for what the payload does not carry.

    Introspection returns no role and no principal kind. Read authority is
    granted (that is the whole point of the fix) and the ledger guard is not: a
    licence token cannot record a screening decision, so unblocking `GET
    /reviews/*` does not quietly unblock attribution-bearing writes.
    """
    from actor import ActorRole, NotAHumanPrincipalError, require_human

    actor = await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())

    assert actor.role is ActorRole.REVIEWER, "a role-less credential must never confer LEAD"
    assert actor.is_service_principal is True
    assert actor.is_human is False
    with pytest.raises(NotAHumanPrincipalError):
        require_human(actor)


async def test_a_two_segment_string_is_treated_as_opaque(spy) -> None:  # noqa: ANN001
    """Not a JWT is not a JWT, whatever it happens to contain.

    Two segments cannot be verified, so calling it a failed JWT would report a
    signature check that never ran.
    """
    await get_actor(authorization="Bearer two.segments", settings=_settings())
    assert spy.tokens == ["two.segments"]


async def test_an_inactive_licence_is_refused(spy) -> None:  # noqa: ANN001
    spy.answer = {"active": False, "tenant_id": TENANT, "subject_id": SUBJECT}
    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())


async def test_an_active_licence_with_no_tenant_is_refused(spy) -> None:  # noqa: ANN001
    """`bind_actor` has nothing to scope RLS by, so this is unusable, not partial."""
    spy.answer = {"active": True, "subject_id": SUBJECT}
    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())


async def test_an_unreachable_introspector_is_refused(spy) -> None:  # noqa: ANN001
    """`None` is what `kag_common` returns for a transport failure or a non-200.

    Fails CLOSED. The alternative — admitting a caller whose credential nothing
    could check — makes authentication weakest exactly when the authority is
    down, which is the moment an attacker would pick.
    """
    spy.answer = None
    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())


async def test_an_introspector_that_raises_is_refused(spy, monkeypatch) -> None:  # noqa: ANN001
    """A 200 whose body is a list makes `kag_common` raise `AttributeError`.

    An unmodelled shape from the authority is not an authentication, and it must
    not escape as a 500 either.
    """

    async def _boom(token: str, *, url: str | None = None) -> dict[str, Any] | None:
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr("api.dependencies.introspect_license", _boom)
    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())


async def test_an_unconfigured_introspect_url_refuses_without_calling_out(spy) -> None:  # noqa: ANN001
    """The recorded decision for (5): unset is a REFUSAL, and a silent one upstream.

    Two things at once. It must not become "allow" — that would make a missing
    manifest line into an open door. And it must not become a call either: with
    no URL there is nobody to ask, and `kag_common` would only re-read the
    ambient environment and reach a different answer than `Settings` did.
    """
    with pytest.raises(UnauthenticatedError):
        await get_actor(
            authorization=f"Bearer {OPAQUE_TOKEN}",
            settings=_settings(license_introspect_url=""),
        )
    assert spy.tokens == [], "with no introspector configured there is nothing to ask"


async def test_every_licence_refusal_reads_identically_to_the_caller(spy, monkeypatch) -> None:  # noqa: ANN001
    """Refusals must not be a probing oracle.

    "not active", "could not be reached" and "never configured" are three
    different operator problems and one caller-visible answer. If they differed
    on the wire, a caller could learn whether the token they hold exists by
    reading the message.
    """
    details: set[str] = set()

    for answer in (None, {"active": False}):
        spy.answer = answer
        with pytest.raises(UnauthenticatedError) as caught:
            await get_actor(authorization=f"Bearer {OPAQUE_TOKEN}", settings=_settings())
        details.add(caught.value.detail)

    with pytest.raises(UnauthenticatedError) as caught:
        await get_actor(
            authorization=f"Bearer {OPAQUE_TOKEN}",
            settings=_settings(license_introspect_url=""),
        )
    details.add(caught.value.detail)

    assert len(details) == 1, f"the licence rung leaks which check failed: {sorted(details)}"


# --------------------------------------------------------------------------- #
# The safety property: a JWT never falls through.
# --------------------------------------------------------------------------- #


async def test_a_forged_jwt_is_refused_and_never_reaches_introspection(
    spy,
    jwks_returns_the_realm_key,  # noqa: ANN001
    attacker_key,  # noqa: ANN001
) -> None:
    """⛔ The bypass this ladder exists not to open.

    A token minted by the WRONG key is three non-empty segments, so it is a JWT,
    so its signature failure is final. If it fell through to introspection, a
    forger would simply lose one round and get another — and the response would
    be a 401 either way, so only the spy can see it.
    """
    forged = mint(attacker_key, sub="attacker-mallory")

    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {forged}", settings=_settings())

    assert spy.tokens == [], (
        "a JWT that failed verification reached the licence introspection path. That is an "
        "authentication bypass, not a fallback: the credential gets a second, weaker attempt."
    )


async def test_an_expired_jwt_is_refused_and_never_reaches_introspection(
    spy,
    jwks_returns_the_realm_key,  # noqa: ANN001
    realm_key,  # noqa: ANN001
) -> None:
    """The same property for the other common JWT failure.

    A correctly signed but expired token is still a JWT. "Expired" must not
    become "try the other authenticator".
    """
    stale = mint(realm_key, exp=int(time.time()) - 3600)

    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization=f"Bearer {stale}", settings=_settings())

    assert spy.tokens == []


async def test_a_valid_jwt_still_authenticates_and_is_not_introspected(
    spy,
    jwks_returns_the_realm_key,  # noqa: ANN001
    realm_key,  # noqa: ANN001
) -> None:
    """The control for the two above: the JWT rung still works, unchanged.

    Without it, a `get_actor` that refused every JWT would satisfy both bypass
    tests perfectly.
    """
    actor = await get_actor(authorization=f"Bearer {mint(realm_key)}", settings=_settings())

    assert str(actor.tenant_id) == TENANT
    assert actor.subject_id == SUBJECT
    assert actor.is_service_principal is False
    assert spy.tokens == [], "a JWT must never be sent to the licence introspector"


async def test_an_empty_bearer_is_refused_without_an_introspection(spy) -> None:  # noqa: ANN001
    with pytest.raises(UnauthenticatedError):
        await get_actor(authorization="Bearer ", settings=_settings())
    assert spy.tokens == []
