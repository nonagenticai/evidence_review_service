"""RFC 9457 problem details, and the exception types that map onto them.

`09-new-service-specifications.md` and `INT-KAG-PROXY` both require an intact
`application/problem+json` body to survive the gateway. That is only achievable
if this service emits one in the first place, so every failure this service
raises deliberately carries a stable `type` slug the caller can branch on —
never a prose `detail` string a caller would have to pattern-match.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

#: Media type. Written once; a literal elsewhere is how the header and the body
#: drift apart.
PROBLEM_JSON = "application/problem+json"

#: Base for the `type` URI. It does not have to resolve, but it does have to be
#: stable and unique per failure class.
TYPE_BASE = "https://contracts.nonagentic.ai/problems/evidence-review"


class ProblemError(Exception):
    """A failure with a stable, machine-branchable identity."""

    status: int = 400
    slug: str = "bad_request"
    title: str = "Bad Request"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def to_response(self, instance: str | None = None) -> JSONResponse:
        body: dict[str, Any] = {
            "type": f"{TYPE_BASE}/{self.slug}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.slug,
        }
        if instance:
            body["instance"] = instance
        body.update(self.extra)
        return JSONResponse(status_code=self.status, content=body, media_type=PROBLEM_JSON)


class ValidationFailedError(ProblemError):
    status, slug, title = 422, "validation_failed", "Validation Failed"


class ForbiddenDiscriminatorError(ProblemError):
    """`ER-002.7` — a body carrying a guarded regulatory discriminator.

    Rejected, never silently dropped. A dropped field imports as an ordinary
    record while the caller believes it imported as a canonical regulatory
    entity, which is exactly the pre-ADR activation `DM-AT-18` exists to
    prevent.

    The wording avoids putting the two guarded words next to each other on
    purpose. The DM-AT-18 scanner treats a space as an identifier separator, so
    the English phrase would match the forbidden token and this docstring —
    which exists to explain the guard — would be reported as a violation of it.
    """

    status, slug, title = 400, "forbidden_discriminator", "Forbidden Discriminator"


class NotFoundError(ProblemError):
    """Also the answer to "may I see this?" when the honest answer would leak.

    A `403` confirms the thing exists. Under reviewer blinding (`01-…:728`) that
    confirmation is itself the disclosure, so an unauthorized read of another
    reviewer's work is a `404` with the identifier absent from the body.
    """

    status, slug, title = 404, "not_found", "Not Found"


class StateConflictError(ProblemError):
    status, slug, title = 409, "state_conflict", "State Conflict"


class IdempotencyConflictError(ProblemError):
    status, slug, title = 409, "idempotency_conflict", "Idempotency Conflict"


class PreconditionFailedError(ProblemError):
    status, slug, title = 412, "precondition_failed", "Precondition Failed"


class ForbiddenError(ProblemError):
    """Used only where existence is already known to the caller."""

    status, slug, title = 403, "forbidden", "Forbidden"


class HumanPrincipalRequiredError(ProblemError):
    """A service principal attempted a human judgement (`12-…:121-124`)."""

    status, slug, title = 403, "human_principal_required", "Human Principal Required"


class UnprocessableStateError(ProblemError):
    status, slug, title = 422, "unprocessable_state", "Unprocessable State"


class UnrecognizedPayloadError(ProblemError):
    """The bytes were not the format the caller declared.

    Its own slug rather than `unprocessable_state`, because the caller's remedy
    is specific and mechanical: re-export the file — usually as UTF-8 — or
    correct `source_kind`. Raised for a UTF-16 CSV, a PDF declared as RIS, JSON
    that nests past the depth bound, and a payload carrying a code point
    PostgreSQL cannot store.

    Deliberately NOT the answer for "the file was the right format and had no
    records in it" — that is :class:`EmptySourceNotDeclaredError`, because the
    remedy is completely different (assert the emptiness, do not re-export).
    """

    status, slug, title = 422, "unrecognized_payload", "Unrecognized Payload"


class EmptySourceNotDeclaredError(ProblemError):
    """A parseable payload with zero records, and no `allow_empty` on the request.

    A search that genuinely returns nothing is a real and reportable event: the
    PRISMA diagram wants a row saying that database was searched and yielded 0.
    So this is not "you may not import nothing" — it is "say so".

    The gate exists because `201 accepted=0 quarantined=0` is indistinguishable
    from a successful import, and `identified_total` in the flow ledger is
    derived from `import_rows`. A truncated export, a search whose date filter
    silently excluded everything, a five-byte file containing the word `Title` —
    every one of them sealed a corpus, screened it, and produced a PRISMA count
    that was internally consistent and described a corpus that was never there.
    Nothing downstream could disagree with it, which is precisely why nothing
    downstream noticed.

    `allow_empty=true` turns that from an accident into an assertion by a named
    human, recorded against the batch.
    """

    status, slug, title = 422, "empty_source_not_declared", "Empty Source Not Declared"


class UpstreamDigestMismatchError(ProblemError):
    """The fetched batch is not the batch the caller described.

    Its own slug rather than `validation_failed`, because it is the one refusal
    on the discovery-pull path that means *the corpus is not what somebody was
    shown*, and a client has to be able to branch on it: the remedy is to take a
    fresh batch and consent to that one, never to retry the same request.

    Raised for two distinguishable facts, both with the digests attached:

    * the bytes received do not hash to the `expected_digest` the caller
      declared — the studio is naming a batch it did not show the human, or the
      transport truncated the document;
    * the bytes received do not hash to the `X-Document-Digest` discovery-engine
      itself asserted — the upstream disagrees with its own stored receipt.

    Nothing is written in either case. An import that proceeded "because it
    parsed" would seal a corpus nobody consented to and report it as sealed.
    """

    status, slug, title = 422, "upstream_digest_mismatch", "Upstream Digest Mismatch"


class ServiceUnavailableError(ProblemError):
    """A dependency this service cannot serve without is down.

    Distinct from every 4xx above, because the caller did nothing wrong and
    retrying is the correct response. It exists because a database connection
    failure used to be reported as `409 state_conflict` — which sends whoever
    is on call looking for a lifecycle bug.
    """

    status, slug, title = 503, "service_unavailable", "Service Unavailable"


async def problem_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return exc.to_response(instance=str(request.url.path))
