"""`13-…:739`: "The domain adapter calls Artifact Service only."

`artifact_service` itself sits in front of `storage_service`, which is the
ONLY thing holding MinIO credentials (`storage_service.py:26-31`). If this
adapter ever imported a MinIO SDK, a `storage_service` client, or an S3-shaped
library, the chain `13-…:739-741` requires would be broken from this side
regardless of what `artifact_service` does — this service would have grown
its own second path to the object store, unaudited and un-credentialed
through the one place credentials are supposed to live.

This is checked by parsing the module's own AST rather than trusting the
module docstring's claim, because a docstring saying "we never do this" is
exactly the kind of claim that goes stale silently the day someone adds an
import under time pressure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import adapters.artifact_store as artifact_store_module

#: Substrings that would indicate a direct storage-layer or MinIO dependency,
#: matched against every imported module/name in the file (not against prose —
#: the module docstring is free to explain the boundary using these exact
#: words, which is why this checks IMPORTS, not the raw text of the file).
_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "minio",
    "boto3",
    "boto",
    "s3",
    "storage_service",
    "StorageService",
)


def _module_source_path() -> Path:
    return Path(artifact_store_module.__file__)  # type: ignore[arg-type]


def _imported_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_the_module_imports_only_the_standard_library_and_httpx() -> None:
    source = _module_source_path().read_text()
    tree = ast.parse(source)
    imported = _imported_names(tree)

    # The allow-list: httpx (to call Artifact Service) plus stdlib. Anything
    # else appearing here is a new dependency this test did not expect, and
    # the assertion below is what actually enforces the MinIO/storage boundary.
    allowed_roots = {
        "httpx",
        # This service's own settings, and the ONLY first-party import permitted
        # here. It was added when the Artifact Service URL moved out of a
        # module-level `os.getenv` — which, used as a default ARGUMENT, froze at
        # import time and could not be changed even by a test.
        #
        # ⚠️ Widening this set is exactly how a guard rots, so be precise about
        # what it guards: NOT "no first-party imports", but "never reaches MinIO
        # or the Storage Service directly", which `13-…:739-741` makes strict.
        # `config` cannot reach either — it is a dataclass of environment reads.
        # The two assertions below are the ones that enforce the boundary, and
        # they are deliberately independent of this list.
        "config",
        "hashlib",
        "io",
        "uuid",
        "dataclasses",
        "datetime",
        "typing",
        "__future__",
    }
    unexpected = [name for name in imported if name.split(".")[0] not in allowed_roots]
    assert unexpected == [], f"adapters.artifact_store imports beyond the expected allow-list: {unexpected}"


def test_no_import_names_minio_boto3_or_a_storage_service_client() -> None:
    source = _module_source_path().read_text()
    tree = ast.parse(source)
    imported = _imported_names(tree)

    lowered = [name.lower() for name in imported]
    hits = [
        name
        for name, lowered_name in zip(imported, lowered, strict=True)
        for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS
        if forbidden.lower() in lowered_name
    ]
    assert hits == [], f"adapters.artifact_store imports a storage/MinIO-shaped dependency: {hits}"


def test_the_only_network_client_constructed_is_httpx() -> None:
    """Belt-and-braces: confirm the one client class actually instantiated in
    the module body is `httpx.Client`, by inspecting call sites in the AST."""
    source = _module_source_path().read_text()
    tree = ast.parse(source)

    client_call_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "Client":
                if isinstance(func.value, ast.Name):
                    client_call_names.add(func.value.id)
            elif isinstance(func, ast.Name) and func.id.endswith("Client"):
                client_call_names.add(func.id)

    assert client_call_names <= {"httpx"}, (
        f"a non-httpx client is constructed in adapters.artifact_store: {client_call_names}"
    )


def test_the_module_object_itself_has_no_storage_or_minio_attribute() -> None:
    """A sibling check at runtime: nothing importable off the module is named
    after Storage Service or MinIO — e.g. no re-exported client instance."""
    for attr_name in dir(artifact_store_module):
        lowered = attr_name.lower()
        assert "minio" not in lowered
        assert "storageservice" not in lowered.replace("_", "")
