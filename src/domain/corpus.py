"""The corpus member manifest — the Python half of a hash Postgres also computes.

`ER-V011` / `DM-AT-05` want the sealed membership, the count and the manifest
hash written in one transaction and verified, not asserted. The verification
lives in a deferred constraint trigger (``corpus_versions_verify_seal`` in
`ER-M003`), which means the hash has to be expressible in SQL. This module is the
Python expression of the *same* definition, so the service can compute the value
it is about to seal, and `tests/integration/test_corpus_seal.py` can prove the
two agree byte for byte.

Deliberately **not** `research_contracts.canonical.canonical_sha256`. That
function canonicalizes JSON per RFC 8785, which a Postgres trigger cannot
reproduce; using it would have meant giving up in-transaction verification —
trading the guarantee for the nicer provenance. RFC 8785 hashing is still used
where a schema-declared canonical hash is genuinely what the contract asks for:
the **export** seal in `ER-M005`, through
``compute_content_hash(instance, schema_ref)``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from uuid import UUID

#: The only ordering rule this release implements. Recorded on every corpus
#: version, because a manifest hash without its ordering is not reproducible.
ORDERING_RULE = "record_version_id_asc"


def order_members(member_version_ids: Iterable[UUID | str]) -> list[str]:
    """Apply `record_version_id_asc`: ascending lexicographic on the text form.

    Lexicographic on the *text*, not on the UUID's byte value, because that is
    what ``ORDER BY review_record_version_id::text ASC`` does in the trigger.
    For canonical lowercase-hyphenated UUIDs the two orders coincide, but pinning
    the text form removes the question.
    """
    return sorted(str(value) for value in member_version_ids)


def member_manifest_hash(member_version_ids: Iterable[UUID | str]) -> str:
    """``sha256:<hex>`` over the newline-joined ordered member version ids.

    An empty membership hashes the empty string rather than being a special
    case — the trigger's ``coalesce(string_agg(...), '')`` does the same, and a
    special case is where the two implementations would first diverge.
    """
    joined = "\n".join(order_members(member_version_ids))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
