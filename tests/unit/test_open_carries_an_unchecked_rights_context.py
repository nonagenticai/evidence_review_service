"""`open()` accepts `current_rights_context` and does NOT check it — `13-…:759-760`.

The rights re-check that section requires needs a rights authority to ask, and
none exists yet: Ingestion rights is backlog item 14, and `13-…:1020` makes
`FND-009` depend on it. Dropping `current_rights_context` from the signature
would hide that gap; this port instead carries the parameter through
unexamined and says so, loudly, in its own docstring. These tests hold both
halves of that promise: the parameter travels all the way through to the
returned value untouched, AND the docstring actually records which actions are
consequently unguarded — so the next reader does not have to trust a comment
that could quietly go stale.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from adapters.artifact_store import (
    ArtifactNotFoundError,
    ArtifactRef,
    RightsContext,
)
from adapters.artifact_store import (
    open as artifact_open,
)

_CONTENT = b"%PDF-1.4 fake bytes"
# ⚠️ The ref must carry the REAL digest of `_CONTENT`. This file's subject is the
# rights context, not integrity — but `open()` now hashes the bytes it receives
# and fails closed on a mismatch, so a placeholder digest (this was
# `"deadbeef" * 8`) makes every test here die on an `ArtifactIntegrityError`
# before reaching the rights assertion. Deriving it keeps the tests measuring
# what they claim to measure, and keeps them honest if `_CONTENT` ever changes.
_CONTENT_SHA256 = hashlib.sha256(_CONTENT).hexdigest()


def _artifact_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid.uuid4(),
        version=1,
        project_id=uuid.uuid4(),
        sha256=_CONTENT_SHA256,
        storage_key="k",
        size=len(_CONTENT),
    )


def _rights_context() -> RightsContext:
    return RightsContext(
        source_document_id="doc-42",
        rights_decision_id="rd-7",
        evaluated_purpose="display",
        decision_version="v3",
        evaluated_at=datetime(2026, 1, 1, tzinfo=UTC),
        expiry=None,
        territory_constraint="EU",
    )


def test_open_returns_the_exact_rights_context_it_was_given_unchanged() -> None:
    ref = _artifact_ref()
    rights = _rights_context()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/projects/{ref.project_id}/artifacts/{ref.artifact_id}/download"
        return httpx.Response(
            200,
            content=_CONTENT,
            headers={"content-type": "application/pdf", "x-artifact-sha256": ref.sha256},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    opened = artifact_open(ref, purpose="display", current_rights_context=rights, client=client)

    # Same object identity is not required, but every field must survive —
    # a re-derived RightsContext that merely LOOKS the same would defeat the
    # point of "carried through, not consulted."
    assert opened.rights_context == rights
    assert opened.purpose == "display"
    assert opened.content == _CONTENT
    assert opened.artifact_id == ref.artifact_id


def test_open_never_refuses_on_the_rights_context_no_matter_what_it_says() -> None:
    """An expired, territory-mismatched rights context must not be refused by
    this layer — because nothing here reads it. The refusal, when it exists,
    has to come from a real rights authority the caller consults separately."""
    ref = _artifact_ref()
    obviously_expired = RightsContext(
        source_document_id="doc-1",
        rights_decision_id="rd-1",
        evaluated_purpose="redistribution",
        decision_version="v1",
        expiry=datetime(2000, 1, 1, tzinfo=UTC),
        territory_constraint="ANTARCTICA_ONLY",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CONTENT, headers={"content-type": "application/pdf"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    # Must not raise — proving there is no gate here to bypass.
    opened = artifact_open(ref, purpose="redistribution", current_rights_context=obviously_expired, client=client)
    assert opened.rights_context is obviously_expired


def test_open_raises_artifact_not_found_on_404() -> None:
    ref = _artifact_ref()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArtifactNotFoundError):
        artifact_open(ref, purpose="display", current_rights_context=_rights_context(), client=client)


# --------------------------------------------------------------------------
# The docstring itself is the record of what is unguarded. This is checked
# directly rather than trusted, because a docstring that goes stale silently
# is exactly the failure `13-…:759-760`'s gap must not be allowed to become.
# --------------------------------------------------------------------------

_ACTIONS_13_759 = (
    "display",
    "model/provider use",
    "translation",
    "quotation",
    "export",
    "redistribution",
    "publication",
)


def test_open_docstring_names_every_unguarded_action_from_13_759() -> None:
    doc = artifact_open.__doc__ or ""
    for action in _ACTIONS_13_759:
        assert action in doc, f"open()'s docstring dropped the unguarded action {action!r}"


def test_open_docstring_states_the_check_is_not_performed() -> None:
    doc = artifact_open.__doc__ or ""
    assert "NOT performed" in doc or "NOT enforced" in doc or "not checked" in doc.lower()
    assert "current_rights_context" in doc
