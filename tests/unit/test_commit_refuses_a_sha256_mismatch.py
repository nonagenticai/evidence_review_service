"""`commit()` is the integrity check `13-…:1020` asks for evidence of.

`artifact_service/src/routes/artifacts.py:169` computes a `sha256` over an
upload but never compares it against anything the caller supplied — there is
no `expected_sha256` parameter on `/upload` at all. `adapters.artifact_store
.commit` is where that comparison happens, and it happens over the bytes this
process already holds, BEFORE any HTTP call. These tests prove both halves:
a mismatch never reaches Artifact Service, and a match does, and gets back a
real `ArtifactRef`.

The HTTP layer is mocked with `httpx.MockTransport` — the standard-library
mechanism, so this test suite adds no new dependency and never touches a live
`artifact_service`.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest

from adapters.artifact_store import (
    ArtifactIntegrityError,
    ArtifactRef,
    ArtifactUpstreamError,
    ResourceContext,
    StagedRef,
    commit,
)

_BYTES = b"the exact bytes a reviewer staged"
_REAL_SHA256 = hashlib.sha256(_BYTES).hexdigest()


def _staged_ref(project_id: uuid.UUID) -> StagedRef:
    return StagedRef(
        data=_BYTES,
        expected_media_type="application/pdf",
        resource_context=ResourceContext(project_id=project_id, filename="protocol.pdf", kind="document"),
    )


def test_a_sha256_mismatch_raises_before_any_http_call_and_nothing_is_recorded() -> None:
    project_id = uuid.uuid4()
    staged = _staged_ref(project_id)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never run
        calls.append(request)
        return httpx.Response(200, json={"artifact_id": str(uuid.uuid4())})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArtifactIntegrityError) as excinfo:
        commit(staged, expected_sha256="0" * 64, immutable_metadata=None, client=client)

    assert calls == []  # the mismatch was caught before the network call was ever made
    assert _REAL_SHA256 in str(excinfo.value)
    assert "0" * 64 in str(excinfo.value)


def test_a_matching_sha256_uploads_and_returns_a_real_artifact_ref() -> None:
    project_id = uuid.uuid4()
    staged = _staged_ref(project_id)
    artifact_id = uuid.uuid4()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url.path == f"/projects/{project_id}/artifacts/upload"
        return httpx.Response(
            200,
            json={
                "artifact_id": str(artifact_id),
                "version": 1,
                "storage_key": f"projects/{project_id}/artifacts/{artifact_id}/v1/protocol.pdf",
                "size": len(_BYTES),
                "sha256": _REAL_SHA256,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ref = commit(
        staged,
        expected_sha256=_REAL_SHA256,
        immutable_metadata={"producing_run_id": "r-1"},
        client=client,
    )

    assert len(captured) == 1  # exactly one upload, no retry, no staging round-trip
    assert isinstance(ref, ArtifactRef)
    assert ref.artifact_id == artifact_id
    assert ref.project_id == project_id
    assert ref.sha256 == _REAL_SHA256
    assert ref.size == len(_BYTES)


def test_expected_sha256_comparison_is_case_and_whitespace_insensitive() -> None:
    """A caller pasting a hash from a log line with trailing whitespace or
    upper-case hex should not get a spurious integrity failure."""
    project_id = uuid.uuid4()
    staged = _staged_ref(project_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "artifact_id": str(uuid.uuid4()),
                "version": 1,
                "storage_key": "k",
                "size": len(_BYTES),
                "sha256": _REAL_SHA256,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ref = commit(staged, expected_sha256=f"  {_REAL_SHA256.upper()}\n", client=client)
    assert ref.artifact_id is not None


def test_an_upstream_error_status_raises_artifact_upstream_error_not_a_bare_httpx_error() -> None:
    project_id = uuid.uuid4()
    staged = _staged_ref(project_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArtifactUpstreamError) as excinfo:
        commit(staged, expected_sha256=_REAL_SHA256, client=client)

    assert "500" in str(excinfo.value)


def test_immutable_metadata_is_accepted_but_never_sent_to_artifact_service() -> None:
    """`13-…:746-747`: the domain service remains authoritative for what an
    artifact means. Artifact Service's `/upload` form has no field for
    arbitrary domain metadata, so it must never appear on the wire."""
    project_id = uuid.uuid4()
    staged = _staged_ref(project_id)
    sentinel = "SECRET_PROVENANCE_MARKER_9f3a"

    def handler(request: httpx.Request) -> httpx.Response:
        body_text = request.content.decode("utf-8", errors="ignore")
        assert sentinel not in body_text
        return httpx.Response(
            200,
            json={
                "artifact_id": str(uuid.uuid4()),
                "version": 1,
                "storage_key": "k",
                "size": len(_BYTES),
                "sha256": _REAL_SHA256,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    commit(
        staged,
        expected_sha256=_REAL_SHA256,
        immutable_metadata={"rights_snapshot": sentinel},
        client=client,
    )
