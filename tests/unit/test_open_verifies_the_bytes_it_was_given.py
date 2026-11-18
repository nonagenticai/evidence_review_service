"""`open()` must hash what it received. Its first version could not fail.

The original `open()` built its result with
`sha256=response.headers.get("x-artifact-sha256", artifact_ref.sha256)`. The
default is the defect: with the header absent — and the deployed
`artifact_service`'s `/download` is not known to send it — the `sha256` handed
back was, by construction, the value the caller was about to compare against
`artifact_ref.sha256`. The comparison was `x == x`. A server returning
`b"TAMPERED CONTENT"` was indistinguishable from one returning the committed
bytes, and no caller could have detected the difference. There was NO test over
`open()` at all, which is why it shipped.

An immutable artifact store exists so that what comes back is provably what was
committed. `commit()` already hashes the bytes it sends
(`artifact_store.py`'s `computed_sha256` check); these tests hold `open()` to
the same standard on the way back, and they are written so that restoring the
header-with-default would turn each of them red rather than leaving them green
on a tautology.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest

from adapters.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactUpstreamError,
)
from adapters.artifact_store import (
    open as open_artifact,
)

COMMITTED = b"the bytes that were committed"
COMMITTED_SHA256 = hashlib.sha256(COMMITTED).hexdigest()


def _ref(sha256: str = COMMITTED_SHA256) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid.uuid4(),
        version=1,
        project_id=uuid.uuid4(),
        sha256=sha256,
        storage_key="projects/x/artifacts/y/v1/z.bin",
        size=len(COMMITTED),
    )


def _client(body: bytes, headers: dict[str, str] | None = None, status: int = 200) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _open(ref: ArtifactRef, client: httpx.Client):
    return open_artifact(
        ref,
        purpose="display",
        current_rights_context=None,
        base_url="http://artifact-service.invalid",
        client=client,
    )


def test_bytes_that_do_not_hash_to_the_committed_digest_are_refused() -> None:
    """The exact repro that the header-with-default version accepted."""
    with pytest.raises(ArtifactIntegrityError) as caught:
        _open(_ref(), _client(b"TAMPERED CONTENT"))

    message = str(caught.value)
    # Both digests are named, so an operator can tell substitution from corruption
    # without re-fetching, and so the failure cannot be read as a transport error.
    assert COMMITTED_SHA256 in message
    assert hashlib.sha256(b"TAMPERED CONTENT").hexdigest() in message


def test_tampered_bytes_are_not_returned_to_the_caller() -> None:
    """Refusing must mean withholding. A caller that catches the error and reads
    on anyway would be back where it started, so there is nothing to read on to."""
    with pytest.raises(ArtifactIntegrityError) as caught:
        _open(_ref(), _client(b"TAMPERED CONTENT"))

    assert not hasattr(caught.value, "content")
    assert b"TAMPERED CONTENT" not in str(caught.value).encode()


def test_the_committed_bytes_pass_and_the_digest_is_computed_not_echoed() -> None:
    opened = _open(_ref(), _client(COMMITTED))

    assert opened.content == COMMITTED
    assert opened.sha256 == COMMITTED_SHA256


def test_the_digest_is_computed_even_when_the_header_is_absent() -> None:
    """The header is not the source of truth and its absence is not a waiver.

    This is the test the original code could not have passed for the right
    reason: it passed then too, but only because the value was echoed from the
    ref rather than derived from the bytes. Pairing it with the tampered case
    above is what makes it load-bearing.
    """
    opened = _open(_ref(), _client(COMMITTED, headers={}))

    assert opened.sha256 == hashlib.sha256(opened.content).hexdigest()


def test_a_header_that_contradicts_its_own_payload_is_refused() -> None:
    """Bytes that hash correctly but arrive under a wrong advertised digest mean
    Artifact Service disagrees with its own response. Preferring either side
    silently would hide a real defect in the service."""
    with pytest.raises(ArtifactIntegrityError) as caught:
        _open(_ref(), _client(COMMITTED, headers={"x-artifact-sha256": "0" * 64}))

    assert "contradicts itself" in str(caught.value)


def test_a_matching_header_is_accepted() -> None:
    opened = _open(_ref(), _client(COMMITTED, headers={"x-artifact-sha256": COMMITTED_SHA256.upper()}))

    assert opened.sha256 == COMMITTED_SHA256


def test_the_reference_digest_is_compared_case_insensitively() -> None:
    """`ArtifactRef.sha256` arriving uppercase is a formatting difference, not a
    mismatch — the same normalisation `commit()` applies to `expected_sha256`."""
    opened = _open(_ref(sha256=COMMITTED_SHA256.upper()), _client(COMMITTED))

    assert opened.sha256 == COMMITTED_SHA256


def test_an_empty_body_is_refused_rather_than_treated_as_a_valid_artifact() -> None:
    """A truncated-to-zero response hashes to the digest of no bytes, which is a
    real sha256 and is not this artifact's."""
    with pytest.raises(ArtifactIntegrityError):
        _open(_ref(), _client(b""))


def test_a_404_is_still_a_not_found_and_not_an_integrity_failure() -> None:
    """Integrity checking must not swallow the transport errors that came first:
    those are different faults with different operator responses."""
    with pytest.raises(ArtifactNotFoundError):
        _open(_ref(), _client(b"", status=404))


def test_a_500_is_still_an_upstream_error() -> None:
    with pytest.raises(ArtifactUpstreamError):
        _open(_ref(), _client(b"boom", status=500))


# ---------------------------------------------------------------------------
# `Content-Disposition` carries more than one parameter in practice.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param('attachment; filename="a.pdf"', "a.pdf", id="simple"),
        pytest.param(
            'attachment; filename="a.pdf"; creation-date="Mon, 1 Jan 2026 00:00:00 GMT"',
            "a.pdf",
            # The old parser stripped quotes off everything after `filename=`,
            # so a quoted value followed by another parameter returned
            # `a.pdf"; creation-date="Mon` — a filename containing a quote, a
            # semicolon and half a date.
            id="second-parameter-does-not-leak-in",
        ),
        pytest.param("attachment; filename=a.pdf; size=10", "a.pdf", id="unquoted-with-following-parameter"),
        pytest.param("attachment", "", id="absent"),
        pytest.param(None, "", id="no-header"),
    ],
)
def test_the_filename_is_only_the_filename(header, expected) -> None:
    from adapters.artifact_store import _filename_from_content_disposition

    assert _filename_from_content_disposition(header) == expected


def test_a_filename_reaches_the_caller_through_open() -> None:
    opened = _open(
        _ref(),
        _client(COMMITTED, headers={"content-disposition": 'attachment; filename="report.pdf"; size=10'}),
    )

    assert opened.filename == "report.pdf"
