"""Request-scoped dependencies: who is calling, and inside which tenant.

`01-…:687` — *"Derive actor from verified credentials. Reject or ignore
actor/reviewer/tenant body fields."* This module is the only place an `Actor` is
constructed from a request, so "the body cannot name the reviewer" is a property
of there being no other constructor rather than a rule every router has to
remember.

Two headers, in priority order:

* ``X-Research-Context: Bearer <token>`` — the fleet's service-audience research
  context, which is what production sends through the KAG gateway.
* ``Authorization: Bearer <token>`` — a Keycloak `portal` realm access token,
  which is what a browser session and the WS-8 run present.

…carrying either of **two credential shapes**, which is a distinction this
module got wrong for its whole life:

* a **JWT**, verified locally against the realm JWKS (`auth.verifier`);
* an **opaque licence token**, which has no segments and no signature and can
  only be authenticated by asking the licenses service to introspect it.

`KAG_ONBEHALF_TOKEN` — the credential the gateway actually presents — is the
second kind, and handing it to a JWT verifier produced `Not enough segments` and
a 401 on every route. `looks_like_jwt` is the branch between the two, and the
order it is applied in is a security property rather than a style: see the
comment in `get_actor`.

On the licence rung ONLY, ``X-Actor-Token: <Keycloak user JWT>`` may name the
human who acted (azp on ``ER_ACTOR_TOKEN_AZP``); the tenant still comes from the
licence. Every other rung ignores that header. See `_human_on_licence`.

With ``ENABLE_AUTH=false`` (local, CI) the headers ``X-Tenant-Id`` /
``X-Subject-Id`` / ``X-Actor-Role`` are honoured instead. That switch is exactly
the shape of a production auth bypass, so it is refused outright when
``ENVIRONMENT`` is ``production``: a service that can be told to trust headers is
one misapplied ConfigMap away from accepting a forged reviewer identity.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Header
from kag_common.auth import introspect_license

from actor import Actor, ActorRole
from api.problem_details import ForbiddenError, ProblemError
from auth.verifier import TokenVerificationError, looks_like_jwt, verify_token_async
from config import Settings, get_settings
from log_config import get_logger

logger = get_logger(__name__)

#: The one refusal every credential failure is reported as.
#:
#: An expired JWT, a forged signature, a revoked licence, an introspector that
#: is down and an introspector that was never configured all produce this exact
#: string. `auth/verifier` states the reason and it applies just as much on the
#: licence rung: telling a caller WHICH check failed is a probing oracle — the
#: difference between "not active" and "could not be checked" tells an attacker
#: whether the token they hold exists at all. The distinctions are made in the
#: logs, where the operator can see them and the caller cannot.
_CREDENTIAL_REFUSED = "the presented credential could not be verified"


class UnauthenticatedError(ProblemError):
    status, slug, title = 401, "unauthenticated", "Unauthenticated"


async def _verified_claims(token: str, settings: Settings) -> dict[str, Any]:
    """Claims from a token whose SIGNATURE has been checked.

    This used to be `_decode_unverified_claims`, which base64-decoded the
    payload and trusted it, on the grounds that verification belonged to the
    gateway. It did not: `kag` forwards `Authorization` verbatim, and this
    service has no auth middleware — so any pod that could reach 8810 could
    name any reviewer. Attribution is the whole product here, and an
    unverified subject makes the decision ledger forgeable.

    `verify_token_async` rather than `verify_token`: `get_actor` is now a
    coroutine, so the JWKS fetch would otherwise run on the event loop. See that
    function for the whole argument.
    """
    try:
        return await verify_token_async(token, settings)
    except TokenVerificationError as exc:
        raise UnauthenticatedError(str(exc)) from exc


async def _actor_from_licence(token: str, settings: Settings, actor_token: str | None = None) -> Actor:
    """Authenticate an OPAQUE licence token by asking the licenses service.

    There is nothing in the credential to check locally — it is 43 characters of
    random with no structure — so the tenant and subject can only come from
    `POST /license/introspect`. `kag_common.auth.introspect_license` is that call:
    pooled client, per-token single-flight, five-minute positive cache and a
    short negative cache so a re-issued licence is picked up quickly. This
    service does not reimplement any of it.

    ## Decision: an introspector we cannot reach is a REFUSAL, never an allow

    `introspect_license` answers `None` for two very different things, and
    `kag_common` cannot tell them apart for us:

    * **`LICENSE_INTROSPECT_URL` unset** — no transport is even attempted, and it
      never clears by itself. 100% of opaque credentials fail until somebody
      edits a manifest.
    * **licenses unreachable, or a non-200, or a non-JSON body** — a blip that
      does clear by itself, and is not memoised.

    The URL case is separated *here*, before the call, purely so the log line can
    name it: a config miss and an outage need opposite responses from an
    operator, and "introspection produced no answer" is not a diagnosis.

    Both, though, fail the same way: **401**. The alternative — admitting a
    caller whose credential nothing was able to check — would make this service's
    authentication strictly weaker exactly when its authority is unavailable,
    which is the moment an attacker would choose. Availability is not the
    property this service exists to protect; attribution is (`ER-004.3`,
    "decisions are authentication-derived"), and a decision recorded under an
    unchecked credential is worse than a decision that could not be recorded. The
    unset-URL log is `ERROR` rather than `WARNING` for the same reason: it is the
    one of the two that a human must act on before anything works again.
    """
    url = settings.license_introspect_url.strip()
    if not url:
        logger.error(
            "LICENSE_INTROSPECT_URL is not set, so an opaque licence token cannot be "
            "authenticated and is refused; set it to the licenses service's "
            "/license/introspect endpoint",
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    # `url=` explicitly rather than letting `kag_common` re-read the environment
    # per call: this service's configuration lives in `Settings`, and a value it
    # can see is a value its tests can set.
    try:
        data = await introspect_license(token, url=url)
    except Exception as exc:  # noqa: BLE001 - transport/parse shapes kag_common does not model
        # `_introspect_uncached` swallows `httpx.HTTPError`/`OSError`, but a 200
        # whose JSON body is a list reaches `data.get(...)` inside it and raises
        # `AttributeError` — an unmodelled shape, documented as such in
        # `kag_graph_service/src/auth/middleware.py`. A malformed answer from the
        # authority is not an authentication.
        logger.warning("license introspection raised", extra={"error": str(exc)})
        raise UnauthenticatedError(_CREDENTIAL_REFUSED) from exc

    if not isinstance(data, dict):
        logger.warning("license introspection returned no answer; the token is refused")
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    if not data.get("active"):
        # A POSITIVE "this credential is dead" — expired, revoked, unknown. Kept
        # distinct from the branch above in the log, and identical to it on the
        # wire.
        logger.info("license introspection reports the token is not active")
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    app_key = _licence_app_key(data)
    accepted = settings.er_accepted_licence_apps
    if accepted and app_key not in accepted:
        # A live licence for ANOTHER app is not a credential for this one
        # (tenant x app licences, each hop authenticates for the TARGET app).
        # Same wire answer as every other refusal: no oracle.
        logger.warning(
            "licence refused: its app is not accepted by this service",
            extra={"app": app_key, "tenant_id": str(data.get("tenant_id") or ""), "accepted_apps": list(accepted)},
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    licence_actor = _actor_from_introspection(data)

    if actor_token is None:
        logger.info(
            "licence actor authenticated",
            extra={
                "tenant_id": str(licence_actor.tenant_id),
                "app": app_key,
                "actor_subject": licence_actor.subject_id,
                "azp": None,
                "service_principal": True,
            },
        )
        return licence_actor

    return await _human_on_licence(actor_token, licence_actor, app_key, settings)


def _licence_app_key(data: dict[str, Any]) -> str | None:
    """The introspected licence's app key: `app` is a NESTED object (`{"id", "key", …}`)."""
    app = data.get("app")
    if not isinstance(app, dict):
        return None
    key = str(app.get("key") or "").strip()
    return key or None


async def _human_on_licence(actor_token: str, licence_actor: Actor, app_key: str | None, settings: Settings) -> Actor:
    """The licence names the TENANT; `X-Actor-Token` names the HUMAN who acted.

    kag-frontend owns tenants that do not exist in Keycloak's `tenant_id` claim,
    so it authenticates to this service with that tenant's own
    `evidence-review` licence and attaches its user's Keycloak JWT to say who
    made the decision (screening decisions need a person, `require_human`).

    Three rules, each a security property rather than a convenience:

    * **The tenant comes from the licence ONLY.** Every tenant claim in the JWT
      is ignored: the JWT proves who the person is, never which tenant they act
      in, so a user cannot carry a portal tenant into a kag tenant's data.
    * **`azp` must be on `ER_ACTOR_TOKEN_AZP`.** Only a client this service
      expects to front humans may vouch; an empty list refuses every actor token.
    * **A present header never degrades.** An invalid, expired, wrong-`azp`,
      subject-less or service-account token is a 401 — never a silent fall back
      to the licence's service principal, which would hide a broken caller and
      record nothing it could be held to.
    * **Only an access token.** `typ` must be `Bearer` (case-insensitive), so an
      ID token for an accepted client cannot stand in for one.

    **Not checked here: that the actor is a MEMBER of the licence's tenant.**
    This service cannot answer that — kag-frontend's tenants are not in
    Keycloak — and does not try. The trust boundary is the licence holder: only
    the tenant's own app (kag-frontend) can mint that tenant's
    `evidence-review` licence token, and it enforces membership before it
    attaches `X-Actor-Token`. A caller holding a tenant's licence is trusted to
    name which of its users acted; the JWT only proves that person exists.
    """
    token = actor_token.strip()
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    if not token or not looks_like_jwt(token):
        logger.warning("X-Actor-Token refused: not a JWT", extra={"tenant_id": str(licence_actor.tenant_id)})
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    claims = await _verified_claims(token, settings)

    # Keycloak stamps `typ` on everything it signs with the realm key: "Bearer"
    # on an access token, "ID" on an ID token, "Refresh"/"Offline" on refresh
    # tokens. Only an access token is a statement the user authorised a client
    # to act; an ID token is addressed to the client itself and must not be
    # replayed as a credential.
    if str(claims.get("typ") or "").strip().lower() != "bearer":
        logger.warning(
            "X-Actor-Token refused: not an access token",
            extra={"tenant_id": str(licence_actor.tenant_id), "typ": str(claims.get("typ") or "") or None},
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    azp = str(claims.get("azp") or "").strip()
    if azp not in settings.er_actor_token_azp:
        logger.warning(
            "X-Actor-Token refused: azp not accepted",
            extra={
                "tenant_id": str(licence_actor.tenant_id),
                "app": app_key,
                "azp": azp or None,
                "accepted_azp": list(settings.er_actor_token_azp),
            },
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        logger.warning(
            "X-Actor-Token refused: no subject", extra={"tenant_id": str(licence_actor.tenant_id), "azp": azp}
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    if _is_service_principal(claims):
        # A client-credentials token minted for an accepted client has that
        # client's `azp` too. It names a machine, and the whole point of this
        # header is to name a person.
        logger.warning(
            "X-Actor-Token refused: it is a service-account token",
            extra={"tenant_id": str(licence_actor.tenant_id), "azp": azp},
        )
        raise UnauthenticatedError(_CREDENTIAL_REFUSED)

    actor = Actor(
        tenant_id=licence_actor.tenant_id,
        subject_id=subject,
        role=ActorRole.REVIEWER,
        is_service_principal=False,
        display_name=_display_name(claims, False),
    )
    logger.info(
        "licence actor authenticated with a human co-credential",
        extra={
            "tenant_id": str(actor.tenant_id),
            "app": app_key,
            "actor_subject": subject,
            "azp": azp,
            "service_principal": False,
        },
    )
    return actor


def _actor_from_introspection(data: dict[str, Any]) -> Actor:
    """An `Actor` built from an ACTIVE introspection payload.

    The payload (`licenses/main.py::TokenIntrospectResponse`) carries
    `tenant_id`, `subject_id` and `license_id`. It carries **no roles and no
    principal kind**, and the two defaults chosen for those absences are the
    substance of this function.

    **Role is `REVIEWER`, never `LEAD`.** `_role_from` already reasons that
    anything unrecognised is a reviewer because defaulting the other way makes a
    typo into a blinding bypass; a credential that cannot express a role at all
    is the strongest version of "unrecognised". A licence-token caller that needs
    to adjudicate or export must present a Keycloak token that says so.

    **`is_service_principal=True`.** Nothing in the payload distinguishes a
    person from a machine, and this file has already settled which way ambiguity
    resolves (`_is_service_principal`): "a machine mistaken for a person writes an
    unattributable row into an append-only ledger and nothing downstream can
    tell; a person mistaken for a machine gets a loud 403 and reports it within
    minutes". The asymmetry is the whole argument, and it is sharper here than
    there — the credential this rung exists for, `KAG_ONBEHALF_TOKEN`, IS a
    service credential, and a human at 8810 authenticates with a portal JWT down
    the other rung.

    So an opaque licence token can read the surface it was 401ing on, and
    `require_human` still refuses it a screening decision. If a genuine human
    path ever needs to arrive this way, it fails loudly with a message naming
    exactly this, which is the correct way for a guard like this to be wrong.

    `display_name` is `None` for the same reason it is `None` for a Keycloak
    service account: a licence has no name to put beside a decision.
    """
    tenant = str(data.get("tenant_id") or "").strip()
    subject = str(data.get("subject_id") or "").strip()
    if not tenant:
        # An active licence with no tenant is an unusable identity, not a
        # partially usable one: `bind_actor` has nothing to scope RLS by.
        raise UnauthenticatedError("credential carries no tenant")
    return Actor(
        tenant_id=_tenant_uuid(tenant, "the introspected licence's tenant"),
        subject_id=subject or None,
        role=ActorRole.REVIEWER,
        is_service_principal=True,
        display_name=None,
    )


#: The role names that confer `ActorRole.LEAD`. `review-lead` is the one the
#: `portal` realm defines; `evidence-review-lead` is kept because the fleet has
#: used both spellings and refusing it would be a silent authority loss on a
#: token minted correctly by somebody's reading of the docs.
#:
#: **`"lead"` was removed.** Measured live: the `portal` realm has 35 clients and
#: 30 of them are `fullScopeAllowed`, so a token minted for one client carries
#: the user's client roles for the others — and `_role_from` scans EVERY grant in
#: `resource_access`. A role called `lead` on a kanban board or a CRM client is an
#: ordinary, obvious name for somebody to add, and it would silently confer
#: evidence-review LEAD: adjudicate, unblind, seal a corpus, export an inclusion
#: set.
#:
#: No such client role exists in `portal` or `secure-apps` today — I enumerated
#: every client's roles — so this was latent, not live. `review-lead` and
#: `evidence-review-lead` are specific enough that nobody adds one by accident.
LEAD_ROLE_NAMES = frozenset({"review-lead", "evidence-review-lead"})


def _role_from(claims: dict[str, Any]) -> ActorRole:
    """A lead reads across reviewers; everyone else reads only their own work.

    Anything unrecognised is a reviewer. Defaulting the other way would make a
    typo in a realm role mapping into a blinding bypass.

    ## Why `resource_access` is read as well

    Keycloak puts REALM roles in `realm_access.roles` and CLIENT roles in
    `resource_access.<client_id>.roles`. This read only the first two shapes, so
    a `review-lead` granted as a client role on `portal-bff` — which is how a
    per-application role is normally scoped, and the shape an operator would
    reach for if they did not want the role visible to every client in the realm
    — produced a token that looked exactly like a plain reviewer's.

    Silently. There is no error for "you hold a role this service cannot see":
    the caller simply gets `ActorRole.REVIEWER`, and `require_review_lead` then
    falls back to the creator grant, so the only person who can close a review is
    whoever created it. If that account is gone, it can never be closed or
    re-protocoled. That is what made this a rollout prerequisite rather than a
    nicety.

    Every client's roles are scanned rather than one named client, deliberately.
    Pinning a client id here would make this service's authority depend on which
    client the token was minted for, so the same person would be a lead through
    the studio and a reviewer through a script — and the failure mode would again
    be silent.
    """
    roles: list[str] = []

    realm = claims.get("realm_access") or {}
    if isinstance(realm, dict):
        roles += [str(r) for r in realm.get("roles", []) if r]

    resource = claims.get("resource_access") or {}
    if isinstance(resource, dict):
        for grant in resource.values():
            if isinstance(grant, dict):
                roles += [str(r) for r in grant.get("roles", []) if r]

    roles += [str(r) for r in claims.get("roles", []) if r]

    if any(r.lower() in LEAD_ROLE_NAMES for r in roles):
        return ActorRole.LEAD
    return ActorRole.REVIEWER


#: Claims Keycloak emits **only** on a client-credentials (service account)
#: token. They describe the calling client, which a browser-obtained user token
#: has nothing to say about.
#: `client_id` is the snake_case spelling newer Keycloak releases emit from the
#: `service_account` client scope; older realms emit `clientId`. Both are listed
#: so a realm upgrade cannot silently turn a machine into a person.
_SERVICE_ACCOUNT_CLAIMS = ("clientId", "client_id", "clientHost", "clientAddress")

#: Keycloak names every service account user `service-account-<clientId>`.
_SERVICE_ACCOUNT_USERNAME_PREFIX = "service-account-"


def _is_service_principal(claims: dict[str, Any]) -> bool:
    """Did a MACHINE present this token?

    `require_human` used to answer this with "is there a `sub`?", which is
    wrong: a Keycloak client-credentials token carries the service account
    user's UUID as `sub`. So every service principal passed the one check that
    exists to stop it, and `ER-004`'s guarantee held only on the header path.

    The markers below are specific rather than heuristic, deliberately. A guard
    that fires on ordinary reviewer tokens would be turned off within a week —
    the same failure mode the audience check in `auth/verifier` avoids — so
    this must not guess. Both markers are set by Keycloak itself and neither
    appears on a user token.

    Ambiguity resolves toward *refusing*, because the two failure directions
    are not symmetric: a machine mistaken for a person writes an unattributable
    row into an append-only ledger and nothing downstream can tell; a person
    mistaken for a machine gets a loud 403 and reports it within minutes.

    **Known boundary.** This closes the mechanical hole — client-credentials
    tokens. It does NOT catch a *named realm user* created to be driven by
    automation: the `portal` realm has several ("service-style users" such as
    `system-workflow-operator`, which n8n authenticates as for the kaizen
    streams). Those are ordinary users with an ordinary `sub` and none of the
    markers above, so they read as human here.

    That is deliberately left open rather than guessed at. Deciding which named
    realm identities count as people is a policy call, and the two candidate
    mechanisms — a username denylist, or a realm role — both need somebody to
    own the list. Encoding a guess would either miss identities or lock out
    reviewers, and the second failure is how guards get deleted.
    """
    username = str(claims.get("preferred_username") or "").strip().lower()
    if username.startswith(_SERVICE_ACCOUNT_USERNAME_PREFIX):
        return True
    return any(claims.get(name) for name in _SERVICE_ACCOUNT_CLAIMS)


def _tenant_uuid(value: str, source: str) -> uuid.UUID:
    """The tenant a credential names, refused here if it is not a UUID.

    Both paths below already rejected an EMPTY tenant. Neither checked its
    shape, and since `0014` made all 25 `tenant_id` columns `uuid`, an
    unparseable one no longer fails at the boundary — it fails at the first
    `set_config('app.current_tenant', …)::uuid` inside the request's
    transaction, as a psycopg `InvalidTextRepresentation`, which this service
    has no handler for and which therefore reaches the caller as a 500 naming
    the database. A malformed credential is the caller's fault and has to read
    like it.

    `UnauthenticatedError` rather than a new 400 class, because this is the same
    fault as the two refusals either side of it — "credential carries no tenant"
    and "X-Tenant-Id is required when auth is disabled" — and answering the same
    field with two different statuses is a contract a caller cannot branch on.

    The value is NOT echoed back. It arrives on an unauthenticated path and
    lands in a log line and a problem-details body; `source` names the header or
    claim to look at, which is the whole of what the caller needs to fix it.
    """
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise UnauthenticatedError(f"the tenant named by {source} is not a UUID") from exc


def _display_name(claims: dict[str, Any], is_service_principal: bool) -> str | None:
    """What to call this principal, or `None` if the token did not say.

    `name` is Keycloak's `profile` full-name mapper; `preferred_username` is the
    username mapper from the same scope and is the fallback rather than the
    first choice, because a username is an identifier a person recognises but
    not the name they go by.

    `None` is an ordinary outcome, not a failure: if the client does not carry
    the `profile` scope, neither claim is present and every caller falls back to
    the subject id, which is exactly what was rendered before this existed.

    Never for a service principal — a `service-account-…` username is not a
    person's name, and putting one beside a human decision misdescribes who
    acted.
    """
    if is_service_principal:
        return None
    return str(claims.get("name") or claims.get("preferred_username") or "").strip() or None


def _actor_from_claims(claims: dict[str, Any]) -> Actor:
    tenant = str(claims.get("tenant_id") or claims.get("tenant") or "").strip()
    subject = str(claims.get("sub") or "").strip()
    if not tenant:
        raise UnauthenticatedError("credential carries no tenant")
    is_service_principal = _is_service_principal(claims)
    return Actor(
        tenant_id=_tenant_uuid(tenant, "the credential's tenant claim"),
        subject_id=subject or None,
        role=_role_from(claims),
        is_service_principal=is_service_principal,
        display_name=_display_name(claims, is_service_principal),
    )


async def get_actor(
    x_research_context: Annotated[str | None, Header(alias="X-Research-Context")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_subject_id: Annotated[str | None, Header(alias="X-Subject-Id")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
    x_actor_token: Annotated[str | None, Header(alias="X-Actor-Token")] = None,
    settings: Annotated[Settings | None, Depends(get_settings)] = None,
) -> Actor:
    settings = settings or get_settings()

    if not settings.enable_auth:
        # A header-trusting mode is a bypass by construction. It exists for the
        # test suite and for local work, and it must be impossible to reach in
        # production even if someone sets the flag — hence the environment
        # check rather than a comment saying "do not do this".
        if settings.environment.lower() in {"production", "prod"}:
            raise ForbiddenError(
                "ENABLE_AUTH=false is refused in a production environment; header-derived identity "
                "would let any caller name themselves as any reviewer"
            )
        tenant = (x_tenant_id or "").strip()
        if not tenant:
            raise UnauthenticatedError("X-Tenant-Id is required when auth is disabled")
        role = ActorRole.LEAD if (x_actor_role or "").strip().lower() == "lead" else ActorRole.REVIEWER
        return Actor(
            tenant_id=_tenant_uuid(tenant, "X-Tenant-Id"),
            subject_id=(x_subject_id or "").strip() or None,
            role=role,
        )

    # Two credential SHAPES arrive on these headers, and the branch between them
    # is on the shape, not on whether verification failed.
    #
    # `licenses/auth/middleware.py:316→358→378` reaches the same property by
    # falling through: verify, then `if looks_like_jwt(token): hard 401`, then
    # introspect. Dispatching on the shape FIRST is that ladder with the rungs in
    # the other order, and it is stricter — a token that looks like a JWT can
    # never reach the introspect call at all, rather than reaching it only if a
    # guard above remembers to stop it.
    #
    # ⛔ Do not "simplify" this into `try: verify … except: introspect`. That is
    # the same code with the safety property removed: a forged JWT would fail the
    # signature check and then get a second, weaker attempt at authenticating,
    # which is an auth bypass. The two failure modes are described in full on
    # `auth.verifier.looks_like_jwt`.
    #
    # The precedence between the two headers is unchanged and remains declared by
    # `tests/security/test_research_context_header_precedence.py`.
    for header_name, header in (("X-Research-Context", x_research_context), ("Authorization", authorization)):
        if header and header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
            if not token:
                # `Bearer ` with nothing after it. Refused here rather than sent
                # to the introspector, which would answer inactive after a round
                # trip and log a token-shaped nothing.
                raise UnauthenticatedError(_CREDENTIAL_REFUSED)
            if looks_like_jwt(token):
                # `X-Actor-Token` is a co-credential of the LICENCE rung only. A
                # JWT caller already names its own subject; letting it attach a
                # second one would be impersonation by header, so it is ignored.
                if x_actor_token is not None:
                    logger.info("X-Actor-Token ignored: it is only honoured beside a licence credential")
                return _actor_from_claims(await _verified_claims(token, settings))
            if header_name != "Authorization":
                # A licence on `X-Research-Context` is a SERVICE hop (kag
                # forwarding its research context), not kag-frontend fronting a
                # person: `X-Actor-Token` is honoured only beside a licence on
                # `Authorization`. Here it is ignored and the licence stays the
                # service principal it always was.
                if x_actor_token is not None:
                    logger.info("X-Actor-Token ignored: the licence arrived on X-Research-Context, not Authorization")
                return await _actor_from_licence(token, settings)
            return await _actor_from_licence(token, settings, actor_token=x_actor_token)

    raise UnauthenticatedError("no X-Research-Context or Authorization bearer credential presented")


ActorDep = Annotated[Actor, Depends(get_actor)]
