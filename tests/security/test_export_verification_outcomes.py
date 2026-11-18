"""`:verify` has to tell "could not check" from "did not match" — `DM-AT-29`.

`_recompute_manifest_hash` goes to some length to keep the two apart. Its
docstring is explicit — "a row sealed strongly that can no longer be verified
strongly is NOT a mismatch — the wheel moved, the export did not" — and it
answers `None` rather than falling back, precisely so the caller can tell.

`verify_export` then threw that away on the way out:

    manifest_matches = recomputed_manifest is not None and recomputed_manifest == export.manifest_hash

`None` becomes `False`, and an UNVERIFIABLE row was served the same
`manifest_matches: false` / `verified: false` as a TAMPERED one. Measured side by
side on the pre-fix code, the only fields that differed were `verified_with`,
`recomputed_manifest_hash` and `seal_unavailable_reason` — none of which is what
a consumer branches on:

    unverifiable                       tampered
    "verified_with": null              "verified_with": "canonical_sha256"
    "recomputed_manifest_hash": null   "recomputed_manifest_hash": "sha256:0455…"
    "manifest_matches": false          "manifest_matches": false
    "verified": false                  "verified": false

That is the same over-claim the rest of this branch removes, pointed the other
way: asserting evidence of forgery where the honest answer is "this deployment
cannot tell". The two are not interchangeable in consequence either. "Tampered"
is an accusation about an evidence artefact in a regulated review and the remedy
is an investigation; "unverifiable" is a fact about the installed contract wheel
and the remedy is to install the right one.

This file is separate from `test_export_seal_honesty.py` on purpose: that one is
about what the WRITE path claims when it seals, this one is about what the READ
path claims when it checks. It asserts on the HTTP body, because the response is
the only thing a consumer sees — the pre-existing tests measure
`_recompute_manifest_hash` directly, which is exactly the layer that was already
correct.

## Why the rows are inserted rather than driven through the API

The three outcomes cannot all be produced by the API: `create_export` seals in
whatever mode the installed wheel supports and is not able to write a row that
disagrees with its own payload, which is the whole point of the seal. So the
unverifiable and tampered rows are cloned from a genuinely sealed export with one
field changed each — the same construction an auditor would use, and the minimum
that makes the two distinguishable at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
OWNER_DB_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-verify")
LEAD = "verify-lead"
REVIEWER = "verify-reviewer"

#: Well-formed under the registry's stable-identity grammar and deliberately not
#: published. A row claiming `content_hash` against it is the "sealed strongly,
#: cannot be checked strongly today" case — a wheel that moved, not a forgery.
UNRESOLVABLE_SCHEMA_REF = "urn:datacompany:not-a-published-export-schema:1"

#: Valid shape (`ck_export_manifest_hash_shape`), wrong value. A row whose stored
#: manifest hash no longer matches the manifest it is supposed to seal.
WRONG_MANIFEST_HASH = "sha256:" + "0455b2e1" * 8


def headers(subject: str, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"
    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def sealed(client):  # noqa: ANN201, ANN001
    """One review walked all the way to a real sealed export."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "verify", "external_key": f"verify-{uuid.uuid4()}", "title": "Verification outcomes"},
        headers=headers(LEAD, "lead"),
    ).json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(LEAD, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(LEAD, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(LEAD, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A verifiable paper", "year": 2021}]}),
        headers={**headers(LEAD, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(LEAD, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(LEAD, "lead")).json()["items"][0][
        "stage_id"
    ]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(LEAD, "lead"),
    )
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue", params={"stage_id": stage_id}, headers=headers(REVIEWER)
    ).json()
    for item in queue["items"]:
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER),
        )

    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers(LEAD, "lead"))
    assert created.status_code == 201, created.text
    return {"review_id": review_id, "export_id": created.json()["export_id"]}


def _a_home_for_the_clone(client) -> str:  # noqa: ANN001
    """A bare review in the same tenant, created to hold ONE cloned export row.

    `0012` added `uq_export_manifest_per_review` — UNIQUE on
    `(review_id, manifest_hash)` — because `create_export` was a
    `SELECT`-then-`INSERT` with nothing underneath it, so two concurrent seals
    minted two permanent, byte-identical, undeletable inclusion sets. The clones
    below copy a genuine export's `manifest_hash` verbatim, which is exactly the
    state that constraint now forbids, so they cannot sit beside their source any
    more.

    Each clone therefore gets its own empty review. Nothing in this file's
    assertions depends on the clone sharing a review with its source: `:verify`
    re-hashes the STORED payload and reads no other table, and the route scopes by
    the review it is called on. The clone's `flow_revision_id` and
    `corpus_version_id` still point at the source review's rows, which the foreign
    keys permit and the verifier never dereferences — it is a deliberately
    incoherent row, which is what a tampered row is.

    A fresh review PER CLONE, not one shared home: two clones that both keep the
    source's `manifest_hash` would collide with each other in a shared one, and
    that collision would be a fixture artefact rather than a finding.
    """
    return client.post(
        "/api/reviews",
        json={"project_id": "verify", "external_key": f"clone-home-{uuid.uuid4()}", "title": "Clone home"},
        headers=headers(LEAD, "lead"),
    ).json()["review_id"]


def _clone_export(
    client,  # noqa: ANN001
    source_export_id: str,
    *,
    sealed_with: str,
    schema_ref: str | None,
    manifest_hash: str | None,
) -> tuple[str, str]:
    """Copy a sealed row into its own review, changing one thing about how it is sealed.

    Returns `(review_id, export_id)` — the review is a fresh empty one, see
    `_a_home_for_the_clone`.

    `INSERT ... SELECT` rather than a hand-built row: the payload, the payload
    digest and all three foreign keys come across verbatim, so the clone differs
    from a genuine export in precisely the field under test and nothing else.
    `inclusion_exports` is append-only, so an `UPDATE` is refused — correctly —
    and a new row is the only way to construct these states at all.
    """
    new_id = str(uuid.uuid4())
    home = _a_home_for_the_clone(client)
    engine = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(
                text(
                    """
                    INSERT INTO inclusion_exports (
                        export_id, review_id, tenant_id, flow_revision_id, corpus_version_id,
                        schema_ref, manifest_hash, sealed_with, payload_digest, payload,
                        omissions, created_by_subject_id
                    )
                    SELECT :new_id, CAST(:home AS uuid), tenant_id, flow_revision_id, corpus_version_id,
                           COALESCE(:schema_ref, schema_ref),
                           COALESCE(:manifest_hash, manifest_hash),
                           :sealed_with, payload_digest, payload,
                           omissions, created_by_subject_id
                      FROM inclusion_exports
                     WHERE export_id = :src
                    """
                ),
                {
                    "new_id": new_id,
                    "home": home,
                    "src": source_export_id,
                    "schema_ref": schema_ref,
                    "manifest_hash": manifest_hash,
                    "sealed_with": sealed_with,
                },
            )
    finally:
        engine.dispose()
    return home, new_id


def _clone_export_with_items(client, source_export_id: str, mutate: Callable[[dict], None]) -> tuple[str, str]:  # noqa: ANN001, E501
    """Copy a sealed row into its own review, edit its ITEM ARRAYS, re-seal `payload_digest`.

    Returns `(review_id, export_id)` — see `_a_home_for_the_clone` for why the
    clone no longer sits beside its source.

    This is the tamper the manifest seal does not catch, performed the way
    someone with database access would perform it rather than the way a broken
    writer would. `payload_digest` is an ordinary sibling column that nothing
    seals, so recomputing it after the edit leaves `payload_matches` true; the
    `manifest` sub-object is left untouched, so `manifest_matches` is true as
    well. Every hash in the row agrees with every other hash in the row, and the
    row is not the document that was sealed.

    The digest is recomputed with the exact serialisation `verify_export` uses —
    `sort_keys=True, separators=(",", ":")` — because a digest computed any other
    way would fail `payload_matches` for an uninteresting reason and the test
    would pass without ever exercising the check under examination.
    """
    new_id = str(uuid.uuid4())
    home = _a_home_for_the_clone(client)
    engine = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            payload = conn.execute(
                text("SELECT payload FROM inclusion_exports WHERE export_id = :src"),
                {"src": source_export_id},
            ).scalar_one()
            mutate(payload)
            digest = (
                "sha256:"
                + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            )
            conn.execute(
                text(
                    """
                    INSERT INTO inclusion_exports (
                        export_id, review_id, tenant_id, flow_revision_id, corpus_version_id,
                        schema_ref, manifest_hash, sealed_with, payload_digest, payload,
                        omissions, created_by_subject_id
                    )
                    SELECT :new_id, CAST(:home AS uuid), tenant_id, flow_revision_id, corpus_version_id,
                           schema_ref, manifest_hash, sealed_with,
                           :payload_digest, CAST(:payload AS jsonb),
                           omissions, created_by_subject_id
                      FROM inclusion_exports
                     WHERE export_id = :src
                    """
                ),
                {
                    "new_id": new_id,
                    "home": home,
                    "src": source_export_id,
                    "payload_digest": digest,
                    "payload": json.dumps(payload),
                },
            )
    finally:
        engine.dispose()
    return home, new_id


def _clone_export_as_a_v1_row(source_export_id: str) -> str:
    """A GENUINE `:1` export, not a `:2` row with its label changed.

    Production has never written one — the table is at zero rows — but every
    export sealed on a branch, in a demo or on a developer's machine before
    today is this shape, and the compatibility claim is about them. Faking it by
    rewriting only `schema_ref` would prove nothing: the manifest would still
    carry `items_hash`, `:1` refuses that member, and the row would come back
    `unverifiable` for a reason that has nothing to do with what is being
    tested.

    So the row is built the way `:1` really built one: the member is REMOVED,
    the manifest declares `:1` as its own `schema_ref`, and `manifest_hash` is
    recomputed against `:1` through the released algorithm. `payload_digest`
    follows the edited bytes. The result is a row this service would have
    written yesterday, and `:verify` has to still accept it.
    """
    from research_contracts.canonical.engine import compute_content_hash

    new_id = str(uuid.uuid4())
    engine = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            payload = conn.execute(
                text("SELECT payload FROM inclusion_exports WHERE export_id = :src"),
                {"src": source_export_id},
            ).scalar_one()
            del payload["manifest"]["items_hash"]
            payload["manifest"]["schema_ref"] = "urn:datacompany:inclusion-set-export:1"
            manifest_hash = compute_content_hash(payload["manifest"], "urn:datacompany:inclusion-set-export:1")
            digest = (
                "sha256:"
                + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            )
            conn.execute(
                text(
                    """
                    INSERT INTO inclusion_exports (
                        export_id, review_id, tenant_id, flow_revision_id, corpus_version_id,
                        schema_ref, manifest_hash, sealed_with, payload_digest, payload,
                        omissions, created_by_subject_id
                    )
                    SELECT :new_id, review_id, tenant_id, flow_revision_id, corpus_version_id,
                           'urn:datacompany:inclusion-set-export:1', :manifest_hash, 'content_hash',
                           :payload_digest, CAST(:payload AS jsonb),
                           omissions, created_by_subject_id
                      FROM inclusion_exports
                     WHERE export_id = :src
                    """
                ),
                {
                    "new_id": new_id,
                    "src": source_export_id,
                    "manifest_hash": manifest_hash,
                    "payload_digest": digest,
                    "payload": json.dumps(payload),
                },
            )
    finally:
        engine.dispose()
    return new_id


def _verify(client, review_id: str, export_id: str) -> dict:  # noqa: ANN001
    response = client.get(f"/api/reviews/{review_id}/exports/{export_id}:verify", headers=headers(LEAD, "lead"))
    assert response.status_code == 200, response.text
    return response.json()


def test_an_intact_export_still_verifies(client, sealed):
    """The positive control, without which every negative below proves nothing.

    A test that only measured failures would also pass if `:verify` had simply
    stopped answering `verified` at all.
    """
    body = _verify(client, sealed["review_id"], sealed["export_id"])
    assert body["outcome"] == "verified", body
    assert body["verified"] is True
    assert body["manifest_verifiable"] is True
    assert body["manifest_matches"] is True
    assert body["payload_matches"] is True
    assert body["seal_unavailable_reason"] is None


def test_a_row_that_cannot_be_checked_is_not_reported_as_a_mismatch(client, sealed):
    """`manifest_matches: false` for an untouched export is an accusation.

    The row claims `content_hash` against a schema this deployment cannot
    resolve. Nothing about the export is wrong — the contract wheel is what
    changed — and `_recompute_manifest_hash` says so by answering `None` rather
    than falling back to a bare hash and comparing two different things.

    Before the fix that `None` was collapsed into `false` and the response was
    indistinguishable from the tampered row below.
    """
    review_id, export_id = _clone_export(
        client,
        sealed["export_id"],
        sealed_with="content_hash",
        schema_ref=UNRESOLVABLE_SCHEMA_REF,
        manifest_hash=None,
    )
    body = _verify(client, review_id, export_id)

    assert body["outcome"] == "unverifiable", body
    # Tri-state. `null` is neither "yes" nor "no", and it is falsy — a consumer
    # testing `if (!verified)` keeps failing closed, a consumer testing
    # `verified === false` stops raising a tamper alarm at a missing schema.
    assert body["verified"] is None
    assert body["manifest_verifiable"] is False
    assert body["manifest_matches"] is None, "an unverifiable manifest must not report as a mismatch"
    assert body["verified_with"] is None
    assert body["seal_unavailable_reason"], "a refusal to claim must say why it could not"
    assert "SchemaValidationError" in body["seal_unavailable_reason"]
    # The document itself is untouched, and the verifier still says so. This is
    # what makes "unverifiable" a narrower statement than "unknown".
    assert body["payload_matches"] is True
    assert body["schema_validated"] is False


def test_a_row_whose_manifest_hash_no_longer_matches_is_still_reported_as_tampered(client, sealed):
    """The other half: honesty about "could not check" must not soften "did not match".

    The seal is checkable here — `canonical_sha256` needs no schema — and it does
    not reproduce. That is the case `verified: false` exists for, and widening
    the unverifiable branch until this fell into it would be the same defect with
    the sign flipped.
    """
    review_id, export_id = _clone_export(
        client,
        sealed["export_id"],
        sealed_with="canonical_sha256",
        schema_ref=None,
        manifest_hash=WRONG_MANIFEST_HASH,
    )
    body = _verify(client, review_id, export_id)

    assert body["outcome"] == "tampered", body
    assert body["verified"] is False
    assert body["manifest_verifiable"] is True
    assert body["manifest_matches"] is False
    assert body["verified_with"] == "canonical_sha256"
    assert body["recomputed_manifest_hash"] and body["recomputed_manifest_hash"] != WRONG_MANIFEST_HASH


def test_the_two_failures_are_distinguishable_in_the_fields_a_consumer_reads(client, sealed):
    """The defect, stated as the property it violated.

    Asserted as a DIFFERENCE between two bodies rather than as two independent
    expectations, because the bug was never that either answer was unreachable —
    it was that both answers were the same one. A future change that quietly
    re-merges them passes every assertion above and fails this.
    """
    unverifiable = _verify(
        client,
        *_clone_export(
            client,
            sealed["export_id"],
            sealed_with="content_hash",
            schema_ref=UNRESOLVABLE_SCHEMA_REF,
            manifest_hash=None,
        ),
    )
    tampered = _verify(
        client,
        *_clone_export(
            client,
            sealed["export_id"],
            sealed_with="canonical_sha256",
            schema_ref=None,
            manifest_hash=WRONG_MANIFEST_HASH,
        ),
    )

    for field in ("outcome", "verified", "manifest_verifiable", "manifest_matches"):
        assert unverifiable[field] != tampered[field], (
            f"{field!r} is {unverifiable[field]!r} for a row that could not be checked and "
            f"{tampered[field]!r} for a row that failed its check; a consumer reading it "
            "cannot tell an unavailable contract from a forged export"
        )


def test_a_study_added_after_sealing_is_caught_by_the_counts_the_manifest_seals(client, sealed):
    """The seal does not cover the items — so the counts inside it have to.

    `manifest_hash` seals the `manifest` sub-object. The included and omitted
    records sit OUTSIDE it, covered only by `payload_digest`, an ordinary sibling
    column no seal reaches. Append a study to `payload["included"]`, recompute
    `payload_digest`, and both of the checks that existed before this test agree:
    the manifest reproduces and the payload digest matches. `:verify` answered
    `verified` for an inclusion set naming a study the review never included.

    What makes it detectable without a contract bump is that the manifest DOES
    carry `included_count` and `omissions_count`, and the manifest IS sealed. So
    the sealed row still contains a true statement about the arrays beside it,
    and cross-checking it turns adding or removing records into a caught tamper
    inside `urn:datacompany:inclusion-set-export:1`.

    Both prior booleans are asserted TRUE here on purpose. If either went false
    the row would be rejected for a reason that has nothing to do with the check
    under test, and this test would pass while proving nothing.
    """

    def add_a_study(payload: dict) -> None:
        payload["included"].append(
            {**payload["included"][0], "title": "A study nobody screened", "review_record_id": str(uuid.uuid4())}
        )

    review_id, export_id = _clone_export_with_items(client, sealed["export_id"], add_a_study)
    body = _verify(client, review_id, export_id)

    assert body["manifest_matches"] is True, "the manifest was not touched; if it fails to reproduce, this test is void"
    assert body["payload_matches"] is True, "the digest was recomputed over the edit; a mismatch here voids the test"
    assert body["counts_match"] is False, body
    assert body["outcome"] == "tampered", body
    assert body["verified"] is False, "an export naming a study the review never included must not verify"


def test_rewriting_a_record_in_place_is_now_caught_by_items_hash(client, sealed):
    """THE INVERSION. This test used to assert `verified is True`, and it was right to.

    Its previous name ended `…_still_verifies_and_that_is_the_gap_items_hash_closes`
    and its docstring said: "when `:2` seals the items, this goes red, and whoever
    makes it red is meant to invert it rather than delete it." That is what
    happened, so this is that inversion and the old expectation is recorded here
    rather than erased — a test whose history is deleted cannot tell a later
    reader that the fix was real.

    THE GAP, as it stood: editing a record WITHOUT changing how many there are
    keeps every count true. `counts_match` passed, `manifest_matches` passed
    (the manifest sub-object was untouched), `payload_matches` passed (the
    digest was recomputed over the edit, and nothing seals that column) — and
    `:verify` answered `verified: true` for an inclusion set naming a study
    nobody screened. A count cannot detect a substitution.

    THE FIX: `items_hash` is inside the SEALED manifest, so rewriting a record
    means either leaving it stale — caught here — or updating it, which moves
    `manifest_hash` and is caught by `manifest_matches`. There is no third move.

    The three assertions below are kept in exactly that order because the first
    two are what make the third mean something: this is still the tamper that
    every OTHER check passes.
    """

    def rewrite_the_title(payload: dict) -> None:
        payload["included"][0]["title"] = "A different paper entirely"

    review_id, export_id = _clone_export_with_items(client, sealed["export_id"], rewrite_the_title)
    body = _verify(client, review_id, export_id)

    assert body["counts_match"] is True, "the record count did not change, so the counts still cannot see this"
    assert body["manifest_matches"] is True, "the manifest sub-object was not touched; if this fails the test is void"
    assert body["payload_matches"] is True, "the digest was recomputed over the edit; if this fails the test is void"

    assert body["items_match"] is False, body
    assert body["recomputed_items_hash"] != body["items_hash"], body
    assert body["outcome"] == "tampered", body
    assert body["verified"] is False, "a substituted record must not verify"


def test_an_untampered_export_reports_the_items_check_passing(client, sealed):
    """The negative control for the inversion above.

    Without it, `items_match is False` would pass just as well against an
    implementation that answers `False` unconditionally — which would report
    every honest export as forged and be a far worse defect than the one being
    fixed. So the same field is asserted `True` on the export the tampered row
    was cloned FROM.
    """
    body = _verify(client, sealed["review_id"], sealed["export_id"])

    assert body["items_match"] is True, body
    assert body["recomputed_items_hash"] == body["items_hash"], body
    assert body["items_hash"] is not None, "a `:2` export with no items_hash is a manifest this service cannot write"
    assert body["outcome"] == "verified", body


def test_an_export_sealed_under_v1_still_verifies_and_says_null_rather_than_false(client, sealed):
    """Backward compatibility, as a THIRD reading rather than a fourth outcome.

    A `:1` export has no `items_hash` because its contract has no such member.
    That is a fact about the contract it was sealed under, not evidence of
    tampering, and the distinction is the same one this whole file exists to
    defend: "could not check" is not "did not match". Answering `False` here
    would accuse every export written before today of forgery — an accusation
    about an evidence artefact in a regulated review, whose remedy is an
    investigation, for a row that is in fact intact.

    So `items_match` is `null` and `outcome` stays `verified`. The row attests
    less than a `:2` row does, and `schema_ref` on the response is the field
    that says so.
    """
    export_id = _clone_export_as_a_v1_row(sealed["export_id"])
    body = _verify(client, sealed["review_id"], export_id)

    assert body["schema_ref"] == "urn:datacompany:inclusion-set-export:1", body
    assert body["items_match"] is None, body
    assert body["items_hash"] is None, "a `:1` manifest carries no such member"
    assert body["recomputed_items_hash"] is None, "nothing was recomputed, so nothing may be reported as recomputed"
    assert body["manifest_matches"] is True, "a `:1` row must still verify against its own contract"
    assert body["outcome"] == "verified", body
    assert body["verified"] is True, body
