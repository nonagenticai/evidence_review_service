"""The evidence-snapshot manifest: its ordering, its digest and its canonical form.

`ER-007`. `ER-M021` stores a snapshot; this module decides what its bytes ARE,
which is the whole of what `14-…:364` requires — "the snapshot reconstructs
byte-identical canonical JSON … under the same serializer version".

## Why this is not `domain/corpus.py`

`corpus.member_manifest_hash` is a newline-joined SHA-256 over sorted uuids, and
its module docstring says why: the corpus seal is verified by a **deferred
constraint trigger**, so the hash has to be expressible in SQL, and RFC 8785 is
not. That trade bought in-transaction verification.

A snapshot makes the opposite trade, deliberately. Its manifest is a **released
cross-service contract** (`urn:datacompany:evidence-snapshot-manifest:1`) that
`8808`, `8813` and `8811` parse and re-hash for themselves in languages this
database cannot run a trigger in. A digest those consumers cannot reproduce is
not a contract, it is a number only this service can check — so the snapshot
hashes through `research_contracts.canonical.engine`, under the released
document's own hash profile, and verification moves from a trigger to the
consumer, which is where `16-…:§3.5` puts it anyway.

Both rules are therefore correct and they are not interchangeable. Reusing
`corpus.ORDERING_RULE` here would produce a digest no consumer could reproduce
from the released manifest, and it would look right.

## The ordering rule is the released one, spelled once

`16-…:§3.5`: "RFC 8785 preserves array order; it does not canonicalize a
mathematical set." `urn:datacompany:pinned-version-ref:1#/$defs/set_of_pins`
declares the order key `/owner_context, /entity_type, /entity_id, /version_id`.
`PINNED_REF_ORDERING_RULE` is that key, and every snapshot records which rule it
used, exactly as `corpus_versions.member_ordering_rule` does — a digest without
its ordering is not merely a different number, it is not COMPARABLE, and a
consumer recomputing under its own order would report tampering where there is
none.

## ⚠️ The hash is PROFILE-DERIVED, not a plain canonical hash

`snapshot_content_hash` calls `compute_content_hash(manifest, MANIFEST_SCHEMA_REF)`,
which derives the hash view from the released document's own annotations, and
NOT `canonical_sha256`, which hashes whatever it is handed.

The difference is not stylistic. `urn:datacompany:pinned-version-ref:1`'s
`resolved_at` is annotated `x-datacompany-hash-excluded` with reason
`resolution_observation` — it is when THIS producer happened to look the member
up. Every member of every snapshot carries one. Under a plain canonical hash,
two snapshots pinning byte-identical evidence, taken a second apart, would have
different `content_hash` values; a consumer that had pinned the first would
report the second as a different snapshot, and re-running a published analysis
would look like tampering. The profile strips those pointers before hashing, so
the digest describes the evidence and not the moment somebody asked about it.

`16-…:§3.5` also excludes an object's own `content_hash` from its own hash view.
`canonical_manifest` therefore never contains one — belt as well as braces,
since the profile would strip it anyway. Nested MEMBER hashes stay IN, which is
what makes the digest sensitive to a member whose content changed and not only
to one that was added or removed.

Nothing in a manifest is evidence. Identities, versions, digests, refs and
counts only — no title, no abstract, no extracted value. `16-…:§3.5` warns that
the hash of licensed content is itself scope-controlled, and a manifest is the
single most-copied artifact in the system.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

#: The released manifest grammar. Mirrors `ER-M021.MANIFEST_SCHEMA_REF`, which
#: is the CHECK the database enforces; two independent spellings held equal by a
#: test, for the reason `0019` gives.
MANIFEST_SCHEMA_REF = "urn:datacompany:evidence-snapshot-manifest:1"

#: The order key `urn:datacompany:pinned-version-ref:1#/$defs/set_of_pins` declares,
#: named so a stored snapshot can say which rule produced its digest.
PINNED_REF_ORDERING_RULE = "pinned_ref_key_asc"

#: Which serializer produced an artifact's bytes. `14-…:364` requires
#: byte-identical reconstruction "under the same serializer version", which is
#: uncheckable unless the artifact records one. Bumped when the canonical form
#: changes in any way a consumer could observe — a new field, a dropped field,
#: a different ordering rule.
SERIALIZER_VERSION = "rfc8785/1"

#: The one artifact every snapshot has: its own manifest, canonically
#: serialised. `13-…:§11`'s `ArtifactStore` is `FND-009` and does not exist, so
#: the bytes are REPRODUCED on demand rather than stored — see `ER-M021`.
CANONICAL_MANIFEST_ARTIFACT = "canonical_manifest"
CANONICAL_MANIFEST_MEDIA_TYPE = "application/json"

#: The bounded context every member of a snapshot taken here belongs to.
#: ⚠️ HYPHEN, not underscore. `urn:datacompany:identity-ref:1` constrains
#: `owner_context` to `^[a-z][a-z0-9-]*$`, and the first draft here wrote
#: `evidence_review`. The failure was not a clear "bad owner_context" either:
#: a failing `allOf` branch contributes NO annotations, so
#: `unevaluatedProperties: false` then rejected every sibling field as
#: unevaluated and the error named eleven innocent properties before the guilty
#: one. Worth remembering when reading any 2020-12 unevaluated-properties error.
OWNER_CONTEXT = "evidence-review"

#: A well-formed placeholder for `content_hash` while the real one is being
#: computed. `urn:datacompany:immutable-version:1` REQUIRES the field, and
#: `compute_content_hash` validates before it hashes, so a manifest cannot be
#: hashed until it already carries one. The released hash profile then strips
#: the field before hashing (`16-…:§3.5`, "removed from its own hash view"), so
#: the placeholder cannot reach the digest — which is a property rather than a
#: hope, and `tests/unit/` proves it by computing with two different
#: placeholders and asserting the results are equal.
_HASH_PLACEHOLDER = "sha256:" + "0" * 64

#: Member fields the released hash profile removes. `resolved_at` is annotated
#: `x-datacompany-hash-excluded` in `urn:datacompany:pinned-version-ref:1` with reason
#: `resolution_observation`. Spelled here as well as derived from the schema
#: because `member_manifest_hash` hashes a BARE array rather than a whole
#: manifest, and `compute_content_hash` needs a document to derive a profile
#: from. `tests/unit/` holds the two equal.
_MEMBER_HASH_EXCLUDED: frozenset[str] = frozenset({"resolved_at"})


def order_members(members: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort members by the released order key.

    Sorted on the TEXT form of each field, for the reason `corpus.order_members`
    gives about its own rule: it removes the question of whether two runtimes
    compare uuids as bytes or as strings. For canonical lowercase-hyphenated
    uuids the two orders coincide; pinning the text form means they coincide by
    construction rather than by luck.
    """
    return sorted(
        members,
        key=lambda m: (
            str(m["owner_context"]),
            str(m["entity_type"]),
            str(m["entity_id"]),
            str(m["version_id"]),
        ),
    )


def member_manifest_hash(members: Sequence[dict[str, Any]]) -> str:
    """`sha256:<hex>` over the ORDERED member list, with `resolved_at` removed.

    A plain canonical hash over the members would carry each one's
    `resolved_at`, which is the moment this producer looked it up and not a fact
    about the evidence — see the module docstring. The field is stripped here
    rather than left to the caller, because the digest's whole purpose is that
    a consumer can recompute it from the manifest it received and get the same
    number.

    An empty membership hashes the empty array rather than being a special
    case — `corpus.member_manifest_hash` records why a special case is where two
    implementations first diverge, and the reasoning carries over unchanged.
    """
    from research_contracts.canonical.engine import canonical_sha256

    stripped = [
        {k: v for k, v in member.items() if k not in _MEMBER_HASH_EXCLUDED} for member in order_members(members)
    ]
    return canonical_sha256(stripped)


def canonical_manifest(
    *,
    snapshot_id: str,
    entity_id: str,
    review_id: str,
    tenant_id: str,
    version_number: int,
    supersedes_snapshot_id: str | None,
    intended_use: str,
    created_at: str,
    created_by: str,
    members: Sequence[dict[str, Any]],
    member_manifest_digest: str,
    protocol_pin: dict[str, Any] | None,
    corpus_pin: dict[str, Any] | None,
    artifacts: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The manifest as the released grammar defines it, ready to hash or serve.

    `content_hash` is present as a PLACEHOLDER, not as the real digest. The
    released `immutable-version` grammar requires the field and
    `compute_content_hash` validates before it hashes, so a manifest with no
    hash cannot be hashed at all; and `16-…:§3.5` removes the field from its own
    hash view, so the placeholder cannot reach the result. `seal` is the
    function that turns a placeholder manifest into a real one.

    Optional pins are omitted rather than set to `null`. The released document
    does not admit `null` for them, and a key whose value is `null` is a
    different canonical string from an absent key — so emitting one would give
    two producers holding identical evidence two different digests, which is the
    exact failure the manifest exists to prevent.
    """
    ordered = order_members(members)
    manifest: dict[str, Any] = {
        "schema_ref": MANIFEST_SCHEMA_REF,
        # Present, and deliberately a placeholder — see `_HASH_PLACEHOLDER`.
        # `seal` replaces it with the real digest once that digest exists.
        "content_hash": _HASH_PLACEHOLDER,
        "version_id": snapshot_id,
        "entity_id": entity_id,
        "version_number": version_number,
        "created_at": created_at,
        "created_by": created_by,
        "review_id": review_id,
        "intended_use": intended_use,
        "members": ordered,
        "member_count": len(ordered),
        "member_manifest_hash": member_manifest_digest,
    }
    if supersedes_snapshot_id is not None:
        manifest["supersedes_version_id"] = supersedes_snapshot_id
    if protocol_pin is not None:
        manifest["protocol_pin"] = protocol_pin
    if corpus_pin is not None:
        manifest["corpus_pin"] = corpus_pin
    if artifacts:
        manifest["artifacts"] = list(artifacts)
    # `tenant_id` rides on each member's `resource_tenant_id` rather than
    # sitting at the root: the released `immutable-version` grammar has no root
    # tenant field, and inventing one here would make this service's manifests
    # fail a consumer validating against the document we ourselves released.
    _ = tenant_id
    return manifest


def seal(manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return `(manifest with its real content_hash, that hash)`.

    The only supported way to finish a manifest. Callers do not set
    `content_hash` themselves: a hand-set hash is the one field whose wrongness
    is invisible until a consumer recomputes it, and by then the snapshot is
    immutable and somebody has published against it.
    """
    digest = snapshot_content_hash(manifest)
    return {**manifest, "content_hash": digest}, digest


def snapshot_content_hash(manifest: dict[str, Any]) -> str:
    """The manifest's own hash, under the released document's hash profile.

    See the module docstring for why this is `compute_content_hash` and not
    `canonical_sha256`.
    """
    from research_contracts.canonical.engine import compute_content_hash

    return compute_content_hash(manifest, MANIFEST_SCHEMA_REF)


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    """The portable artifact's bytes: RFC 8785 JSON, UTF-8.

    ⚠️ These bytes are the WHOLE manifest, including the `resolved_at` fields
    the hash profile strips. That asymmetry is deliberate and it is the reason
    the artifact records `payload_digest` (over these bytes) while the snapshot
    records `content_hash` (over the hash view): `16-…:§3.5` says the two "MUST
    NOT be substituted for one another", and here is exactly why. The artifact is
    what a consumer downloads and must be able to verify byte for byte; the
    content hash is what a consumer PINS and must not change when nothing about
    the evidence did.
    """
    from research_contracts.canonical.engine import canonical_json_bytes

    return canonical_json_bytes(manifest)


def pin(
    *,
    entity_type: str,
    entity_id: str,
    version_id: str,
    version_number: int,
    schema_ref: str,
    content_hash: str,
    tenant_id: str,
    resolved_at: str,
) -> dict[str, Any]:
    """One member, shaped as `urn:datacompany:pinned-version-ref:1`.

    `resource_scope` is always `tenant` here and `resource_tenant_id` is
    therefore always present: every table this service pins from is
    tenant-scoped and under `FORCE ROW LEVEL SECURITY`. Emitting `global` would
    validate against the released document and would describe a row that does
    not exist.
    """
    return {
        "owner_context": OWNER_CONTEXT,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "resource_scope": "tenant",
        "resource_tenant_id": tenant_id,
        "version_id": version_id,
        "version_number": version_number,
        "schema_ref": schema_ref,
        "content_hash": content_hash,
        "resolved_at": resolved_at,
    }
