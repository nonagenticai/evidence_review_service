"""A snapshot manifest hashes the evidence, and not the moment somebody asked.

`ER-007`. `domain/snapshot_manifest` decides what a snapshot's bytes ARE, which
is the whole of what `14-…:364` requires — "the snapshot reconstructs
byte-identical canonical JSON … under the same serializer version". This file is
the proof that it does, and it is a *unit* file: no database, no fixtures, no
network. Everything asserted here is a pure function of its arguments.

The claims under test are not stylistic. `urn:datacompany:evidence-snapshot-manifest:1`
is a released cross-service contract that `8808`, `8813` and `8811` parse and
**re-hash for themselves**, in languages this service does not run. That makes
every property below a property a consumer depends on:

* if the ORDER of the member list reached the digest, two producers holding
  byte-identical evidence would compute different numbers and each would call
  the other tampered;
* if `resolved_at` reached the digest, the SAME producer would compute different
  numbers a second apart, and re-running a published analysis would look like
  tampering by its own author;
* if a member's `content_hash` did NOT reach the digest, the manifest would be
  sensitive only to members added and removed, and evidence could be swapped
  underneath a pinned snapshot without the number moving.

The first two are invariance claims and the third is their negative control.
Invariance is the failure mode that passes vacuously: a function that hashes
nothing at all satisfies both invariance tests perfectly, so the sensitivity
test is not optional decoration — without it the two above prove nothing.

⚠️⚠️ THIS FILE CARRIES A MODULE-LEVEL SKIP AND THE SKIP MUST BE DELETED. ⚠️⚠️
`urn:datacompany:evidence-snapshot-manifest:1` is released in `research_contracts`
0.6.0. `pyproject.toml` already floors the dependency at `research-contracts>=0.6.0`,
so the skip below exists ONLY to keep this file green on a runner whose
environment was resolved before that release landed. The moment the floor is
satisfied everywhere — the moment a fresh `uv sync` anywhere in this fleet gets
0.6.0 — `_HAS_MANIFEST_SCHEMA` and the `pytestmark` line are to be REMOVED. A
skip that becomes permanent is not caution, it is an unexercised claim wearing a
green tick: the tests still exist, still read as coverage, and can no longer
fail. `pytest.importorskip` would be worse still — the package imports fine at
0.5.0, so an import guard would never trip and the skip would be silent.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import pytest
from research_contracts.canonical.engine import assert_valid_instance
from research_contracts.canonical.registry import canonical_schemas

from domain.snapshot_manifest import (
    _HASH_PLACEHOLDER,
    _MEMBER_HASH_EXCLUDED,
    MANIFEST_SCHEMA_REF,
    OWNER_CONTEXT,
    canonical_bytes,
    canonical_manifest,
    member_manifest_hash,
    order_members,
    pin,
    seal,
    snapshot_content_hash,
)

PINNED_REF_SCHEMA_REF = "urn:datacompany:pinned-version-ref:1"

# ⚠️ DELETE BOTH OF THESE once `research-contracts>=0.6.0` is satisfied
# everywhere — see the module docstring. They are a release-window shim, not a
# permanent tolerance.
_HAS_MANIFEST_SCHEMA = MANIFEST_SCHEMA_REF in canonical_schemas()
pytestmark = pytest.mark.skipif(
    not _HAS_MANIFEST_SCHEMA,
    reason="needs research_contracts >= 0.6.0 (releases urn:datacompany:evidence-snapshot-manifest:1)",
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _digest() -> str:
    """A well-formed, distinct `sha256:<64 hex>`.

    Built from two uuid hexes rather than from a real hash of anything: these
    fixtures stand for digests the manifest CARRIES, and a manifest never
    recomputes a member's hash. Using a shape-correct but arbitrary value keeps
    a reader from believing the number means something it does not.
    """
    return "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex


def _member(
    *,
    owner_context: str | None = None,
    entity_type: str = "observation",
    entity_id: str | None = None,
    version_id: str | None = None,
    content_hash: str | None = None,
    resolved_at: str = "2026-09-03T10:00:00Z",
) -> dict[str, Any]:
    """One valid `urn:datacompany:pinned-version-ref:1`, every field overridable."""
    member = pin(
        entity_type=entity_type,
        entity_id=entity_id or _uuid(),
        version_id=version_id or _uuid(),
        version_number=1,
        schema_ref="urn:datacompany:evidence-observation:1",
        content_hash=content_hash or _digest(),
        tenant_id=_uuid(),
        resolved_at=resolved_at,
    )
    if owner_context is not None:
        member["owner_context"] = owner_context
    return member


def _manifest(
    *,
    members: list[dict[str, Any]] | None = None,
    protocol_pin: dict[str, Any] | None = None,
    corpus_pin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A complete placeholder manifest, `version_number` 1 and no predecessor.

    Version 1 on purpose: `urn:datacompany:immutable-version:1`'s supersession
    conditional REQUIRES `supersedes_version_id` for every version above 1 and
    FORBIDS it at 1, so a fixture that pairs a version number with the wrong
    supersession is rejected for a reason that has nothing to do with the
    property under test.
    """
    if members is None:
        members = [_member(), _member()]
    return canonical_manifest(
        snapshot_id=_uuid(),
        entity_id=_uuid(),
        review_id=_uuid(),
        tenant_id=_uuid(),
        version_number=1,
        supersedes_snapshot_id=None,
        intended_use="analysis_run",
        created_at="2026-09-03T10:00:00Z",
        created_by="evidence-review/test",
        members=members,
        member_manifest_digest=member_manifest_hash(members),
        protocol_pin=protocol_pin,
        corpus_pin=corpus_pin,
    )


def test_order_members_sorts_by_the_released_order_key() -> None:
    """`(owner_context, entity_type, entity_id, version_id)`, in that priority.

    The key is not this service's invention: it is declared by
    `urn:datacompany:pinned-version-ref:1#/$defs/set_of_pins`, and `16-…:§3.5` is
    explicit that RFC 8785 preserves array order and does NOT canonicalize a
    set. So the sort IS the canonicalization — a consumer re-sorts under this
    exact key before it re-hashes.

    The fixtures below are chosen so each field is the tie-breaker for exactly
    one pair: swapping any two components of the key, or dropping the fourth
    because "uuids are unique anyway", reorders this list and turns the
    assertion red. If this fails, every digest this service has ever published
    is computed under a different rule from the one consumers apply, and the
    symptom at the consumer is `member_manifest_hash` mismatch on evidence that
    is in fact identical.
    """
    shared_entity = "11111111-1111-4111-8111-111111111111"
    expected = [
        _member(
            owner_context="alpha-context", entity_type="assertion", entity_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        ),
        _member(
            owner_context="alpha-context", entity_type="observation", entity_id="00000000-0000-4000-8000-000000000000"
        ),
        # Same owner_context, same entity_type, same entity_id — separated only
        # by the FOURTH component of the key.
        _member(
            owner_context="alpha-context",
            entity_type="observation",
            entity_id=shared_entity,
            version_id="22222222-2222-4222-8222-222222222222",
        ),
        _member(
            owner_context="alpha-context",
            entity_type="observation",
            entity_id=shared_entity,
            version_id="33333333-3333-4333-8333-333333333333",
        ),
        _member(
            owner_context="zeta-context", entity_type="assertion", entity_id="00000000-0000-4000-8000-000000000000"
        ),
    ]
    shuffled = [expected[3], expected[0], expected[4], expected[2], expected[1]]

    assert order_members(shuffled) == expected


def test_the_member_hash_does_not_depend_on_the_order_it_was_handed() -> None:
    """The digest a consumer recomputes must not encode this producer's loop.

    `members` is a SET in the released grammar. The order this service happens
    to read rows in — a query plan, a dict iteration, a page boundary — is not a
    fact about the evidence, and if it reached the digest then two producers
    holding byte-identical evidence would publish different numbers. A consumer
    that pinned one would report the other as a different snapshot, which is
    indistinguishable from tampering and would be investigated as such.

    Several shuffles rather than one, seeded so a failure reproduces: a single
    shuffle can accidentally return the sorted order and pass against a function
    that never sorts at all.
    """
    members = [_member() for _ in range(6)]
    expected = member_manifest_hash(members)

    rng = random.Random(20260903)
    for attempt in range(8):
        shuffled = members[:]
        rng.shuffle(shuffled)
        assert member_manifest_hash(shuffled) == expected, f"digest moved on shuffle {attempt}: {shuffled}"


def test_the_member_hash_ignores_resolved_at() -> None:
    """The defect the released hash profile exists to prevent.

    `resolved_at` is annotated `x-datacompany-hash-excluded` in
    `urn:datacompany:pinned-version-ref:1` with reason `resolution_observation`, and
    the reason is the whole point: it records when THIS producer happened to
    look the member up. It is not a property of the evidence. Every member of
    every snapshot carries one.

    Under a plain canonical hash, two snapshots pinning byte-identical evidence
    taken a second apart would carry different `member_manifest_hash` values. A
    consumer that pinned the first would report the second as different
    evidence; an author re-running their own published analysis would be told,
    by the system, that the record had been altered. Nothing would have changed
    except the clock.

    The two member lists here differ in `resolved_at` and in NOTHING else —
    same identities, same versions, same content hashes — so the only way this
    can fail is if the stripping stopped happening.
    """
    base = [_member(resolved_at="2026-09-03T10:00:00Z") for _ in range(4)]
    later = [{**member, "resolved_at": "2026-09-03T10:00:01Z"} for member in base]

    assert [{k: v for k, v in m.items() if k != "resolved_at"} for m in base] == [
        {k: v for k, v in m.items() if k != "resolved_at"} for m in later
    ], "fixture error: the two lists differ in more than resolved_at"
    assert member_manifest_hash(later) == member_manifest_hash(base)


def test_the_member_hash_changes_when_a_members_content_hash_changes() -> None:
    """The non-vacuity control for the two invariance tests above.

    A function that returned a constant — or hashed only the member COUNT —
    would satisfy "invariant to order" and "invariant to resolved_at" perfectly,
    and both tests above would be green while proving nothing whatsoever. This
    is the test that makes them mean something.

    It is also the substantive claim: `16-…` authoring decision 4 says a
    consumer recomputing this digest catches "a member whose CONTENT changed,
    not merely one that went missing". If a member's `content_hash` did not
    reach the digest, evidence could be substituted underneath an already-pinned
    snapshot and every consumer would verify it as unchanged.
    """
    members = [_member() for _ in range(3)]
    before = member_manifest_hash(members)

    altered = [{**members[0], "content_hash": _digest()}, *members[1:]]
    assert altered[0]["content_hash"] != members[0]["content_hash"], "fixture error: content_hash did not change"

    assert member_manifest_hash(altered) != before


def test_an_optional_pin_is_omitted_rather_than_emitted_as_null() -> None:
    """An absent key and a null-valued key are DIFFERENT canonical strings.

    RFC 8785 serialises `{"protocol_pin": null}` and `{}` differently, so a
    producer that emits an explicit null and one that omits the key compute
    different digests over identical evidence — the exact failure the manifest
    exists to prevent, arriving through a field that is not even set. The
    released document compounds it: `protocol_pin` and `corpus_pin` `$ref` a
    `pinned-version-ref`, which does not admit `null` at all, so an emitted null
    would also fail validation at every consumer.

    Both halves are asserted. The named-key check states the specific claim; the
    sweep over every value catches the same mistake arriving in a field added
    later, which is where it would otherwise go unnoticed.
    """
    manifest = _manifest(protocol_pin=None, corpus_pin=None)

    assert "protocol_pin" not in manifest
    assert "corpus_pin" not in manifest
    assert "supersedes_version_id" not in manifest

    nulls = sorted(key for key, value in manifest.items() if value is None)
    assert nulls == [], f"manifest carries null-valued keys instead of omitting them: {nulls}"


def test_the_manifest_carries_a_placeholder_and_seal_replaces_it() -> None:
    """`canonical_manifest` cannot know the hash; `seal` is what supplies it.

    The ordering is forced, not chosen. `urn:datacompany:immutable-version:1`
    REQUIRES `content_hash`, and `compute_content_hash` validates before it
    hashes — so a manifest with no `content_hash` cannot be hashed at all, and a
    manifest cannot carry its real hash before it has been hashed. The
    placeholder resolves the circularity.

    `seal` is then the only supported way to finish one, and it returns the
    digest as well as the sealed manifest so a caller never has to reach back
    into the dict for it. A caller that hand-set `content_hash` instead would
    produce the one field whose wrongness is invisible until a consumer
    recomputes it — by which time the snapshot is immutable and somebody has
    published against it.
    """
    manifest = _manifest()
    assert manifest["content_hash"] == _HASH_PLACEHOLDER

    sealed, digest = seal(manifest)

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert digest != _HASH_PLACEHOLDER
    assert sealed["content_hash"] == digest
    assert manifest["content_hash"] == _HASH_PLACEHOLDER, (
        "seal mutated its argument instead of returning a new manifest"
    )
    assert {k: v for k, v in sealed.items() if k != "content_hash"} == {
        k: v for k, v in manifest.items() if k != "content_hash"
    }, "seal changed something other than content_hash"


def test_the_placeholder_cannot_reach_the_digest() -> None:
    """An object's own `content_hash` is removed from its own hash view.

    `16-…:§3.5` requires it and `urn:datacompany:immutable-version:1` annotates the
    field `x-datacompany-hash-excluded` with reason `own_content_hash`, so
    `snapshot_content_hash` must return the same number regardless of what sits
    in `content_hash` when it is called.

    This is what makes the placeholder a PROPERTY rather than a hope. If it
    failed, `seal` would be non-idempotent in the worst possible way: hashing a
    sealed manifest would produce a different digest from the one it carries,
    and every consumer performing the verification the contract asks of them —
    recompute, compare — would report a valid snapshot as corrupt. Note the
    substitute here is a well-formed `sha256:` value, not garbage: validation
    runs before hashing, so a malformed one would be rejected for the wrong
    reason and this test would pass without ever exercising the exclusion.
    """
    manifest = _manifest()
    expected = snapshot_content_hash(manifest)

    other = _digest()
    assert other != manifest["content_hash"], "fixture error: substitute equals the placeholder"

    assert snapshot_content_hash({**manifest, "content_hash": other}) == expected
    assert snapshot_content_hash({**manifest, "content_hash": expected}) == expected


def test_a_sealed_manifest_validates_against_the_released_schema() -> None:
    """The shape this service emits is the shape it published.

    Every property above is about the digest; none of them says the document is
    one a consumer can parse. This does. `8808`, `8813` and `8811` validate
    against `urn:datacompany:evidence-snapshot-manifest:1` before they read a
    manifest, so a field this service spells differently — or omits, or invents
    — is not a cosmetic drift: it is a snapshot no consumer will accept, and the
    rejection happens at their end, after the snapshot is immutable.

    Both forms are validated. The placeholder form must be valid too, because
    `compute_content_hash` validates before it hashes: an unsealed manifest that
    does not validate can never be sealed, and the error would surface as a hash
    failure rather than as the schema problem it is.
    """
    manifest = _manifest(protocol_pin=_member(entity_type="protocol"), corpus_pin=_member(entity_type="corpus"))
    assert_valid_instance(manifest, MANIFEST_SCHEMA_REF)

    sealed, _digest_value = seal(manifest)
    assert_valid_instance(sealed, MANIFEST_SCHEMA_REF)

    assert sealed["schema_ref"] == MANIFEST_SCHEMA_REF
    assert sealed["member_count"] == len(sealed["members"])


def test_canonical_bytes_are_deterministic_across_calls() -> None:
    """The portable artifact reconstructs byte-identically, which is `14-…:364`.

    The completion evidence for `ER-007` is literally that "the snapshot
    reconstructs byte-identical canonical JSON … under the same serializer
    version". `13-…:§11`'s `ArtifactStore` is `FND-009` and does not exist, so
    these bytes are REPRODUCED on demand rather than stored — which turns
    determinism from a nicety into the only thing standing between a consumer's
    recorded `payload_digest` and a download that no longer matches it.

    A non-empty result is asserted alongside, because a function returning `b""`
    is perfectly deterministic.
    """
    sealed, _digest_value = seal(_manifest())

    first = canonical_bytes(sealed)
    assert isinstance(first, bytes)
    assert len(first) > 0

    for _attempt in range(4):
        assert canonical_bytes(sealed) == first

    # The same manifest reached by a different route serialises identically —
    # canonical form is a function of the value, not of how the dict was built.
    assert canonical_bytes(dict(reversed(list(sealed.items())))) == first


def test_a_pinned_member_spells_its_owner_context_with_a_hyphen() -> None:
    """`evidence-review`, HYPHEN — and this is pinned directly, on purpose.

    `urn:datacompany:identity-ref:1` constrains `owner_context` to
    `^[a-z][a-z0-9-]*$`. The first draft of `pin` wrote `evidence_review`, and
    what makes that worth a dedicated test is not the typo but the ERROR it
    produced: in JSON Schema 2020-12 a failing `allOf` branch contributes no
    annotations, so `unevaluatedProperties: false` then rejected every sibling
    field as unevaluated. The report named eleven innocent properties before the
    guilty one, and reads as though the member's whole shape were wrong.

    Validating the member is therefore not enough on its own — a future
    underscore would fail, but it would fail with a diagnosis pointing anywhere
    except at `owner_context`. The equality assertion below is the one that says
    what actually broke. Worth remembering when reading ANY 2020-12
    unevaluated-properties error: the first name in the list is rarely the
    cause.
    """
    member = _member(owner_context=None)

    assert member["owner_context"] == "evidence-review"
    assert OWNER_CONTEXT == "evidence-review"
    assert "_" not in member["owner_context"]

    assert_valid_instance(member, PINNED_REF_SCHEMA_REF)


def test_the_hand_mirrored_member_exclusions_match_the_released_profile() -> None:
    """`_MEMBER_HASH_EXCLUDED` is a hand-written copy. This is what checks it.

    `member_manifest_hash` hashes a BARE array of members, not a whole manifest,
    so it cannot call `compute_content_hash` — that function needs a document to
    derive a profile from. The exclusion set is therefore spelled by hand in
    `domain/snapshot_manifest`, and a hand-spelled mirror of a released
    annotation is drift waiting to happen: the day a member field gains an
    `x-datacompany-hash-excluded` annotation upstream, `member_manifest_hash` would
    keep including it and this service's digest would silently stop agreeing
    with every other implementation of the same contract.

    Derived from the profile rather than restated: this reads the released
    document's OWN annotations, so it fails when they move, which is the only
    moment the copy is wrong.

    Measured at the time of writing: the manifest's excluded pointers are
    `/content_hash` (own hash), `/content_hash_signatures`, and
    `<pin>/resolved_at` under each of seven pin-shaped fields. Only the
    `/members/-` one is this function's business — the rest sit on the whole
    manifest, where `compute_content_hash` handles them.
    """
    from research_contracts.canonical.engine import derive_hash_profile

    profile = derive_hash_profile(MANIFEST_SCHEMA_REF)
    member_excluded = {
        entry["pointer"].removeprefix("/members/-/")
        for entry in profile["excluded_pointers"]
        if entry["pointer"].startswith("/members/-/")
    }
    assert member_excluded == set(_MEMBER_HASH_EXCLUDED), (
        "the released profile and the hand-written mirror disagree about which member fields leave the hash view"
    )


def test_the_members_array_is_declared_a_set_with_the_order_key_we_sort_by() -> None:
    """`order_members` must sort by the key the RELEASED document declares.

    Sorting by anything else produces a `member_manifest_hash` no other
    implementation can reproduce — and it would look completely correct, because
    this service would recompute its own digest and agree with itself forever.
    `16-…:§3.5`: "RFC 8785 preserves array order; it does not canonicalize a
    mathematical set."
    """
    from research_contracts.canonical.engine import derive_hash_profile

    profile = derive_hash_profile(MANIFEST_SCHEMA_REF)
    members = next(a for a in profile["arrays"] if a["pointer"] == "/members")
    assert members["kind"] == "set"
    assert members["order_key"] == ["/owner_context", "/entity_type", "/entity_id", "/version_id"]
