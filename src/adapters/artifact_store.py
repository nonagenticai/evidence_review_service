"""The `ArtifactStore` port — `FND-009`, `13-…:§11.1`.

## What this is, and what it is not

`13-…:725-747` gives the domain adapter exactly five operations and a strict
chain: `domain → Artifact Service → Storage Service → MinIO`, with Storage the
ONLY holder of MinIO credentials. "The domain adapter calls Artifact Service
only" (`13-…:739`) is the whole contract this module exists to keep — so this
file imports `httpx` and nothing else network-shaped. It never imports a MinIO
SDK, never imports `storage_service`'s client, and never reads a MinIO or
Storage Service endpoint from configuration. `tests/unit/test_the_artifact_store_
never_touches_storage_or_minio.py` parses this module's own AST and asserts
that, rather than trusting a promise in prose.

This module is the PORT, not the wiring. Nothing in
`application/services/snapshots.py` calls it yet — that file's own docstring
says why it still executes inline: "the artifact store is `FND-009`, which does
not exist." Landing this file makes the first half of that sentence false and
deliberately leaves the second half (`application/services/`, out of this
change's scope) for whoever does the wiring next.

## `artifact_service` is deployed, and it is not what `13-…:§11.1` describes yet

`artifact_service/src/routes/artifacts.py` and
`artifact_service/src/services/storage_service.py:26-31` already implement the
`Artifact → Storage → MinIO` half of the chain, with Storage as the sole
credential holder — that part of `13-…:739-741` is real and running. But the
FIVE operations `13-…:732-736` specifies are not five endpoints over there.
Measured against the live route file:

* upload is single-shot (`routes/artifacts.py:67`) — bytes in, artifact record
  out, in one call. There is no endpoint that accepts bytes and hands back a
  pending reference for a later, separate promotion.
* `routes/artifacts.py:95` states outright: "No presigned URLs are used."
  (repeated at `:435`). A repo-wide search of `storage_service/src` for
  `presign` returns zero hits.
* crypto-shredding does not exist in either repository, under any name. The
  vocabulary a caller would need to request one — the disposition matrix at
  `13-…:763-777` — is seven bullet points with no cells filled in; filling them
  is `IMP-DEC-005`, which is still open.

`13-…:743-744` calls presigning and crypto-shredding "net-new
security-qualified Artifact Service capabilities, not assumed current
behavior." That sentence is the specification for what this module does with
`stage`, `presign` and `delete_or_crypto_shred`: each raises its own named
error, always, citing exactly the gap above. None of the three returns `None`
and none silently no-ops — a caller cannot mistake "this was not built" for "this
ran and did nothing."

`commit` and `open` ARE wired against the real, deployed `/upload` and
`/download` routes. Both are honest about what they cannot do, in their own
docstrings below, rather than pretending the gap does not exist.

## Why `commit` takes a `StagedRef` when `stage` never produces one

`13-…:732`'s two-phase shape is: `stage` puts bytes somewhere pending, and
`commit` promotes a `staged_ref` by NAME, without re-sending them. That shape
needs a server that can hold a pending upload, and Artifact Service cannot —
its only relevant endpoint is `/upload`, and it is atomic: stage and promote
happen in the one HTTP call. So `StagedRef` here holds the bytes **locally, in
this process** — nothing is sent anywhere by constructing one. `stage()` cannot
produce a `StagedRef` (it always raises, see its docstring), so the only way to
get one is to construct it directly. That is not a hidden back door around
`stage()`'s refusal: `stage()` is refusing to talk to a staging endpoint that
does not exist, and a locally-held `StagedRef` never talks to one either. When
`commit()` runs, it performs the entire remote operation — staging and
promotion collapsed into the one `/upload` POST Artifact Service actually has.

## The integrity check `13-…:1020` wants evidence of

`routes/artifacts.py:169` computes a `sha256` over the uploaded bytes but never
compares it against anything the caller supplied — there is no
`expected_sha256` parameter on `/upload` at all. `commit()` is where that
comparison happens, and it happens over the LOCALLY held bytes, before any HTTP
call: a mismatch never reaches Artifact Service, so "the artifact is not
recorded" on a mismatch is a property of the code path, not a best-effort
cleanup after the fact.

## The rights re-check `open()` cannot perform

`13-…:759-760` requires a rights re-check before display, model/provider use,
translation, quotation, export, redistribution and publication. That check
needs a rights authority to ask, and none exists: Ingestion rights is backlog
item 14, and `13-…:1020` names it as a real dependency of `FND-009`, not a
future nicety. `open()` therefore accepts `current_rights_context` — dropping
the parameter would hide the gap rather than name it — carries it through
unread, and returns it unchanged on `OpenedArtifact`. **No action `open()`
gates is actually checked against it.** Every one of display, model/provider
use, translation, quotation, export, redistribution and publication is
UNGUARDED at this layer today. A caller relying on `open()` to enforce rights
is relying on something this function does not do.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from config import get_settings


#: Read once, at import time, purely as a convenience default for callers that
#: do not otherwise have a base URL handy (a script, a REPL, a unit test).
#: Resolved from `Settings` PER CALL, never captured at import.
#:
#: This was a module-level `os.getenv(...)` used as a default ARGUMENT, which is
#: worse than it looks: a default argument is evaluated once when the function is
#: defined, so the value froze at import time and no amount of setting the
#: environment afterwards could change it — including in a test. Reading it through
#: `get_settings()` puts it on the same footing as every other upstream in this
#: service and keeps the manifest the single place the URL is declared.
def _default_base_url() -> str:
    return get_settings().artifact_service_url


DEFAULT_TIMEOUT_SECONDS = 30.0


# ------------------------------------------------------------------------------
# Errors — one distinct, named class per refusal. None of them is a bare
# `Exception` or a `ValueError`: a caller has to be able to `except` the
# specific gap without also swallowing an unrelated bug.
# ------------------------------------------------------------------------------


class ArtifactStoreError(Exception):
    """Base for every failure this adapter raises. Never returns `None`."""


class StagingNotAvailableError(ArtifactStoreError):
    """`stage()` was called. Artifact Service has no staging endpoint to call.

    See the module docstring's "`artifact_service` is deployed, and it is not
    what `13-…:§11.1` describes yet" section — `routes/artifacts.py:67`'s
    `/upload` is single-shot, so there is no `staged_ref` this function could
    honestly return.
    """


class PresignNotAvailableError(ArtifactStoreError):
    """`presign()` was called. Artifact Service explicitly does not presign.

    `routes/artifacts.py:95`: "No presigned URLs are used." — repeated at
    `:435`. `13-…:743` calls presigning "net-new security-qualified," not
    current behavior.
    """


class CryptoShredNotAvailableError(ArtifactStoreError):
    """`delete_or_crypto_shred()` was called. Crypto-shredding exists nowhere.

    Not in `artifact_service`, not in `storage_service`. The lawful-disposition
    vocabulary a caller would name is `IMP-DEC-005`'s open decision — the
    disposition matrix at `13-…:763-777` is seven bullets with no cells filled
    in yet.
    """


class ArtifactIntegrityError(ArtifactStoreError):
    """`commit()`'s `expected_sha256` did not match the staged bytes.

    Raised before any HTTP call — see `commit()`'s docstring. The artifact is
    not recorded on Artifact Service because it was never sent there.
    """


class ArtifactUpstreamError(ArtifactStoreError):
    """Artifact Service could not be reached, or answered an unexpected status."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Artifact Service answered 404 for a committed `ArtifactRef`."""


# ------------------------------------------------------------------------------
# Refs — typed, frozen, and named for what they actually are (see module
# docstring for why `StagedRef` is local-only).
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceContext:
    """Where in Artifact Service's tenancy an artifact belongs, and what kind it is.

    Field names mirror the form fields `POST /projects/{project_id}/artifacts/
    upload` actually requires (`artifact_service/src/routes/artifacts.py:67`) —
    this port does not invent an upload shape the remote service does not have.
    """

    project_id: uuid.UUID
    filename: str
    kind: str
    card_id: uuid.UUID | None = None
    step: str | None = None
    container_name: str | None = None
    sensitivity: str = "internal"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagedRef:
    """Bytes held in THIS process, pending `commit()`. Never sent by construction.

    See the module docstring's "Why `commit` takes a `StagedRef` when `stage`
    never produces one" for why this is local rather than a name for something
    sitting on Artifact Service.
    """

    data: bytes
    expected_media_type: str
    resource_context: ResourceContext


@dataclass(frozen=True)
class ArtifactRef:
    """What Artifact Service calls a committed artifact — from its `UploadResponse`
    (`artifact_service/src/models/schemas.py:264-276`)."""

    artifact_id: uuid.UUID
    version: int
    project_id: uuid.UUID
    sha256: str
    storage_key: str
    size: int


@dataclass(frozen=True)
class RightsContext:
    """What the caller currently believes about rights over this artifact's
    underlying content — the shape `13-…:751-757` says a consumer stores:
    source/document/version identity, the rights decision that was evaluated
    and for what purpose, its version and evaluation time, and any expiry or
    territory constraint.

    Carried through `open()` UNCHECKED. See `open()`'s docstring.
    """

    source_document_id: str | None
    rights_decision_id: str | None
    evaluated_purpose: str
    decision_version: str | None = None
    evaluated_at: datetime | None = None
    expiry: datetime | None = None
    territory_constraint: str | None = None


@dataclass(frozen=True)
class OpenedArtifact:
    """Bytes retrieved through `open()`, plus what Artifact Service reported
    about them, plus the `RightsContext` the caller supplied — unread, unchanged."""

    artifact_id: uuid.UUID
    content: bytes
    content_type: str
    sha256: str
    filename: str
    purpose: str
    rights_context: RightsContext


# ------------------------------------------------------------------------------
# stage — NOT AVAILABLE.
# ------------------------------------------------------------------------------


def stage(
    data: bytes,
    expected_media_type: str,
    resource_context: ResourceContext,
) -> StagedRef:
    """Always raises `StagingNotAvailableError`. See the module docstring.

    `data`, `expected_media_type` and `resource_context` are accepted — not
    dropped — so a future implementation can be slotted in against this exact
    signature (`13-…:732`) the day Artifact Service ships a real staging
    endpoint. Until then, this function refuses rather than approximating one.
    """
    raise StagingNotAvailableError(
        "Artifact Service exposes only a single-shot POST "
        "/projects/{project_id}/artifacts/upload (artifact_service/src/routes/"
        "artifacts.py:67); there is no endpoint that accepts bytes and returns a "
        "pending reference to promote later, so a real two-phase stage cannot be "
        "performed against it. This is not a smaller version of staging — it is "
        "a capability that does not exist yet. commit() can still be called with "
        "a StagedRef constructed directly (the bytes never leave this process "
        "until commit() sends them); see commit()'s docstring for what that "
        "does and does not buy you."
    )


# ------------------------------------------------------------------------------
# commit — wired against the real /upload route.
# ------------------------------------------------------------------------------


def commit(
    staged_ref: StagedRef,
    expected_sha256: str,
    immutable_metadata: dict[str, Any] | None = None,
    *,
    base_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> ArtifactRef:
    """Upload `staged_ref`'s bytes, but only if they hash to `expected_sha256`.

    `routes/artifacts.py:169` computes a `sha256` server-side and never
    compares it to anything the caller sent — there is no `expected_sha256`
    parameter on `/upload`. This function is where that comparison happens, and
    it happens over the bytes THIS PROCESS ALREADY HOLDS, before any network
    call: a mismatch raises `ArtifactIntegrityError` and nothing is sent to
    Artifact Service. "The artifact is not recorded" is therefore exact, not a
    best-effort cleanup performed after an upload that already landed.

    `immutable_metadata` is accepted per `13-…:732`'s signature and is
    deliberately NOT transmitted to Artifact Service — `13-…:727` draws the
    line at "the domain service owns semantic role, producing aggregate/run,
    rights snapshot and provenance metadata" and Artifact Service's schema has
    no column for any of it. `13-…:746-747`: "The domain service stores the
    returned immutable artifact ID/version/hash and remains authoritative for
    what that artifact means." Recording `immutable_metadata` alongside the
    returned `ArtifactRef` is the CALLER's job, not this function's.

    `client`, when given, is used instead of a private `httpx.Client` — the
    hook the unit tests use to swap in an `httpx.MockTransport` rather than
    reaching the network.
    """
    computed_sha256 = hashlib.sha256(staged_ref.data).hexdigest()
    if computed_sha256 != expected_sha256.strip().lower():
        raise ArtifactIntegrityError(
            f"expected_sha256={expected_sha256!r} does not match the staged bytes "
            f"(computed sha256={computed_sha256!r}). Nothing was sent to Artifact "
            "Service: the mismatch is caught over the locally-held bytes before "
            "any HTTP call, so no artifact was recorded for this content."
        )

    ctx = staged_ref.resource_context
    base_url = base_url or _default_base_url()
    url = f"{base_url.rstrip('/')}/projects/{ctx.project_id}/artifacts/upload"
    files = {"file": (ctx.filename, io.BytesIO(staged_ref.data), staged_ref.expected_media_type)}
    form: dict[str, str] = {
        "filename": ctx.filename,
        "content_type": staged_ref.expected_media_type,
        "kind": ctx.kind,
        "sensitivity": ctx.sensitivity,
        "tags": ",".join(ctx.tags),
    }
    if ctx.card_id is not None:
        form["card_id"] = str(ctx.card_id)
    if ctx.step is not None:
        form["step"] = ctx.step
    if ctx.container_name is not None:
        form["container_name"] = ctx.container_name

    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=timeout_seconds)
    try:
        try:
            response = http_client.post(url, data=form, files=files)
        except httpx.HTTPError as exc:
            raise ArtifactUpstreamError(
                f"could not reach Artifact Service at {url}: {type(exc).__name__}: {exc}. Nothing was recorded."
            ) from exc

        if response.status_code >= 400:
            raise ArtifactUpstreamError(
                f"Artifact Service answered {response.status_code} for POST {url}: "
                f"{response.text[:500]!r}. Nothing was recorded."
            )

        body = response.json()
        return ArtifactRef(
            artifact_id=uuid.UUID(str(body["artifact_id"])),
            version=int(body["version"]),
            project_id=ctx.project_id,
            sha256=str(body["sha256"]),
            storage_key=str(body["storage_key"]),
            size=int(body["size"]),
        )
    finally:
        if owns_client:
            http_client.close()


# ------------------------------------------------------------------------------
# open — wired against the real /download route. Rights re-check NOT enforced.
# ------------------------------------------------------------------------------


def open(
    artifact_ref: ArtifactRef,
    purpose: str,
    current_rights_context: RightsContext,
    *,
    base_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> OpenedArtifact:
    """Fetch the committed bytes named by `artifact_ref`, for `purpose`.

    ⚠️ **The rights re-check `13-…:759-760` requires is NOT performed here.**
    That section requires checking current rights before display,
    model/provider use, translation, quotation, export, redistribution and
    publication. Every one of those seven actions is UNGUARDED at this layer:
    `current_rights_context` is accepted — dropping it would hide the gap
    rather than name it — carried through, and returned unchanged on
    `OpenedArtifact.rights_context`, but nothing in this function reads it to
    permit or refuse anything. There is no rights authority to ask: Ingestion
    rights is backlog item 14, and `13-…:1020` names it as a real dependency of
    `FND-009`, not a future nicety. A caller relying on `open()` to enforce
    rights is relying on something this function does not do; the check has to
    happen in the caller, against a real rights authority, once one exists.

    Raises `ArtifactNotFoundError` on a 404 and `ArtifactUpstreamError` on any
    other failure to reach or read from Artifact Service.
    """
    base_url = base_url or _default_base_url()
    url = f"{base_url.rstrip('/')}/projects/{artifact_ref.project_id}/artifacts/{artifact_ref.artifact_id}/download"

    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=timeout_seconds)
    try:
        try:
            response = http_client.get(url)
        except httpx.HTTPError as exc:
            raise ArtifactUpstreamError(
                f"could not reach Artifact Service at {url}: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise ArtifactNotFoundError(f"Artifact Service has no artifact {artifact_ref.artifact_id} at {url}")
        if response.status_code >= 400:
            raise ArtifactUpstreamError(
                f"Artifact Service answered {response.status_code} for GET {url}: {response.text[:500]!r}"
            )

        # ⚠️ HASH THE BYTES. The first version of this function read the digest
        # from `x-artifact-sha256` and DEFAULTED it to `artifact_ref.sha256`,
        # which made every integrity check a tautology: with the header absent —
        # and `artifact_service`'s `/download` is not known to send it — the
        # `sha256` returned was, by construction, the value the caller was about
        # to compare it against. A server returning `b"TAMPERED CONTENT"` was
        # indistinguishable from one returning the committed bytes, and no caller
        # could ever have detected the difference. An immutable artifact store
        # whose only integrity signal is `x == x` has no integrity signal.
        #
        # The bytes are the evidence. `artifact_ref.sha256` is what `commit()`
        # recorded for them, so a disagreement is a real defect — corruption in
        # transit, a mis-keyed fetch, or substitution — and this fails CLOSED on
        # it rather than handing back content it cannot vouch for.
        computed_sha256 = hashlib.sha256(response.content).hexdigest()
        expected_sha256 = artifact_ref.sha256.strip().lower()
        if computed_sha256 != expected_sha256:
            raise ArtifactIntegrityError(
                f"artifact {artifact_ref.artifact_id} downloaded from {url} does not match its "
                f"committed digest: ArtifactRef.sha256={expected_sha256!r}, sha256 of the "
                f"{len(response.content)} bytes received={computed_sha256!r}. "
                f"The bytes were NOT returned to the caller."
            )

        # A header that disagrees with the bytes it accompanies is also a defect,
        # even though the bytes just passed: Artifact Service and its own payload
        # contradict each other, and silently preferring either one hides that.
        advertised = response.headers.get("x-artifact-sha256")
        if advertised is not None and advertised.strip().lower() != computed_sha256:
            raise ArtifactIntegrityError(
                f"Artifact Service advertised x-artifact-sha256={advertised!r} for artifact "
                f"{artifact_ref.artifact_id} at {url} but the bytes it sent hash to "
                f"{computed_sha256!r}. The response contradicts itself; nothing was returned."
            )

        return OpenedArtifact(
            artifact_id=artifact_ref.artifact_id,
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            sha256=computed_sha256,
            filename=_filename_from_content_disposition(response.headers.get("content-disposition")),
            purpose=purpose,
            rights_context=current_rights_context,
        )
    finally:
        if owns_client:
            http_client.close()


def _filename_from_content_disposition(header: str | None) -> str:
    """`attachment; filename="x.pdf"` -> `x.pdf`. Empty string if absent or unparsable."""
    if not header:
        return ""
    marker = "filename="
    idx = header.find(marker)
    if idx == -1:
        return ""
    value = header[idx + len(marker) :].strip()
    # Stop at the end of THIS parameter. `attachment; filename="a.pdf";
    # creation-date="Mon, 1 Jan 2026 00:00:00 GMT"` used to return
    # `a.pdf"; creation-date="Mon`, because the old code stripped quotes from
    # both ends of everything following `filename=` — so a quoted value
    # containing `;` swallowed every parameter after it.
    if value.startswith('"'):
        closing = value.find('"', 1)
        return value[1:closing] if closing != -1 else value[1:]
    return value.split(";", 1)[0].strip()


# ------------------------------------------------------------------------------
# presign — NOT AVAILABLE.
# ------------------------------------------------------------------------------


def presign(
    artifact_ref: ArtifactRef,
    purpose: str,
    short_expiry: timedelta,
) -> str:
    """Always raises `PresignNotAvailableError`. See the module docstring.

    `artifact_ref`, `purpose` and `short_expiry` are accepted — not dropped —
    matching `13-…:735`'s signature, so a future implementation slots in
    without a signature change once Artifact Service ships presigning.
    """
    raise PresignNotAvailableError(
        f"Artifact Service does not presign URLs (routes/artifacts.py:95: "
        f"'No presigned URLs are used.', repeated at :435; zero hits for "
        f"'presign' anywhere in storage_service/src). Requested for artifact "
        f"{artifact_ref.artifact_id}, purpose={purpose!r}. `13-…:743` calls "
        "presigning a net-new security-qualified capability, not current "
        "behavior — it has not been built, on either service."
    )


# ------------------------------------------------------------------------------
# delete_or_crypto_shred — NOT AVAILABLE.
# ------------------------------------------------------------------------------


def delete_or_crypto_shred(
    artifact_ref: ArtifactRef,
    lawful_disposition: str,
) -> None:
    """Always raises `CryptoShredNotAvailableError`. See the module docstring.

    `lawful_disposition` is typed as `str` rather than a closed vocabulary on
    purpose: `13-…:763-777`'s disposition matrix — tenant metadata deletion,
    licensed content removal, participant/confidential data erasure, legal
    hold, artifact crypto-shredding, retained non-content hashes/audit facts/
    tombstones, already-published outputs — is seven bullets with no cells
    filled in. `IMP-DEC-005` is the open decision that fills them; inventing an
    enum here would invent a vocabulary nobody has approved.
    """
    raise CryptoShredNotAvailableError(
        f"crypto-shredding does not exist in artifact_service or storage_service, "
        f"under any name, and the lawful-disposition vocabulary "
        f"({lawful_disposition!r} was requested for artifact "
        f"{artifact_ref.artifact_id}) is IMP-DEC-005's open decision — the "
        "disposition matrix at 13-…:763-777 has no approved cells yet. Ordinary "
        "delete (routes/artifacts.py:490) exists on Artifact Service but is a "
        "DIFFERENT, weaker guarantee than what this operation's name promises, "
        "and this port will not quietly substitute one for the other."
    )


__all__ = [
    "_default_base_url",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactRef",
    "ArtifactStoreError",
    "ArtifactUpstreamError",
    "CryptoShredNotAvailableError",
    "OpenedArtifact",
    "PresignNotAvailableError",
    "ResourceContext",
    "RightsContext",
    "StagedRef",
    "StagingNotAvailableError",
    "commit",
    "delete_or_crypto_shred",
    "open",
    "presign",
    "stage",
]
