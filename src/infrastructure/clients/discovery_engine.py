"""Fetch a frozen review-export batch from `discovery-engine`, server to server.

## Why this exists at all

Screening's corpus used to arrive as bytes the **browser** built. `fr` posted
`JSON.stringify({records: toScreeningRecords(sessionSnapshot)})`: server-sourced
data re-shaped in the tab, which this service then digested and sealed. The
digest was therefore a statement about a document constructed on a machine
outside the trust boundary, and `import_batches.raw_payload_digest` — the value
the corpus manifest and the PRISMA `identified_total` ultimately rest on — named
that construction rather than anything discovery-engine ever froze.

Forwarding the bytes verbatim *through* `fr` was measured and is impossible:
its `HttpClient` exposes only `json<T>()`, so every hop is parse-then-reserialize
— an equal object, never equal bytes. And `fr` cannot check a digest itself:
there is no RFC 8785 canonicaliser in the frontend and `research_contracts`'
TypeScript package is not published to a public npm registry.

So the browser names a batch and this service goes and gets it. The check then
happens in Python, where the canonicaliser lives.

## Correction: for a while this route was added ALONGSIDE the old one

The paragraphs above described the browser as removed from the data path, and
that was only half true. `POST /api/reviews/{id}/imports?source_kind=discovery_session`
remained open: an arbitrary request body, no `expected_digest`, no contact with
discovery-engine, and `raw_payload_digest` written over whatever arrived.
Downstream the two were indistinguishable except by `source_ref.fetch_mode`,
which is null on that path and which nothing reads — so the digest underneath
`identified_total`, the corpus manifest and the PRISMA appendix could still be a
statement about bytes a tab built.

That route now refuses `discovery_session` (`api/routers/imports.py`,
`VERIFIED_ONLY_SOURCE_KINDS`) with a `422` naming this one, so the claim above
is true as of that change rather than aspirational. The database still accepts
the value — this route writes it for every legitimate pull, and the rows the old
path wrote are real imports whose provenance an auditor still needs.

## What the digest comparison does and does not prove

`expected_digest` is the receipt `fr` was handed when it took the batch
(`POST /api/sessions/{id}/review-export` → `{batch_id, digest, record_count}`).
Both that value and the bytes fetched here originate at discovery-engine, so
this comparison is **not** a proof against a dishonest upstream — a service that
lied twice would agree with itself.

What it does prove, and what was actually broken:

* the bytes sealed here are bytes discovery-engine froze, not bytes a tab built;
* the batch the human consented to is the batch that was imported — `fr` showing
  batch A's record count while this service imports batch B is caught, not
  reconciled;
* the transport did not truncate. A short read that still parsed would otherwise
  seal a subset of the corpus and report it as the whole one.

The stronger claim — a signature over the batch that discovery-engine cannot
forge — needs a signing key discovery-engine holds and is deliberately not
smuggled in under this name.

## Identity on this hop

`X-Tenant-Id: <uuid>`, bare. discovery-engine's middleware accepts that from a
peer inside `TRUSTED_CIDRS` (in-cluster pod traffic).

When `DISCOVERY_ENGINE_LICENSE_TOKEN` is set, the hop also carries this
service's own licence as `Authorization: Bearer <token>` — the delegated shape
discovery-engine requires once `AUTH_TOKENLESS_IDENTITY_MODE=enforce` refuses a
bare tenant. See `request_headers`.

⚠️ This paragraph used to say the hop derives an **empty** subject, so that "a
batch on a session a reviewer marked `visibility='user'` is a 404 here, not a
leak." **That was measured false on 2026-08-15.** `User.effective_subject_id`
(discovery-engine `auth/models.py`) falls back to `effective_tenant_id`, and its
own docstring records that returning `""` was tried and broke the read path,
because backfilled rows carry `subject_id == tenant_id`. So `scope()` matches
`visibility='user'` rows **whenever the session's `subject_id` equals the tenant
id** — true of 10 of the 12 sessions in production, including the one this
review runs against. An agent fetched such a session's batch over this exact hop
and got 200 and 463,762 bytes.

So the fetch below works, and it works because of an equality nobody declared.
The negative control holds: session `019f578a`, whose `subject_id` differs from
its tenant, is a genuine 404 on the same hop — the scoping is real, it simply
does not mean what this docstring claimed.

**The dependency, and when it breaks.** discovery-engine `middleware.py` names
the planned follow-up in which kag forwards a license token naming a real
subject. On the day that ships, new sessions get `subject_id != tenant_id` and
**every import here 404s against a batch that plainly exists.** The fix then is
to send a subject this service can name, not to widen discovery-engine's scope.
The dependency is pinned in discovery-engine by
`tests/unit/test_a_tenant_only_caller_is_not_subjectless.py`; when that goes red,
this client is what broke.

A 404 is still reported as a 404 rather than as "no such batch", because the two
have completely different remedies.

The tenant is the ACTOR's tenant, never a value from the request body. A caller
cannot name someone else's tenant and have this service fetch it for them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import quote

import httpx

#: The same ceiling `import_parser` enforces on an uploaded payload. Enforced
#: HERE as well, while streaming, because the parser's check happens after the
#: whole body is in memory — which is the moment a hostile or broken upstream
#: has already cost us the memory. `limits.memory` on this pod is 512Mi.
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


class DiscoveryFetchError(Exception):
    """Base for everything this module refuses on. Never leaks an httpx type."""


class UpstreamUnavailableError(DiscoveryFetchError):
    """discovery-engine could not be reached, or answered 5xx. Retry is correct."""


class BatchNotVisibleError(DiscoveryFetchError):
    """404 — no such batch on that session, FOR THIS TENANT's tenant-visible rows."""


class UpstreamRefusedError(DiscoveryFetchError):
    """A 4xx that is not a 404: the request was reached and rejected."""


class DocumentTooLargeError(DiscoveryFetchError):
    """The response exceeded `MAX_DOCUMENT_BYTES` and was abandoned mid-stream."""


@dataclass(frozen=True)
class FetchedBatch:
    """Exactly what came back, plus what we computed over it. Nothing derived."""

    #: The response body, byte for byte. NOT parsed and re-encoded: the digest
    #: below is only meaningful over the bytes as received.
    document: bytes
    #: `sha256:<hex>` computed here, over `document`. The authority.
    computed_digest: str
    #: `X-Document-Digest` as discovery-engine sent it, or None if absent. Kept
    #: separate from `computed_digest` so a disagreement between them is visible
    #: rather than averaged away.
    asserted_digest: str | None
    #: The URL fetched, recorded into `import_batches.source_ref` so provenance
    #: names a place a third party can go and look.
    url: str


def digest_of(payload: bytes) -> str:
    """`sha256:<64-lowercase-hex>` of exactly these bytes.

    Deliberately the same formula as `import_parser.payload_digest` here and
    `review_export_service.digest_of` in discovery-engine — one hash of one
    byte string, no canonicalisation. That is what makes
    `import_batches.raw_payload_digest` come out EQUAL to the batch's digest on
    the other side, so the provenance chain is checkable by anyone holding both
    numbers rather than only by this service.
    """
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def request_headers(*, tenant_id: str, license_token: str | None) -> dict[str, str]:
    """The headers of the fetch. Two shapes, and only two.

    * No licence configured: ``{X-Tenant-Id, Accept}`` — exactly the request
      this hop has always sent.
    * A licence configured: the SAME two, plus ``Authorization: Bearer <licence>``.
      This is discovery-engine's on-behalf shape: the licence authenticates THIS
      service (subject ``svc-evidence-review``), and the plain ``X-Tenant-Id``
      still names the customer tenant it acts for. The licence must NOT go into
      ``X-Tenant-Id: Bearer`` — that carrier binds the request to the licence's
      own tenant, and this service acts for many.

    The tenant is never replaced, wrapped or dropped by the licence. Under
    discovery-engine's ``observe`` mode the extra header is read only by
    ``auth/vouch.authorized_onbehalf``, which can only widen provenance, so the
    request is served identically; under ``enforce`` it is what keeps it served.
    """
    headers = {"X-Tenant-Id": tenant_id, "Accept": "application/json"}
    token = (license_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_review_export(
    *,
    base_url: str,
    tenant_id: str,
    session_id: str,
    batch_id: str,
    timeout_seconds: float,
    license_token: str | None = None,
) -> FetchedBatch:
    """GET the frozen batch and return its bytes, capped and digested.

    Streamed rather than read whole, so an oversized document is abandoned at
    the cap instead of after it has already been buffered.
    """
    # PERCENT-ENCODED, `safe=""`, and this is not defensive tidiness.
    #
    # `session_id` and `batch_id` are caller-supplied strings off the request
    # body, and interpolating them raw let a caller choose the PATH on
    # discovery-engine's host: measured, `session_id="x/../../../health"` builds
    # `.../api/sessions/x/../../../health/review-export/y`, and httpx normalises
    # the dot segments before sending, so the request that actually leaves is
    # `GET /health/review-export/y` — carrying this tenant's `X-Tenant-Id`.
    #
    # The damage is bounded (one host, GET only, and a response that is not a
    # session export is refused rather than imported), but the bound is a
    # property of the other endpoints rather than of this code, and "nothing
    # useful is reachable today" is not a thing a fetch path may rely on.
    # Encoding turns a path-choosing parameter back into an identifier: a `/` in
    # an id becomes `%2F` and 404s as the nonexistent session it is.
    url = f"{base_url.rstrip('/')}/api/sessions/{quote(session_id, safe='')}/review-export/{quote(batch_id, safe='')}"
    headers = request_headers(tenant_id=tenant_id, license_token=license_token)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    # THREE causes, and the third one was missing.
                    #
                    # Measured against the live engine while proving this route:
                    # a real batch named with ANOTHER session's id 404s here, and
                    # the message said "either it was never taken, or the session
                    # is user-visible". Both are wrong for that case, and the
                    # first sends the reader off to freeze a fresh export — which
                    # produces a second batch and the identical 404. A refusal
                    # that names the wrong remedy is worse than a terse one.
                    raise BatchNotVisibleError(
                        f"discovery-engine has no review-export batch {batch_id!r} on session "
                        f"{session_id!r} that is visible to tenant {tenant_id!r}. Three things "
                        "produce this: the batch belongs to a DIFFERENT session (check the pair, "
                        "a batch id alone is never enough), the batch was never taken, or the "
                        "session is visibility='user' and therefore unreachable to a service "
                        "acting for the tenant as a whole."
                    )
                if 400 <= response.status_code < 500:
                    await response.aread()
                    raise UpstreamRefusedError(
                        f"discovery-engine answered {response.status_code} for {url}. "
                        "This hop identifies itself with a bare X-Tenant-Id and relies on being "
                        "an in-cluster peer; from outside the trusted network it is refused."
                    )
                if 300 <= response.status_code < 400:
                    # `follow_redirects` is off — deliberately, because a
                    # redirect is a chance to be handed bytes from somewhere
                    # this code never named. But the branches below only
                    # recognised 4xx and 5xx, so a 3xx FELL THROUGH to the read:
                    # httpx gives an empty body for it, we would digest `b""`,
                    # and the caller would be told "the batch fetched does not
                    # hash to the expected_digest you declared" — an accusation
                    # aimed at the studio for something an ingress did.
                    await response.aread()
                    location = response.headers.get("Location")
                    raise UpstreamRefusedError(
                        f"discovery-engine answered {response.status_code} for {url} with "
                        f"Location={location!r}. Redirects are not followed: a frozen batch is "
                        "fetched from the address this service names or not at all. Nothing was "
                        "imported."
                    )
                if response.status_code >= 500:
                    await response.aread()
                    raise UpstreamUnavailableError(
                        f"discovery-engine answered {response.status_code} for {url}. Nothing was "
                        "imported; this is safe to retry."
                    )

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise DocumentTooLargeError(
                            f"the batch exceeds {MAX_DOCUMENT_BYTES} bytes and was abandoned at "
                            f"{size}. A corpus this large has to be split at the search, not "
                            "truncated here — a truncated import would seal a subset of the "
                            "session and report it as the whole one."
                        )
                    chunks.append(chunk)

                document = b"".join(chunks)
                asserted = response.headers.get("X-Document-Digest")
    except httpx.HTTPError as exc:
        # Connect refused, DNS, TLS, read timeout. Deliberately caught as one
        # class: the caller's remedy is identical for all of them and the
        # distinction belongs in the log, not in a problem detail.
        raise UpstreamUnavailableError(
            f"could not reach discovery-engine at {url}: {type(exc).__name__}: {exc}. "
            "Nothing was imported; this is safe to retry."
        ) from exc

    return FetchedBatch(
        document=document,
        computed_digest=digest_of(document),
        asserted_digest=asserted,
        url=url,
    )


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "BatchNotVisibleError",
    "DiscoveryFetchError",
    "DocumentTooLargeError",
    "FetchedBatch",
    "UpstreamRefusedError",
    "UpstreamUnavailableError",
    "digest_of",
    "fetch_review_export",
]
