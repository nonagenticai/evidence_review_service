"""Token signature verification against the Keycloak realm's JWKS.

## The defect this closes

`api/dependencies.get_actor` used to decode a bearer token's claims **without
verifying its signature**, on the stated grounds that verification is "the
gateway's and the auth middleware's job". This service has no auth middleware,
and the gateway in front of it (`kag`) forwards `Authorization` verbatim without
introspecting it. So the actual behaviour in production was: any caller that
could reach `evidence-review:8810` could present a self-made, unsigned JWT
naming any `sub` and any `tenant_id`, and the service would accept it.

That is not merely a missing check. Everything this service exists to guarantee
is *attribution*:

* `ER-004.3` — decisions are "authentication-derived";
* `01-…:687` — "derive actor from verified credentials";
* `ER-004`'s completion evidence — "service/n8n principals cannot submit a human
  decision".

A forgeable subject makes all three untrue. Reviewer blinding survives it
(the RLS policy still isolates by whatever subject is bound), but the *ledger*
does not: a decision could be recorded under a reviewer who never made it, and
nothing downstream could tell.

Being unreachable from the internet is not a substitute. The NetworkPolicy
admits the whole pod CIDR, so "only the gateway calls us" is a convention, not a
control — and the service's own threat model (`01-…:181`) names
"actor/context forgery" explicitly.

## What is verified

Signature (RS256/ES256 family, key resolved from the realm JWKS by `kid`),
expiry, and issuer. The audience is deliberately NOT enforced: this fleet's
`portal` tokens carry `account` as the audience and are consumed by several
services, so an `aud` check would reject every real token and the pressure would
be to disable verification entirely — a check nobody can satisfy gets removed,
and then nothing is verified at all.

JWKS is fetched once and cached, with a single forced refresh on an unknown
`kid` so a Keycloak key rotation recovers without a restart.

## What this module does NOT verify

A JWT is only one of the two credential shapes that reach 8810. The other is the
fleet's **opaque licence token**, which has no segments, no signature and no
claims, and which nothing here can check. `looks_like_jwt` is the boundary
between the two, and `api/dependencies` routes an opaque token to the licenses
service instead. Keeping that boundary a positive test for the JWS shape — never
"verification failed, try something else" — is what stops a forged JWT from
getting a second attempt at authenticating.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import jwt
from jwt import PyJWKClient

from config import Settings
from log_config import get_logger

logger = get_logger(__name__)

#: How long a fetched JWKS is trusted before it is re-fetched. A rotation is
#: also handled immediately by the unknown-`kid` refresh below; this bound is
#: for the case where a key is *removed*.
JWKS_TTL_SECONDS = 3600

#: Accepted signing algorithms. Symmetric algorithms are excluded on purpose:
#: accepting `HS256` alongside an RSA JWKS is the classic confusion attack,
#: where the public key is presented as an HMAC secret.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

#: Sent on the JWKS fetch, and load-bearing.
#:
#: `PyJWKClient` fetches with `urllib.request.urlopen`, which sends
#: `User-Agent: Python-urllib/3.x`. `auth.nonagentic.ai` is Cloudflare-fronted
#: and its bot protection returns **403 for that UA** — so the first deploy of
#: this verifier could not fetch a key at all, every token failed to verify, and
#: every authenticated request to 8810 returned 401. The service was up, healthy
#: and completely unusable.
#:
#: The failure had exactly the shape that hides: `/health` and `/health/ready`
#: stayed green because neither touches Keycloak, and the only symptom was one
#: `could not resolve a signing key` warning per request in a log nobody was
#: watching.
#:
#: The in-cluster Keycloak (the in-cluster Keycloak service) would
#: avoid Cloudflare entirely and is what the rest of the fleet uses, but it is
#: not reachable from this pod today — it resets the connection, so the mesh
#: policy is missing too. That is the better end state and a separate change;
#: this one makes the public path work.
JWKS_REQUEST_HEADERS = {
    "User-Agent": "evidence-review/1.0 (+https://nonagentic.ai) python-jwt",
    "Accept": "application/json",
}


class TokenVerificationError(Exception):
    """The presented credential is not a valid token for this realm."""


class _JWKSCache:
    """One `PyJWKClient` per issuer, refreshed on TTL or on an unknown key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: PyJWKClient | None = None
        self._url: str | None = None
        self._fetched_at: float = 0.0

    def signing_key(self, jwks_url: str, token: str) -> Any:
        """Resolve the signing key. **Blocking** — never call this on the loop.

        `PyJWKClient` fetches over `urllib.request.urlopen`, a synchronous socket
        read with a 10s timeout, and the unknown-`kid` branch below can perform
        TWO of them back to back. `verify_token_async` is the entry point that
        keeps that off the event loop; see its docstring for why the distinction
        is load-bearing rather than tidy.
        """
        with self._lock:
            stale = (time.monotonic() - self._fetched_at) > JWKS_TTL_SECONDS
            if self._client is None or self._url != jwks_url or stale:
                self._client = self._build(jwks_url)
                self._url = jwks_url
                self._fetched_at = time.monotonic()
            client = self._client

        try:
            return client.get_signing_key_from_jwt(token).key
        except Exception:
            # An unknown `kid` is the normal shape of a key rotation. Refetch
            # once and retry, so a rotation does not require a pod restart —
            # and so the failure that IS reported is a real one.
            with self._lock:
                self._client = self._build(jwks_url)
                self._url = jwks_url
                self._fetched_at = time.monotonic()
                client = self._client
            return client.get_signing_key_from_jwt(token).key

    @staticmethod
    def _build(jwks_url: str) -> PyJWKClient:
        """⚠️ **A KNOWN, DELIBERATELY UNBOUNDED GAP. Read this before "fixing" it.**

        `timeout=10` reaches `urlopen` -> `socket.create_connection`, which calls
        `socket.getaddrinfo` FIRST — and `getaddrinfo` takes no timeout. So this
        has the identical hole that `bounded_db` exists for: a stalled resolver
        blocks outside the stated bound, measured at 30 s in this environment,
        and the unknown-`kid` branch above does it TWICE back to back. About
        60 s, worst case, against external `auth.nonagentic.ai`.

        **It is deliberately NOT given a wall-clock deadline, and the reason is
        the trade rather than the effort.**

        Against it — the blast radius here is latency, not the failure class the
        rest of this change is about:

        * It is off the event loop AND off the limiter. `verify_token_async`
          dispatches through `asyncio.to_thread`, which uses asyncio's default
          executor — **not** the anyio threadpool `app.py` caps to
          `pool_size + max_overflow`. A stalled JWKS fetch therefore consumes no
          anyio token and no database connection, which is precisely what makes
          `health.ready()` and `worker.tick()` urgent and this not.
        * It cannot restart the pod. `/health/live` is `async` and touches
          nothing; `/health/ready` does not authenticate. Neither probe can
          observe a slow JWKS, so no amount of it produces the exit-137 signature.
        * The fetch happens OUTSIDE `self._lock` — only `_build` is inside it,
          and `PyJWKClient` fetches lazily — so one stalled request does not
          block the others.
        * `cache_keys=True` with a 1 h TTL means this path runs on cold start,
          on rotation, and once an hour. It is not on the hot path.

        For it — **a wrong bound here is an authentication outage.** Abandoning
        a JWKS fetch turns a transient resolver stall during a key rotation into
        a refusal for every request in that window, and the natural place to
        report it is a 401, which is a hard failure a client must not retry. The
        thing this module protects — a forged signature never getting a second
        chance — is worth more than the latency a bound would save.

        ⛔ So if this IS bounded later, the shape matters: a deadline on the
        WHOLE verify (about 25 s, above `2 x timeout` so it cannot fire on a
        fetch that is merely slow), reported as a **503 with `Retry-After`**,
        never a 401. Bounding it as an auth failure would be worse than the gap.

        What would change this decision: evidence that requests are actually
        piling up on asyncio's default executor (32 threads) during a resolver
        stall. Nothing measured says they are, and the honest state of that is
        "not proven either way" rather than "fine".
        """
        return PyJWKClient(
            jwks_url,
            cache_keys=True,
            headers=JWKS_REQUEST_HEADERS,
            timeout=10,
        )


_CACHE = _JWKSCache()


def issuer_for(settings: Settings) -> str:
    return f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"


def jwks_url_for(settings: Settings) -> str:
    return f"{issuer_for(settings)}/protocol/openid-connect/certs"


def looks_like_jwt(token: str) -> bool:
    """Is this credential SHAPED like a JWS compact serialization?

    ## The two failure modes this keeps apart

    Both end in a 401, which is exactly why merging them is so easy and so bad:

    * **A JWT that did not verify** — a forged signature, an expired token, one
      minted by another realm. The answer is a hard refusal, and it must NOT be
      retried against any other authentication method. A second chance after a
      failed signature check is an authentication bypass: the forger loses one
      round and wins the next, and the surviving log line says "authenticated".
    * **Not a JWT at all** — the fleet's opaque licence token (`KAG_ONBEHALF_TOKEN`
      is 43 characters with zero `.` separators). There is no signature to check
      and no payload to read. `verify_token` used to hand it to
      `PyJWKClient.get_signing_key_from_jwt`, which logged `Not enough segments`,
      so the credential the gateway actually drives 8810 with could not
      authenticate at all and `GET /reviews/*` 401'd across the whole surface.
      The answer here is to introspect the token, not to refuse it.

    A predicate that let the first case reach the second case's path would turn a
    verification failure into a fallback, so this must be a positive test for the
    JWS shape and never a negative test for "did verification fail".

    An EMPTY segment counts as "not a JWT". `"a..b"` carries no signature and can
    never verify; treating it as a JWT would mean refusing it as though a real
    signature had been checked, which misdescribes what happened.

    Mirrors `licenses/auth/jwt_verify.looks_like_jwt` deliberately and to the
    character. Both services sit on the same hop reading the same
    `Authorization` header, and a shape predicate that disagreed between them
    would make one credential authenticate at one service and 401 at the other —
    a discrepancy nobody would think to look for.
    """
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


async def verify_token_async(token: str, settings: Settings) -> dict[str, Any]:
    """`verify_token`, dispatched off the event loop.

    This is the entry point every request path must use, and the sync
    `verify_token` beside it is for tests and for callers that are already in a
    thread.

    Why it exists: `get_actor` used to be a plain `def`, so FastAPI ran it — and
    the blocking JWKS fetch inside it — in the threadpool, and the event loop was
    never held. Introspecting an opaque licence token needs `await`, so `get_actor`
    became `async def`, and that single keyword silently moved the JWKS fetch ONTO
    the loop. Two `urlopen` calls with a 10s timeout each (the unknown-`kid`
    branch does the fetch twice) would then stall every other request in the
    process, `/health/live` included — the exact shape
    `tests/api/test_event_loop_is_not_blocked.py` exists to prevent, where kubelet
    restarts a pod that is merely busy rather than broken.

    An unknown `kid` is not a rare event: it is what a Keycloak key rotation looks
    like from here, and a single such token would be enough.

    `licenses` offloads the same hop the same way
    (`auth/middleware.py:290`, `await asyncio.to_thread(self._extract_user_from_jwt, …)`).
    """
    return await asyncio.to_thread(verify_token, token, settings)


def verify_token(token: str, settings: Settings) -> dict[str, Any]:
    """Return the token's claims, or raise.

    ⚠️ **Blocking.** Callers on the event loop must use `verify_token_async`.

    The caller is responsible for only reaching here with something
    `looks_like_jwt` accepts. This function will refuse an opaque licence token —
    correctly, since it cannot verify one — but the refusal would read as "this
    credential is invalid" when the truth is "this credential is not of this
    kind", and that mislabelling is what cost the whole `/reviews` surface.

    Every failure is the same class from the caller's point of view — an
    expired token, a forged signature and a token from another realm are all
    "this credential is not usable here". They are logged distinctly and
    reported identically, because telling a caller *which* check failed is a
    probing oracle.
    """
    try:
        key = _CACHE.signing_key(jwks_url_for(settings), token)
    except Exception as exc:  # noqa: BLE001 - JWKS transport/format failures
        logger.warning("could not resolve a signing key", extra={"error": str(exc)})
        raise TokenVerificationError("the presented credential could not be verified") from exc

    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer_for(settings),
            options={
                "require": ["exp", "iss", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                # See the module docstring: `aud` is not enforced, deliberately
                # and with the reason recorded, rather than silently skipped.
                "verify_aud": False,
            },
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        logger.info("token rejected", extra={"reason": type(exc).__name__})
        raise TokenVerificationError("the presented credential is not valid for this realm") from exc
