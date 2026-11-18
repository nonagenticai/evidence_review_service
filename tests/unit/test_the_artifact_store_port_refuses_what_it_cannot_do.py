"""`stage`, `presign` and `delete_or_crypto_shred` are not built — `FND-009`.

`13-…:743-744` calls presigning and crypto-shredding "net-new
security-qualified Artifact Service capabilities, not assumed current
behavior," and the deployed `artifact_service` has no staging endpoint either
(`routes/artifacts.py:67` is single-shot). `adapters.artifact_store` refuses
all three loudly rather than approximating them: each raises its own named,
importable error, never `None`, never a silent no-op. These tests exist so
that "not built" cannot regress into "quietly returns something that looks
like success" without a red test naming exactly which operation stopped
refusing.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from adapters.artifact_store import (
    ArtifactRef,
    CryptoShredNotAvailableError,
    PresignNotAvailableError,
    ResourceContext,
    StagingNotAvailableError,
    delete_or_crypto_shred,
    presign,
    stage,
)


def _artifact_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid.uuid4(),
        version=1,
        project_id=uuid.uuid4(),
        sha256="a" * 64,
        storage_key="projects/x/artifacts/y/v1/z.pdf",
        size=1024,
    )


def test_stage_raises_staging_not_available_and_names_the_missing_endpoint() -> None:
    resource_context = ResourceContext(project_id=uuid.uuid4(), filename="report.pdf", kind="document")

    with pytest.raises(StagingNotAvailableError) as excinfo:
        stage(b"some bytes", "application/pdf", resource_context)

    # Named the exact gap, not a generic "not implemented" — a caller reading
    # only the exception message must be able to tell this apart from a bug.
    assert "routes/artifacts.py:67" in str(excinfo.value)
    assert "staged reference" not in str(excinfo.value)  # never claims to have made one


def test_presign_raises_presign_not_available_and_cites_the_disclaimer() -> None:
    ref = _artifact_ref()

    with pytest.raises(PresignNotAvailableError) as excinfo:
        presign(ref, purpose="download", short_expiry=timedelta(minutes=5))

    message = str(excinfo.value)
    assert "No presigned URLs are used" in message
    assert str(ref.artifact_id) in message  # names which artifact was asked for


def test_delete_or_crypto_shred_raises_crypto_shred_not_available() -> None:
    ref = _artifact_ref()

    with pytest.raises(CryptoShredNotAvailableError) as excinfo:
        delete_or_crypto_shred(ref, lawful_disposition="tenant_metadata_deletion")

    message = str(excinfo.value)
    assert "IMP-DEC-005" in message
    assert "tenant_metadata_deletion" in message  # the requested disposition is named, not swallowed
