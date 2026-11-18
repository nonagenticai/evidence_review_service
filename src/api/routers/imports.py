"""`/api/reviews/{id}/imports`, `/records`, `/corpus-versions` — `ER-002`.

Import accepts either a JSON envelope (`{"source_kind": ..., "payload": ...}`)
or a raw upload with the kind in the query string. Both paths funnel into the
same service call, so the denylist guard and the digest-based replay cannot be
reached by only one of them.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from actor import Actor
from api.body_guard import MAX_BODY_DEPTH, require_bounded_depth
from api.dependencies import ActorDep
from api.problem_details import (
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableStateError,
    UpstreamDigestMismatchError,
    ValidationFailedError,
)
from application.services import imports as svc
from application.services import reviews as review_svc
from config import get_settings
from database import actor_scope
from models.records import CorpusVersion, ImportBatch, ImportRow, ReviewRecordVersion

router = APIRouter(prefix="/api/reviews", tags=["Imports"])

#: Accepted by `POST /imports`, the route that takes bytes from the request body.
#:
#: `discovery_session` is DELIBERATELY absent, and its absence here is the whole
#: of the fix — see `VERIFIED_ONLY_SOURCE_KINDS`. This tuple is the generic
#: route's vocabulary, NOT the database's: `models.records.SOURCE_KINDS` still
#: carries `discovery_session` and must.
SOURCE_KINDS = ("ris", "bibtex", "csv", "canonical_json")

#: Source kinds that name an UPSTREAM, and may therefore only be created by the
#: route that goes and talks to it.
#:
#: `infrastructure/clients/discovery_engine.py` opens by explaining that the
#: corpus used to arrive as bytes the browser built, that the digest therefore
#: described a construction outside the trust boundary, and that the browser now
#: names a batch this service fetches. That was half true: `/imports/from-discovery`
#: was added ALONGSIDE this route, not in place of it, and
#: `POST /imports?source_kind=discovery_session` still took an arbitrary body,
#: never contacted discovery-engine, had no `expected_digest`, and wrote
#: `raw_payload_digest` over whatever arrived.
#:
#: Downstream the two were indistinguishable except by `source_ref.fetch_mode`,
#: which is null on the unverified path — and `identified_total`, the corpus
#: manifest and the PRISMA appendix all rest on that digest. So one `source_kind`
#: carried two meanings: "bytes discovery-engine froze, which we hashed
#: ourselves" and "somebody posted this".
#:
#: The refusal is at the application layer, on this route only. The database is
#: NOT tightened: `import_batches.source_kind = 'discovery_session'` is what the
#: verified route writes for every legitimate pull, and historical rows written
#: through the unverified path are real imports whose provenance an auditor
#: needs. A constraint, if ever wanted, would be on the PAIR — roughly
#: ``CHECK (source_kind <> 'discovery_session' OR source_ref ->> 'fetch_mode' =
#: 'discovery_engine_pull')`` — and it cannot be applied without first deciding
#: what to do with the existing rows, whose `fetch_mode` is null. That is its own
#: change, with its own migration.
VERIFIED_ONLY_SOURCE_KINDS = ("discovery_session",)

#: Formats that are RECOGNISED and deliberately not supported, as opposed to
#: unrecognised. `import_parser.unsupported_format_blocker` answers each with its
#: `AHTA-DISC-*` blocker and a sentence saying what is missing.
#:
#: They needed listing here because that function was DEAD CODE from every HTTP
#: entry point: `import_payload` is its only caller, the route's `kind not in
#: SOURCE_KINDS` check ran five lines before it on both entry paths, and none of
#: these strings is in `SOURCE_KINDS`. So a tester uploading a Catchii workbook
#: was told the valid enum and never why workbooks are blocked — and `AHTA-DISC`
#: had zero hits across the whole test suite, because nothing could reach it.
#:
#: The service-layer branch stays, and becomes genuine defence in depth rather
#: than the only, unreachable, implementation.
BLOCKED_SOURCE_KINDS = ("xlsx", "xls", "workbook", "catchii", "cachii")

#: Depth bound for the JSON *envelope*, which is `MAX_BODY_DEPTH` plus the one
#: level the envelope itself adds.
#:
#: The two import forms must accept exactly the same payloads. The raw form hands
#: the bytes to the parser, whose `_audit_json` measures the payload root as
#: depth 1; the envelope form wraps that same root in `{"source_kind": …,
#: "payload": <root>}`, so every node sits one level lower here than the number
#: the parser will later judge it by. Bounding the envelope at `MAX_BODY_DEPTH`
#: flat would therefore refuse, through one URL shape, a payload the other URL
#: shape accepts — a difference nobody would think to look for. The `+ 1` is that
#: wrapper and nothing else; `tests/api/test_request_body_bounds.py` pins the
#: boundary from both directions.
ENVELOPE_DEPTH_BOUND = MAX_BODY_DEPTH + 1


@router.post("/{review_id}/imports", status_code=status.HTTP_201_CREATED, operation_id="create_import")
async def create_import(
    actor: ActorDep,
    review_id: uuid.UUID,
    request: Request,
    source_kind: Annotated[str | None, Query()] = None,
    allow_empty: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Import one payload.

    `allow_empty` is the caller asserting that a search genuinely returned no
    records. Without it a payload that parses to zero records is refused
    (`422 empty_source_not_declared`) rather than written as a `201` with
    `accepted=0` — see :class:`api.problem_details.EmptySourceNotDeclaredError`.
    It may be given as a query parameter or as a key in the JSON envelope.

    ## Why this handler is `async` but does almost nothing

    FastAPI runs an `async def` handler ON the event loop and a plain `def`
    handler in a threadpool. This was `async def` with the whole import inside
    it: a JSON decode of up to 32 MB, the parser, the denylist walk and three
    synchronous SQLAlchemy INSERTs per record. None of that yields, so for the
    duration of one import — seconds, on a realistic 3,000-8,000 record first
    pass — the process served nothing else, `/health/live` included. A liveness
    probe that cannot be answered is a restart, so a large import could take the
    pod down mid-import. Every other route in this service is a plain `def`;
    this one was the outlier.

    It cannot simply BECOME a plain `def`. Starlette has no synchronous body
    reader — `Request.body()` is a coroutine — and the only sync-handler route to
    the raw bytes is a `bytes`-annotated `Body()` parameter, which does not
    behave the same: FastAPI's body resolver calls `await request.json()`
    whenever the content type is `application/json` and hands the handler a
    parsed **dict**. The digest that makes a retried import a replay rather than
    a second import is computed over the exact bytes the caller sent
    (`import_batches.raw_payload_digest`), and a dict re-encoded by this service
    is not those bytes. Changing that would silently break worker-death/retry
    safety, which is not a trade this fix is entitled to make.

    So the split is: `await request.body()` stays on the loop, because reading
    the body IS asynchronous and does not block it, and everything after it —
    which is all of the blocking work, unchanged — moves to the threadpool.
    """
    raw = await request.body()
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    return await run_in_threadpool(
        _import_payload_blocking,
        actor,
        review_id,
        raw,
        content_type,
        source_kind,
        allow_empty,
    )


def _import_payload_blocking(
    actor: Actor,
    review_id: uuid.UUID,
    body: bytes,
    content_type: str,
    source_kind: str | None,
    allow_empty: bool,
) -> dict[str, Any]:
    """Everything `create_import` used to do inline, verbatim, off the event loop.

    Split out rather than rewritten: this is the same code in the same order, so
    the change is "where it runs", not "what it does". The only edits are that
    `body` and `content_type` arrive as arguments instead of being awaited here.
    """
    if not body:
        raise ValidationFailedError("import body is empty")

    kind = source_kind
    payload = body
    empty_allowed = allow_empty

    if content_type == "application/json":
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValidationFailedError(f"body is not valid JSON: {exc}") from exc
        except RecursionError as exc:
            # A 40 KB body of nothing but `[` and `]` recurses the decoder past
            # the interpreter's limit. `RecursionError` is a `RuntimeError`, so
            # the `JSONDecodeError` arm above never saw it and it left as a
            # plain-text 500 with no problem+json body at all.
            raise ValidationFailedError(
                "body nests too deeply to parse. An import envelope is a flat object with a "
                "`source_kind` and a `payload` in it."
            ) from exc
        if isinstance(envelope, dict) and "source_kind" in envelope:
            # DEPTH FIRST, THEN THE DENYLIST. The order is the whole point and it
            # is load-bearing in a way the reviews and screening routers do not
            # have to think about, because they reach `guard_body`, which does
            # both. This is the one free-form body that guards itself.
            #
            # `find_forbidden_keys` is iterative, so it no longer BLOWS UP on a
            # deep body — it runs to completion, building an O(depth) path string
            # per node. That is O(nodes x depth), and it made an unbounded walk
            # an amplifier rather than a crash: measured, a 371 KiB envelope at
            # depth 40,000 with 150,000 leaves took 2.89s of wall time and 3.28 GB
            # of RSS to refuse, against a `limits.memory` of 512Mi. Removing a
            # `RecursionError` removed the thing that was accidentally
            # TERMINATING the walk, and the terminator has to be replaced with a
            # deliberate one rather than simply deleted.
            #
            # It is reachable before `actor_scope`, before `load_review`, before
            # `require_review_lead_for` and before the parser's size and depth
            # bounds, so a tenant header is the entire prerequisite — and since
            # this handler moved to the threadpool it is reachable concurrently.
            require_bounded_depth(envelope, "the import envelope", max_depth=ENVELOPE_DEPTH_BOUND)
            # Guarded BEFORE the inner payload is unwrapped, so a forbidden key
            # in the envelope is caught even if the inner payload is clean.
            svc.guard_denylist(envelope)
            kind = str(envelope["source_kind"])
            empty_allowed = empty_allowed or bool(envelope.get("allow_empty"))
            inner = envelope.get("payload")
            if inner is None:
                raise ValidationFailedError("envelope has no 'payload'")
            try:
                payload = inner.encode("utf-8") if isinstance(inner, str) else json.dumps(inner).encode("utf-8")
            except RecursionError as exc:
                # Re-encoding recurses too. A body can decode and then fail to
                # re-encode at a depth just under the decoder's limit.
                raise ValidationFailedError("the envelope's 'payload' nests too deeply to re-encode") from exc

    # `kind` is `str | None` — an absent `source_kind` on both entry paths. The
    # generic check below handles `None` correctly (it is not in `SOURCE_KINDS`),
    # so this one must not assume otherwise.
    if kind is not None and kind.lower() in BLOCKED_SOURCE_KINDS:
        # BEFORE the generic check, which would otherwise answer a recognised-but-
        # blocked format with the same message as a typo.
        from infrastructure.imports.import_parser import unsupported_format_blocker

        blocker = unsupported_format_blocker(kind)
        raise UnprocessableStateError(str(blocker), blocker=getattr(blocker, "blocker", None))
    if kind is not None and kind.lower() in VERIFIED_ONLY_SOURCE_KINDS:
        # BEFORE the generic check too, and for the same reason: `discovery_session`
        # is a real source kind this service really writes, so "must be one of ..."
        # would send the caller hunting for a typo they did not make. The mistake
        # is the ROUTE, and the message says which one to use instead.
        raise ValidationFailedError(
            f"source_kind {kind!r} cannot be imported from a request body. These bytes would be "
            "digested and sealed as a discovery-engine corpus without this service ever having "
            "asked discovery-engine for them: there is no expected_digest on this route and "
            "nothing to compare one against. Name the frozen batch instead — POST "
            f"/api/reviews/{{review_id}}/imports/from-discovery with {{session_id, batch_id, "
            "expected_digest}} — and this service will fetch it, hash what it receives and refuse "
            "unless the two agree."
        )
    if kind not in SOURCE_KINDS:
        raise ValidationFailedError(f"source_kind must be one of {', '.join(SOURCE_KINDS)}; got {kind!r}")

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.import_payload(
            session, actor, review_id, source_kind=kind, payload=payload, allow_empty=empty_allowed
        )


#: `sha256:` + 64 lowercase hex. The only digest spelling this service and
#: discovery-engine both write, so a bare hex or an uppercase one is a caller
#: mistake worth naming rather than a comparison that always fails.
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class FromDiscoveryBody(BaseModel):
    """Name a frozen batch and assert what it hashes to. No records travel here."""

    session_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    #: NOT a pydantic `pattern`, deliberately. A constraint failure inside the
    #: model is answered by FastAPI's own `RequestValidationError` shape —
    #: `{"detail": [...]}` — and this service's contract with the kag gateway
    #: (`INT-KAG-PROXY`) is that a failure carries an intact
    #: `application/problem+json` body with a stable `code`. The shape is checked
    #: in the handler instead, so a mis-spelled digest gets the same branchable
    #: refusal as every other thing that can go wrong on this route.
    expected_digest: str = Field(min_length=1)
    allow_empty: bool = False


@router.post(
    "/{review_id}/imports/from-discovery",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_import_from_discovery",
)
async def create_import_from_discovery(
    actor: ActorDep,
    review_id: uuid.UUID,
    body: FromDiscoveryBody,
) -> dict[str, Any]:
    """Import a discovery-engine review-export batch this service fetches itself.

    ## Why a second import route rather than another `source_kind`

    `POST /imports` has one contract: *these bytes are the corpus, and I digest
    exactly what you sent me*. That is what makes a retried import a replay
    rather than a second import, and it is why the handler goes to such lengths
    to keep the caller's bytes unparsed.

    This route's contract is the opposite one — *I fetch the bytes, and you tell
    me what they should hash to* — and it has refusals the other cannot have:
    an unreachable upstream, a batch not visible to the tenant, a digest that
    disagrees with the caller's, and a digest that disagrees with the upstream's
    own. Folding those into `source_kind=discovery_session` would have meant one
    route where `payload` is sometimes required and sometimes forbidden, and a
    caller could not tell from a `422` which contract it had broken.

    Both paths converge on the same `import_payload`, so the lead gate, the
    denylist, the empty-source refusal and the replay-by-digest behaviour are
    shared and cannot drift.

    ## What ends up in `source_ref`

    The session, the batch, the URL fetched, and the digest computed **here**
    over the bytes as received. `raw_payload_digest` on the batch comes out
    equal to discovery-engine's own `digest` for that batch — same formula, same
    bytes — so anyone holding both numbers can check the chain without asking
    either service to vouch for itself.

    Keys are deliberately not `session_id`: `parse_discovery_session_export`
    harvests that name out of the document, and the service layer merges the
    harvested provenance OVER the declared one. Two different facts (what the
    caller asked for, what the document says it is) must not collapse into one
    key — the case where they disagree is refused below, and it could not be
    refused if one silently overwrote the other.
    """
    from infrastructure.clients import discovery_engine as de

    if not _DIGEST_PATTERN.match(body.expected_digest):
        raise ValidationFailedError(
            f"expected_digest must be 'sha256:' followed by 64 lowercase hex characters; got "
            f"{body.expected_digest!r}. That is the spelling both this service and discovery-engine "
            "write, so a bare hex digest would never match anything and the refusal would read as "
            "tampering."
        )

    # AUTHORITY BEFORE THE OUTBOUND CALL, and this ordering is the fix, not a
    # tidy-up.
    #
    # The lead gate lives inside `import_payload`, which runs AFTER the fetch.
    # So every reviewer in the tenant — anyone `load_review` lets through, which
    # is everyone — could make this service issue a request to discovery-engine
    # for any (session, batch) pair they cared to name, and read the answer off
    # the status code: `404` means no such batch on that session, `422
    # upstream_digest_mismatch` means it exists and hashes to something else,
    # `201` means it exists and they guessed the digest. That is an oracle over
    # other people's frozen corpora and a request amplifier, and both are
    # reachable by a caller with no authority over the review at all.
    #
    # `import_payload` still holds its own gate — it is a service-level check
    # and must not become a router-level one (`require_review_lead_for`'s own
    # docstring is about exactly that mistake). This is the SECOND holder of the
    # same gate, placed where it can stop the side effect rather than the write.
    #
    # In the THREADPOOL, like every other database touch on this route:
    # `actor_scope` is synchronous SQLAlchemy, and running it inline in an
    # `async def` handler is precisely the event-loop block `create_import`'s
    # docstring exists to explain.
    await run_in_threadpool(_require_import_authority_blocking, actor, review_id)

    settings = get_settings()
    try:
        fetched = await de.fetch_review_export(
            base_url=settings.discovery_engine_url,
            # `X-Tenant-Id` on the wire, so a `str` — httpx refuses a non-string
            # header value outright, and `Actor.tenant_id` is a `uuid.UUID`
            # since `0014`.
            tenant_id=str(actor.tenant_id),
            session_id=body.session_id,
            batch_id=body.batch_id,
            timeout_seconds=settings.http_timeout_seconds,
            # This SERVICE's licence, never the actor's credential: the tenant
            # above is who we act for, this is who is acting.
            license_token=settings.discovery_engine_license_token or None,
        )
    except de.UpstreamUnavailableError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    except de.BatchNotVisibleError as exc:
        raise NotFoundError(str(exc)) from exc
    except de.UpstreamRefusedError as exc:
        raise UnprocessableStateError(str(exc)) from exc
    except de.DocumentTooLargeError as exc:
        raise ValidationFailedError(str(exc)) from exc

    # UPSTREAM SELF-CONSISTENCY FIRST. If the transport truncated the document,
    # it disagrees with BOTH digests, and "the bytes are not the bytes
    # discovery-engine stored" is the more precise diagnosis — the caller's
    # `expected_digest` was right and would send whoever reads the error looking
    # at the studio instead of the network.
    if fetched.asserted_digest is not None and fetched.asserted_digest != fetched.computed_digest:
        raise UpstreamDigestMismatchError(
            "discovery-engine's X-Document-Digest does not match the bytes it sent. Nothing was "
            "imported. This is a truncated or altered response, not a client mistake.",
            asserted_digest=fetched.asserted_digest,
            computed_digest=fetched.computed_digest,
            fetched_from=fetched.url,
        )
    if fetched.computed_digest != body.expected_digest:
        raise UpstreamDigestMismatchError(
            "the batch fetched does not hash to the expected_digest you declared. Nothing was "
            "imported: the batch a human consented to and the batch that would have been sealed "
            "are different documents. Take a fresh review-export and import that receipt.",
            expected_digest=body.expected_digest,
            computed_digest=fetched.computed_digest,
            fetched_from=fetched.url,
        )

    declared_source_ref = {
        "fetch_mode": "discovery_engine_pull",
        "discovery_session_id": body.session_id,
        "review_export_batch_id": body.batch_id,
        "fetched_from": fetched.url,
        "fetched_document_digest": fetched.computed_digest,
    }

    return await run_in_threadpool(
        _import_fetched_blocking,
        actor,
        review_id,
        fetched.document,
        body,
        declared_source_ref,
    )


def _require_import_authority_blocking(actor: Actor, review_id: uuid.UUID) -> None:
    """The lead gate, held before anything leaves this pod.

    Deliberately returns nothing. It is not a lookup whose result the caller
    uses — it is a refusal, and giving it a return value would invite somebody
    to treat "we already loaded the review" as a reason to skip the gate that
    `import_payload` holds for itself.
    """
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        review_svc.require_review_lead_for(session, actor, review_id, "importing records")


def _import_fetched_blocking(
    actor: Actor,
    review_id: uuid.UUID,
    document: bytes,
    body: FromDiscoveryBody,
    declared_source_ref: dict[str, Any],
) -> dict[str, Any]:
    """The blocking half — the same threadpool discipline `create_import` documents.

    The document's own `session_id` is checked against the one requested before
    anything is written. discovery-engine scopes a batch to its session, so a
    disagreement should be unreachable; it is checked anyway because the
    consequence of being wrong is a corpus whose provenance names a session it
    did not come from, and nothing downstream would ever disagree with it.
    """
    try:
        declared = json.loads(document) if document else {}
    except json.JSONDecodeError, RecursionError:
        # Not this route's refusal to make. `parse_discovery_session_export`
        # answers a malformed body with a message naming the format, and a
        # second, different message from here would be the one the caller saw.
        declared = None
    if isinstance(declared, dict):
        document_session = declared.get("session_id")
        if document_session is not None and str(document_session) != body.session_id:
            raise UnprocessableStateError(
                f"the fetched batch declares session_id {document_session!r} but was requested as "
                f"{body.session_id!r}. Nothing was imported.",
            )

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.import_payload(
            session,
            actor,
            review_id,
            source_kind="discovery_session",
            payload=document,
            declared_source_ref=declared_source_ref,
            allow_empty=body.allow_empty,
        )


@router.get("/{review_id}/imports", operation_id="list_imports")
def list_imports(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(ImportBatch).where(ImportBatch.review_id == review_id).order_by(ImportBatch.created_at)
            )
            .scalars()
            .all()
        )
        return {
            "items": [svc._batch_summary(session, b, replayed=False) for b in rows],
            "count": len(rows),
        }


@router.get("/{review_id}/records", operation_id="list_records")
def list_records(
    actor: ActorDep,
    review_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Current version of every record in the review.

    "Current" is the tail of each record's version chain. Superseded versions
    remain queryable through `GET /records/{review_record_id}`; they are not
    shown here
    because a list that mixed versions of the same record would double-count in
    a way the flow ledger does not.
    """
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        current = svc.current_record_version_ids(session, review_id)
        total = len(current)
        page = current[offset : offset + limit]
        rows = (
            session.execute(
                select(ReviewRecordVersion)
                .where(ReviewRecordVersion.review_record_version_id.in_(page))
                .order_by(ReviewRecordVersion.created_at)
            )
            .scalars()
            .all()
            if page
            else []
        )
        return {
            "items": [_record_summary(v) for v in rows],
            "count": len(rows),
            "total": total,
            "offset": offset,
        }


@router.get("/{review_id}/records/{review_record_id}", operation_id="get_record")
def get_record(actor: ActorDep, review_id: uuid.UUID, review_record_id: uuid.UUID) -> dict[str, Any]:
    """One record and its FULL version history, oldest first.

    `list_records` returns only the tail of each version chain, and its
    docstring already told readers that superseded versions "remain queryable
    through the record detail route" — a route that did not exist. Two things
    depended on it and could not get there:

    * a lead adjudicating a conflict had only the title, because
      `GET /conflicts` carries no abstract, DOI or journal;
    * the pre-merge state of a deduplicated record was visible nowhere, so the
      reversibility trail was half-readable: the resolution CHAIN could be read
      but not the record versions those resolutions point at.

    ## "FULL" became true in `0007`

    Until then it was aspirational and the second bullet was unreachable:
    `imports._record_version` was the only writer of `version_number` and wrote a
    literal `1`, `supersedes_version_id` was assigned nowhere, and a merge with
    survivorship left the survivor byte-identical. So `version_count` was ALWAYS
    exactly 1 and there was no pre-merge state to read.

    `apply_survivorship` is what produces a second version, and it is currently
    the only thing that does. A record with `version_count == 1` therefore means
    "never survived a merge", not "history unavailable".
    """
    from api.problem_details import NotFoundError
    from models.records import ReviewRecord

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        record = session.get(ReviewRecord, review_record_id)
        if record is None or record.review_id != review_id:
            raise NotFoundError("no such record for this review")
        versions = (
            session.execute(
                select(ReviewRecordVersion)
                .where(ReviewRecordVersion.review_record_id == review_record_id)
                .order_by(ReviewRecordVersion.version_number)
            )
            .scalars()
            .all()
        )
        return {
            "review_record_id": str(review_record_id),
            "review_id": str(review_id),
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "current_version_number": versions[-1].version_number if versions else None,
            "versions": [
                {
                    **_record_summary(v),
                    "supersedes_version_id": (str(v.supersedes_version_id) if v.supersedes_version_id else None),
                    "field_provenance": v.field_provenance,
                    # The dedup baseline for THIS version. On the detail route
                    # only: it is what explains why a record clustered, and after
                    # a survivorship merge it is the observable proving the
                    # normalized values were recomputed from the surviving
                    # fields rather than copied off the source version.
                    "normalized_title": v.normalized_title,
                    "normalized_doi": v.normalized_doi,
                    "normalized_pmid": v.normalized_pmid,
                    "source_import_row_id": (str(v.source_import_row_id) if v.source_import_row_id else None),
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ],
            "version_count": len(versions),
        }


@router.get("/{review_id}/quarantine", operation_id="list_quarantined_rows")
def list_quarantine(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    """Quarantined rows with their original bytes and the reason.

    `ER-009.3` — an omission must be explicit. A quarantine count with no way to
    see what was quarantined presents a filtered corpus as a complete one.
    """
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(ImportRow)
                .where(ImportRow.review_id == review_id, ImportRow.status == "quarantined")
                .order_by(ImportRow.created_at)
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "import_row_id": str(r.import_row_id),
                    "import_batch_id": str(r.import_batch_id),
                    "row_index": r.row_index,
                    "quarantine_reason": r.quarantine_reason,
                    "raw_representation": r.raw_representation,
                }
                for r in rows
            ],
            "count": len(rows),
        }


@router.post("/{review_id}/corpus-versions", status_code=status.HTTP_201_CREATED, operation_id="seal_corpus_version")
def seal_corpus(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        return svc.seal_corpus_version(session, actor, review_id)


@router.get("/{review_id}/corpus-versions", operation_id="list_corpus_versions")
def list_corpus_versions(actor: ActorDep, review_id: uuid.UUID) -> dict[str, Any]:
    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        rows = (
            session.execute(
                select(CorpusVersion).where(CorpusVersion.review_id == review_id).order_by(CorpusVersion.version_number)
            )
            .scalars()
            .all()
        )
        return {"items": [svc.corpus_summary(c) for c in rows], "count": len(rows)}


@router.get("/{review_id}/corpus-versions/{corpus_version_id}/members", operation_id="list_corpus_members")
def list_corpus_members(actor: ActorDep, review_id: uuid.UUID, corpus_version_id: uuid.UUID) -> dict[str, Any]:
    """The sealed membership, in its canonical order.

    Returned with `recomputed_manifest_hash` so a caller can verify the seal
    against the members it was actually given, rather than trusting the stored
    value it is also being handed.
    """
    from api.problem_details import NotFoundError
    from domain.corpus import member_manifest_hash
    from models.records import CorpusVersionMember

    with actor_scope(actor) as session:
        review_svc.load_review(session, review_id)
        corpus = session.get(CorpusVersion, corpus_version_id)
        if corpus is None or corpus.review_id != review_id:
            raise NotFoundError("no such corpus version for this review")
        members = (
            session.execute(
                select(CorpusVersionMember)
                .where(CorpusVersionMember.corpus_version_id == corpus_version_id)
                .order_by(CorpusVersionMember.member_ordinal)
            )
            .scalars()
            .all()
        )
        return {
            "corpus_version_id": str(corpus_version_id),
            "member_ordering_rule": corpus.member_ordering_rule,
            "member_manifest_hash": corpus.member_manifest_hash,
            "recomputed_manifest_hash": member_manifest_hash([m.review_record_version_id for m in members]),
            "items": [
                {
                    "member_ordinal": m.member_ordinal,
                    "review_record_id": str(m.review_record_id),
                    "review_record_version_id": str(m.review_record_version_id),
                }
                for m in members
            ],
            "count": len(members),
        }


def _record_summary(v: ReviewRecordVersion) -> dict[str, Any]:
    return {
        "review_record_id": str(v.review_record_id),
        "review_record_version_id": str(v.review_record_version_id),
        "version_number": v.version_number,
        "record_type": v.record_type,
        "title": v.title,
        "abstract": v.abstract,
        "publication_year": v.publication_year,
        "authors": v.authors,
        "container_title": v.container_title,
        "doi": v.doi,
        "pmid": v.pmid,
        "pmcid": v.pmcid,
        "arxiv_id": v.arxiv_id,
        "url": v.url,
        "content_hash": v.content_hash,
        "schema_ref": v.schema_ref,
    }


__all__ = ["router", "func"]
