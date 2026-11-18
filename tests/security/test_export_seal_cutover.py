"""The seal EXECUTES now — `ER-009`, `DM-AT-28`, `DM-AT-29`, `OUT-001`.

`test_export_seal_honesty.py` made the seal mode observable and pinned what it
observed: `canonical_sha256`, on every export, forever. This file is the other
half — the cutover that makes the observation report the strong mode — and it
exists as a separate file because it pins the opposite state of the same system
and the two must not be edited into each other by accident.

## What actually changed, and why it was invisible for so long

`EXPORT_SCHEMA_REF` was
`https://contracts.nonagentic.ai/evidence/inclusion-set-export.v1.schema.json`.
The obvious reading is "the schema has not been published yet", and that reading
is wrong in a way that matters: **the URL could never have resolved, whatever was
published.** `derive_hash_profile` validates a manifest whose own `schema_ref`
member is the released stable-identity grammar
`^urn:datacompany:[a-z][a-z0-9-]*:[1-9][0-9]*$`, so the ref was refused on its SHAPE
before any registry lookup happened at all. Publishing the document under that
URL would have changed nothing. `test_the_https_ref_failed_on_shape_not_absence`
measures exactly that, because it is the reason nobody found this by looking at
the error: the failure was identical to the failure of a schema that simply did
not exist yet, and the old `except Exception` erased even that.

Two things had to happen together, and neither works alone: `research-contracts`
had to release `urn:datacompany:inclusion-set-export:1` (0.2.0 does — hence the
`>=0.2.0` floor), and the constant had to become a URN.

## The risk this file is really about

Rows sealed before today recorded `canonical_sha256`, and carry the OLD URL in
their own `schema_ref` column. A verifier that recomputed in TODAY's mode would
try `compute_content_hash` against a ref that cannot resolve, fail, and report
those rows as unverifiable — or worse, in an earlier shape of the code, as
`manifest_matches: false`, which is the verifier's word for tampered. The
cutover would have read as a fleet-wide forgery of the review record.

So the old rows are reconstructed here as they genuinely were — sealed through
the real code path with the real unresolvable ref — inserted, and verified after
the cutover. That is the single most likely way this change breaks production,
and it is checked end to end rather than argued about.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-cutover")
LEAD = "cutover-lead"
REVIEWER = "cutover-reviewer"

#: The ref this branch cuts over TO. Spelled out rather than imported wherever a
#: test asserts the digest, so the assertion is independent of the constant it is
#: checking — importing `EXPORT_SCHEMA_REF` and hashing with it would agree with
#: the code by construction no matter what the code said.
#:
#: `:1` -> `:2` at the SECOND cutover, which sealed the item arrays. This file
#: is about a cutover, not about one particular version, so it moves with the
#: constant — and the same three tests that caught the first cutover being
#: claimed rather than executed are what caught the second, by going red the
#: moment the constant moved and the expectation did not.
EXPECTED_SCHEMA_REF = "urn:datacompany:inclusion-set-export:2"

#: What every export written before the item seal declares. The historical rows
#: this file reconstructs are `:1` rows, and they must keep verifying.
PREVIOUS_SCHEMA_REF = "urn:datacompany:inclusion-set-export:1"

#: What every row written before this branch carries in its `schema_ref` column.
LEGACY_SCHEMA_REF = "https://contracts.nonagentic.ai/evidence/inclusion-set-export.v1.schema.json"


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
def sealed(client):  # noqa: ANN001, ANN201
    """One review walked all the way to a real sealed export, through the API.

    Not a hand-built manifest. The whole question is whether the manifest the
    service actually emits is one the released schema ACCEPTS — a fabricated
    instance that validates proves nothing about the export path, and the
    failure mode of the cutover is precisely that a real export gets refused
    where the old code would have quietly degraded.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "cutover", "external_key": f"cutover-{uuid.uuid4()}", "title": "Seal cutover"},
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
        content=json.dumps(
            {
                "records": [
                    {"title": "Included paper", "year": 2021},
                    {"title": "Excluded paper", "year": 2019},
                ]
            }
        ),
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
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(REVIEWER),
    ).json()
    # One include and one exclude WITH a reason code, so `counts.exclusion_reasons`
    # is a NON-EMPTY object. That subtree is the single `x-datacompany-hash-opaque`
    # node in the schema, and an empty one would exercise none of it: without the
    # opacity declaration the released engine raises `UndeclaredPathError` the
    # moment a reason code is present, so an all-include export would validate
    # while every real export failed.
    for index, item in enumerate(queue["items"]):
        body: dict[str, Any] = {"stage_id": stage_id, "review_record_id": item["review_record_id"]}
        if index == 0:
            body["disposition"] = "include"
        else:
            body["disposition"] = "exclude"
            body["exclusion_reason_code"] = "wrong_design"
        client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(REVIEWER))

    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers(LEAD, "lead"))
    assert created.status_code == 201, created.text
    return {"review_id": review_id, "export": created.json()}


def _engine():  # noqa: ANN202
    from sqlalchemy import create_engine

    return create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})


def _stored_row(export_id: str) -> dict[str, Any]:
    from sqlalchemy import text

    with _engine().begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        return dict(
            conn.execute(
                text(
                    "SELECT review_id, tenant_id, flow_revision_id, corpus_version_id, schema_ref, "
                    "manifest_hash, sealed_with, payload_digest, payload, omissions, created_by_subject_id "
                    "FROM inclusion_exports WHERE export_id = :e"
                ),
                {"e": export_id},
            )
            .mappings()
            .one()
        )


def _insert_pre_cutover_row(client, model_export_id: str, *, sealed_with: str) -> tuple[str, str]:  # noqa: ANN001
    """Reconstruct a row as the PRE-cutover code would genuinely have written it.

    Returns `(review_id, export_id)`. The row lands in its OWN empty review,
    because `0012` added `uq_export_manifest_per_review` — UNIQUE on
    `(review_id, manifest_hash)` — to stop `create_export`'s `SELECT`-then-`INSERT`
    minting two permanent, undeletable seals for one manifest. Both calls below
    reconstruct the SAME legacy manifest and therefore the same `manifest_hash`,
    differing only in the `sealed_with` label under test, so they would now
    collide with each other in a shared review. Nothing asserted here depends on
    the row sharing a review with its model: `:verify` re-hashes the stored
    payload and reads no other table.

    Not a row with a field poked to a different value. The manifest is put back
    to carrying the legacy URL in its own sealed `schema_ref` member, and the
    hash is then produced by calling the REAL `_seal_manifest` with the REAL
    legacy ref — which still does not resolve, so it still returns the bare
    canonical hash, exactly as it did on every export ever taken. Asserting the
    mode it hands back is the guarantee that this fixture is reproducing the old
    behaviour rather than asserting it.

    `inclusion_exports` is append-only by trigger, so this is an INSERT of a new
    row rather than an UPDATE of the real one — which is also the more honest
    fixture: the real row stays untouched and both generations exist side by
    side, which is the actual production state after the cutover.
    """
    from sqlalchemy import text

    from application.services.flow import _seal_manifest

    model = _stored_row(model_export_id)
    payload = deepcopy(model["payload"])
    payload["manifest"]["schema_ref"] = LEGACY_SCHEMA_REF

    manifest_hash, produced_mode = _seal_manifest(payload["manifest"], LEGACY_SCHEMA_REF)
    assert produced_mode == "canonical_sha256", (
        f"the legacy ref must still be unresolvable, otherwise this fixture is not a pre-cutover row: {produced_mode}"
    )

    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_digest = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    export_id = str(uuid.uuid4())
    home_review_id = client.post(
        "/api/reviews",
        json={"project_id": "cutover", "external_key": f"legacy-home-{uuid.uuid4()}", "title": "Legacy row home"},
        headers=headers(LEAD, "lead"),
    ).json()["review_id"]

    with _engine().begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(
            text(
                "INSERT INTO inclusion_exports (export_id, review_id, tenant_id, flow_revision_id, "
                "corpus_version_id, schema_ref, manifest_hash, sealed_with, payload_digest, payload, "
                "omissions, created_by_subject_id) VALUES (:export_id, :review_id, :tenant_id, "
                ":flow_revision_id, :corpus_version_id, :schema_ref, :manifest_hash, :sealed_with, "
                ":payload_digest, CAST(:payload AS jsonb), CAST(:omissions AS jsonb), :subject)"
            ),
            {
                "export_id": export_id,
                "review_id": home_review_id,
                "tenant_id": model["tenant_id"],
                "flow_revision_id": model["flow_revision_id"],
                "corpus_version_id": model["corpus_version_id"],
                "schema_ref": LEGACY_SCHEMA_REF,
                "manifest_hash": manifest_hash,
                "sealed_with": sealed_with,
                "payload_digest": payload_digest,
                "payload": json.dumps(payload),
                "omissions": json.dumps(model["omissions"]),
                "subject": model["created_by_subject_id"],
            },
        )
    return home_review_id, export_id


# ---------------------------------------------------------------------------
# 1. The seal runs
# ---------------------------------------------------------------------------


def test_the_https_ref_failed_on_shape_not_on_a_missing_document():
    """The measured reason this went unnoticed — and it is not "not published yet".

    Both refs are probed against the SAME installed wheel, in the same process,
    one line apart. The URN resolves; the URL does not, and the profile the URN
    yields is what proves the wheel is capable of resolving something. So the
    URL's failure cannot be attributed to an absent document: the document is
    right there. It is the ref's shape that is refused, which is why publishing
    under that URL would have fixed nothing and why the error looked forever
    like "the contract is not out yet".
    """
    from application.services.flow import seal_mode

    legacy_mode, legacy_reason = seal_mode(LEGACY_SCHEMA_REF)
    assert legacy_mode == "canonical_sha256"
    assert legacy_reason is not None

    urn_mode, urn_reason = seal_mode(EXPECTED_SCHEMA_REF)
    assert (urn_mode, urn_reason) == ("content_hash", None), (
        f"the released schema must resolve; got {urn_mode} because {urn_reason}"
    )


def test_the_constant_is_the_urn_the_wheel_registers():
    """The constant and the registry agree — checked against the registry itself.

    A hardcoded string equality would pass just as well if the wheel had been
    rolled back, which is the one regression that reintroduces the whole defect.
    """
    from research_contracts.canonical.registry import canonical_schemas

    from application.services.flow import EXPORT_SCHEMA_REF

    assert EXPORT_SCHEMA_REF == EXPECTED_SCHEMA_REF
    assert EXPECTED_SCHEMA_REF in set(canonical_schemas())


def test_a_new_export_is_sealed_by_the_schema(sealed, capsys):  # noqa: ANN001
    """`sealed_with == "content_hash"`, and the digest reproduces independently.

    The mode alone would be satisfied by a code path that recorded the label and
    computed something else, so the digest is recomputed here from the STORED
    manifest with the ref spelled out literally — not imported — and compared to
    what the row carries. That is the assertion that says the strong algorithm
    ran over the exact bytes the row claims.
    """
    from research_contracts.canonical.engine import compute_content_hash

    export = sealed["export"]
    assert export["sealed_with"] == "content_hash", export
    assert export["schema_ref"] == EXPECTED_SCHEMA_REF

    row = _stored_row(export["export_id"])
    assert row["sealed_with"] == "content_hash", "the mode has to survive in the row, not only in the response"
    assert row["schema_ref"] == EXPECTED_SCHEMA_REF

    manifest = row["payload"]["manifest"]
    # Non-empty, because the opaque subtree is the only part of this schema that
    # an all-include export would never exercise.
    assert manifest["counts"]["exclusion_reasons"], manifest["counts"]
    independent = compute_content_hash(deepcopy(manifest), EXPECTED_SCHEMA_REF)
    assert independent == row["manifest_hash"] == export["manifest_hash"]

    with capsys.disabled():
        print(f"\nMEASURED manifest_hash = {independent}\nMEASURED manifest = {json.dumps(manifest, sort_keys=True)}")


def test_the_readiness_probe_reports_the_healthy_mode(client):  # noqa: ANN001
    """`ok (... ); sealed_with=content_hash` — the string an operator reads."""
    response = client.get("/health/ready")
    assert response.status_code == 200, response.text
    seal = response.json()["checks"]["export_seal"]
    assert seal.startswith("ok ("), seal
    assert "sealed_with=content_hash" in seal, seal
    assert "degraded" not in seal, seal


def test_a_regressed_wheel_degrades_the_probe_without_gating_the_pod(client, monkeypatch):  # noqa: ANN001
    """Still not a gate — asserted, now that the healthy path is the live one.

    The non-gating decision was easy to hold while every deployment was degraded;
    it only becomes load-bearing once `degraded` means "somebody rolled the
    contract wheel back". It must still not take the pod out: the export path
    records the weaker mode per row and warns, and refusing to serve would trade
    a weaker seal for no service at all.
    """
    import application.services.flow as flow

    monkeypatch.setattr(flow, "seal_mode", lambda _ref: ("canonical_sha256", "SchemaValidationError: simulated"))
    response = client.get("/health/ready")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    seal = body["checks"]["export_seal"]
    assert seal.startswith("degraded ("), seal
    assert "sealed_with=canonical_sha256" in seal, seal


# ---------------------------------------------------------------------------
# 2. The cutover does not accuse the past
# ---------------------------------------------------------------------------


def test_an_old_canonical_sha256_row_still_verifies_after_the_cutover(client, sealed):  # noqa: ANN001
    """THE migration risk, end to end. An old row is not a tampered row.

    Sealed through the real code path while the ref was unresolvable, then
    verified now that it resolves. `verified` must be `True` and `outcome` must
    be `verified` — not `tampered`, which is an accusation about an evidence
    artefact in a regulated review and triggers an investigation, and not
    `unverifiable`, which this row is not: its mode is recorded and that mode is
    reproducible.

    `schema_validated` stays `False`, which is the honest residue: nothing ever
    validated this manifest, and the cutover does not retroactively make it so.
    """
    home_review_id, old_export_id = _insert_pre_cutover_row(
        client, sealed["export"]["export_id"], sealed_with="canonical_sha256"
    )

    verified = client.get(
        f"/api/reviews/{home_review_id}/exports/{old_export_id}:verify",
        headers=headers(LEAD, "lead"),
    ).json()

    assert verified["verified"] is True, verified
    assert verified["outcome"] == "verified", verified
    assert verified["manifest_matches"] is True
    assert verified["payload_matches"] is True
    assert verified["sealed_with"] == "canonical_sha256"
    assert verified["verified_with"] == "canonical_sha256"
    assert verified["seal_unavailable_reason"] is None
    assert verified["schema_validated"] is False, "the cutover must not retro-claim validation of an old row"


def test_a_pre_column_unknown_row_still_verifies_after_the_cutover(client, sealed):  # noqa: ANN001
    """The backfill rows, which are the ones with no recorded mode at all.

    `0006` wrote `unknown` rather than guessing `canonical_sha256`, so these rows
    are resolved by recomputing. The weak mode is tried first and reproduces the
    stored hash, so the row is attributed to the mode that CLAIMS LESS — and it
    must still come out verified rather than unverifiable, because an operator
    who upgrades the contract wheel and finds their whole export history
    unverifiable has been handed the same alarm as tampering with extra steps.
    """
    home_review_id, old_export_id = _insert_pre_cutover_row(
        client, sealed["export"]["export_id"], sealed_with="unknown"
    )

    verified = client.get(
        f"/api/reviews/{home_review_id}/exports/{old_export_id}:verify",
        headers=headers(LEAD, "lead"),
    ).json()

    assert verified["verified"] is True, verified
    assert verified["outcome"] == "verified", verified
    assert verified["sealed_with"] == "unknown"
    assert verified["verified_with"] == "canonical_sha256"
    assert verified["schema_validated"] is False


def test_the_two_modes_agree_on_this_schema_which_is_why_no_digest_moves(sealed):  # noqa: ANN001
    """Measured, and deliberately NOT relied on.

    The released profile for `urn:datacompany:inclusion-set-export:1` excludes no
    pointers, declares no arrays and hashes the one opaque subtree exactly as it
    arrives — so its hash view IS the whole document and `compute_content_hash`
    and `canonical_sha256` produce the SAME bytes for a valid manifest. That is
    measured here rather than assumed, because it is the reason no stored digest
    changes value across this cutover and every historical `manifest_hash` stays
    literally correct.

    It is also exactly why the `sealed_with` dispatch must not be deleted as
    redundant. The agreement is a property of THE schema version in force: a
    version that excluded one pointer, or an array declaration arriving with a
    new member, would break it instantly and silently, and the only thing
    standing between that and a fleet of exports reporting themselves tampered
    with is that verification reads the mode off the row instead of inferring it.

    That warning has now been tested rather than merely written down. `:2` DOES
    exist, and this assertion runs against it: it adds one plain declared string
    (`items_hash`), excludes nothing and declares no array, so the hash view is
    still the whole document and the agreement survives. A later version that
    breaks it will fail here, which is what this line is for. Note the
    equality does NOT hold for a rejected instance — `canonical_sha256` still
    returns a digest where `compute_content_hash` refuses — which is the
    difference the old blanket `except Exception` erased.
    """
    from research_contracts.canonical.engine import canonical_sha256, compute_content_hash

    manifest = _stored_row(sealed["export"]["export_id"])["payload"]["manifest"]
    assert compute_content_hash(deepcopy(manifest), EXPECTED_SCHEMA_REF) == canonical_sha256(deepcopy(manifest))


# ---------------------------------------------------------------------------
# 3. The failure that used to be swallowed
# ---------------------------------------------------------------------------


def test_a_manifest_the_schema_rejects_now_raises_instead_of_being_sealed(sealed):  # noqa: ANN001
    """The whole point of the cutover, and the thing that was unreachable before.

    Before, `EXPORT_SCHEMA_REF` never resolved, so `_seal_manifest` never got as
    far as structural validation and NO malformed manifest could ever have been
    refused — the `except Exception` had nothing to swallow because the strong
    path was never entered. Now it is, so a manifest the contract does not accept
    must stop the export rather than be signed.

    Two rejections are checked because they are different mistakes: a member the
    sealed envelope must not carry (`additionalProperties: false`, which is what
    makes a twelfth count or a stray field detectable) and a required member
    removed. Each is derived from the REAL manifest so the only difference from a
    passing instance is the defect itself.

    The bare hash is computed alongside, to show it was available the whole time:
    the old code would have returned that number, recorded a clean seal and
    reported the export verified.
    """
    from research_contracts.canonical.engine import canonical_sha256
    from research_contracts.canonical.errors import SchemaValidationError

    from application.services.flow import EXPORT_SCHEMA_REF, _seal_manifest

    good = _stored_row(sealed["export"]["export_id"])["payload"]["manifest"]
    # Control: unmodified, it seals strongly. Without this the rejections below
    # would also pass if the strong path were simply broken for everything.
    assert _seal_manifest(deepcopy(good), EXPORT_SCHEMA_REF)[1] == "content_hash"

    with_extra = deepcopy(good) | {"smuggled_member": "not in the contract"}
    missing_required = deepcopy(good)
    del missing_required["member_manifest_hash"]

    for bad in (with_extra, missing_required):
        # The fallback the old code would have taken is right here and works.
        assert canonical_sha256(deepcopy(bad)).startswith("sha256:")
        with pytest.raises(SchemaValidationError):
            _seal_manifest(deepcopy(bad), EXPORT_SCHEMA_REF)


def test_a_rejected_manifest_is_refused_even_where_the_counts_are_smuggled(sealed):  # noqa: ANN001
    """A twelfth tally inside `counts` — the case the schema comment calls out.

    `flow_counts` is sealed with `additionalProperties: false` precisely because
    the conservation identities the producer checks are arithmetic over exactly
    those eleven members plus the reason breakdown. An extra tally arriving
    unnoticed would make the sealed block assert something no identity was ever
    checked against, and it is nested one level down from the members above, so
    it is worth pinning that the refusal reaches inside the sub-object.
    """
    from research_contracts.canonical.errors import SchemaValidationError

    from application.services.flow import EXPORT_SCHEMA_REF, _seal_manifest

    smuggled = deepcopy(_stored_row(sealed["export"]["export_id"])["payload"]["manifest"])
    smuggled["counts"]["retracted"] = 3

    with pytest.raises(SchemaValidationError):
        _seal_manifest(smuggled, EXPORT_SCHEMA_REF)
