"""The seal has to say what it actually is — `ER-009`, `DM-AT-28`, `DM-AT-29`.

`_manifest_hash` preferred `compute_content_hash(instance, schema_ref)` and fell
back to a bare `canonical_sha256` inside `except Exception`. Its docstring said
the difference "is visible in the returned `sealed_with` field rather than being
silent"; `grep -rn sealed_with src/` matched that sentence and nothing else. And
`EXPORT_SCHEMA_REF` was an `https://` URL, which the contract registry cannot key
on at all, so the fallback fired on **every export** while the docstring, the
`schema_ref` column and the verifier all implied a schema had been involved.

`verify_export` recomputed through the same fallback, so two bare hashes agreed
and it answered `manifest_matches: true` — a verifier structurally unable to
report that nothing had ever been validated.

## The ref resolves now, and that changed the observation, not the property

`EXPORT_SCHEMA_REF` is `urn:datacompany:inclusion-set-export:2`,
`research-contracts>=0.3.0` releases it, and a new export seals with
`content_hash`. (It was `:1` / `>=0.2.0` until the item seal landed; this file
names the ref only to say the strong path runs, and both versions run it.)
`test_export_seal_cutover.py` pins that cutover end to end. What
this file keeps are the properties that outlive it, with the expected VALUES
flipped where the state they observe moved:

1. an export records the mode it was sealed in — on every read surface and in
   the row, not only in the create response;
2. the DEGRADED branch is still reachable and still loud. It is exercised here
   through the legacy `https://` ref, which is not merely unpublished but
   permanently unresolvable: `derive_hash_profile` refuses it on SHAPE against
   the released stable-identity grammar. An older wheel or a future schema
   rename puts a live deployment back on that branch, which is the reason
   `sealed_with` exists at all;
3. an instance the schema REJECTS raises instead of degrading — a deployment
   fault and a malformed document are different failures with different remedies;
4. verification happens in the mode the ROW was sealed in, never in today's, so
   the day the schema was published every pre-existing export did not start
   reporting itself as tampered with.

Two tests that lived here were deleted rather than flipped, because
`test_export_seal_cutover.py` asserts strictly more about the same thing: the
post-cutover state of a new export (`test_a_new_export_is_sealed_by_the_schema`,
which also reproduces the digest independently of the constant) and both halves
of the readiness probe (`test_the_readiness_probe_reports_the_healthy_mode` and
`test_a_regressed_wheel_degrades_the_probe_without_gating_the_pod`).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-seal")
LEAD = "seal-lead"
REVIEWER = "seal-reviewer"

#: A released schema unrelated to the export envelope, used as the control for
#: the mode-dispatch tests below. Deliberately NOT `EXPORT_SCHEMA_REF`: those
#: tests are about `_recompute_manifest_hash` choosing a mode, and a ref whose
#: instance is four small members keeps the fixture independent of whatever the
#: export manifest happens to look like this week.
RESOLVABLE_SCHEMA_REF = "urn:datacompany:identity-ref:1"

#: The ref every export carried before the cutover. It is not "not published
#: yet" — `derive_hash_profile` refuses it on shape against the stable-identity
#: grammar `^urn:datacompany:[a-z][a-z0-9-]*:[1-9][0-9]*$`, so it cannot resolve
#: whatever is released. That makes it the one dependable way to reach the
#: degraded branch from a test without monkeypatching the code under test.
LEGACY_SCHEMA_REF = "https://contracts.nonagentic.ai/evidence/inclusion-set-export.v1.schema.json"
VALID_INSTANCE = {
    "owner_context": "evidence-review",
    "entity_type": "inclusion_export",
    "entity_id": "0f7f2c9e-2a52-4a2f-9d1f-1a1b2c3d4e5f",
    "resource_scope": "global",
}


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
        json={"project_id": "seal", "external_key": f"seal-{uuid.uuid4()}", "title": "Seal honesty"},
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
        content=json.dumps({"records": [{"title": "Sealed paper", "year": 2021}]}),
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
    for item in queue["items"]:
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER),
        )

    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers(LEAD, "lead"))
    assert created.status_code == 201, created.text
    return {"review_id": review_id, "export": created.json()}


def test_the_mode_survives_a_read_and_a_list(client, sealed):
    """Every surface that shows a `schema_ref` shows how it was sealed.

    `schema_ref` alone cannot tell a consumer which generation of row they are
    holding: rows written before the cutover declare a ref no schema ever checked
    them against, and rows written after declare one that did. The distinction
    only exists in `sealed_with`, so the field is not optional on any route that
    hands out a `schema_ref` — and the create response is the one surface an
    auditor re-reading the record years later will never see.
    """
    review_id = sealed["review_id"]
    export_id = sealed["export"]["export_id"]
    body = client.get(f"/api/reviews/{review_id}/exports/{export_id}", headers=headers(LEAD, "lead")).json()
    assert body["sealed_with"] == "content_hash"
    listing = client.get(f"/api/reviews/{review_id}/exports", headers=headers(LEAD, "lead")).json()
    assert all(item["sealed_with"] == "content_hash" for item in listing["items"])


def test_the_stored_row_carries_the_mode_not_just_the_response(sealed):
    """Persisted, because a response field cannot be re-read years later.

    The seal is evidence. "How was this hashed" has to be answerable from the
    row alone, long after the wheel that produced it has moved on — which is the
    same reason `schema_ref` is a column rather than a constant read at render
    time.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        stored = conn.execute(
            text("SELECT sealed_with FROM inclusion_exports WHERE export_id = :e"),
            {"e": sealed["export"]["export_id"]},
        ).scalar_one()
    assert stored == "content_hash"


def test_a_degraded_seal_is_logged_once_with_the_schema_ref(client, caplog):
    """WARNING, naming the ref, on the write path only.

    There is no other surface on which an operator would ever discover that the
    seal is weaker than the contract implies: the export succeeds, the response
    is well-formed, and the verifier used to agree with itself. A log line is the
    only thing that reaches somebody who is not already suspicious.

    Exercised through `LEGACY_SCHEMA_REF` rather than `EXPORT_SCHEMA_REF`, which
    resolves now — passing today's ref would take the strong path and, for this
    stub instance, raise `SchemaValidationError` on 23 missing members, testing
    rejection instead of degradation. The legacy ref reaches the branch for the
    real reason a deployment would: a ref this wheel cannot key on. It is the
    only remaining route to the warning that does not monkeypatch the function
    that is supposed to emit it.

    "Exactly once" is the assertion that matters as much as the content. A
    fallback that warned per manifest member, or twice per export, is one an
    operator filters out; one that warned zero times is what this whole branch
    exists to end.
    """
    import logging

    from application.services.flow import _seal_manifest

    with caplog.at_level(logging.WARNING, logger="application.services.flow"):
        _, mode = _seal_manifest({"counts": {"included": 1}}, LEGACY_SCHEMA_REF)

    assert mode == "canonical_sha256", "the legacy ref must still be unresolvable for this test to mean anything"

    degraded = [
        r for r in caplog.records if r.levelno == logging.WARNING and "WITHOUT schema validation" in r.getMessage()
    ]
    assert len(degraded) == 1, f"expected exactly one warning, got {[r.getMessage() for r in caplog.records]}"
    rendered = degraded[0].getMessage()
    assert LEGACY_SCHEMA_REF in rendered, "the warning must name the ref that failed to resolve"
    assert "canonical_sha256" in rendered


def test_an_instance_the_schema_rejects_raises_instead_of_degrading():
    """The failure the blanket `except Exception` swallowed.

    An unresolvable ref is a fact about the deployment and nothing is wrong with
    the export; a rejected instance means the document we are about to seal is
    malformed, and signing it anyway puts our name on something we already know
    is wrong. The old code answered both with a bare hash, so the second one
    became invisible — the export was created, sealed and reported verified.

    Measured on a ref that DOES resolve, because that is the only configuration
    in which the two failures are distinguishable at all.
    """
    from research_contracts.canonical.errors import SchemaValidationError

    from application.services.flow import _seal_manifest

    with pytest.raises(SchemaValidationError):
        _seal_manifest({"this": "is not an identity ref"}, RESOLVABLE_SCHEMA_REF)

    # Positive control: the same ref, a conforming instance, and the strong seal
    # is reported. Without this the test above would also pass if the strong path
    # were simply broken.
    digest, mode = _seal_manifest(dict(VALID_INSTANCE), RESOLVABLE_SCHEMA_REF)
    assert mode == "content_hash"
    assert digest.startswith("sha256:")


def test_verification_claims_schema_validation_only_when_both_ends_were_strong(client, sealed):
    """`schema_validated: true`, and what it costs to say it — `DM-AT-29`.

    The verifier recomputed through the same silent fallback the writer used, so
    it could never disagree with itself about the mode and reported a clean round
    trip for an envelope no schema had ever seen. `schema_validated` is the field
    that answers the question `manifest_matches` was being read as, and it is a
    conjunction of three things, not a rename of `manifest_matches`: the hash
    reproduced, the ROW says it was sealed strongly, and THIS RUN was able to
    check it strongly. A row sealed weakly, or one whose ref no longer resolves,
    fails it on a different clause each.

    This is the positive control for that field. `test_export_seal_cutover.py`
    asserts `schema_validated is False` on reconstructed pre-cutover rows and
    `test_export_verification_outcomes.py` asserts it on an unverifiable clone —
    both of which would pass just as well if the field were hardwired to `False`.
    Nothing else in the suite makes it say `true`, so nothing else would notice
    if the strong branch stopped being reachable through `:verify`.
    """
    verified = client.get(
        f"/api/reviews/{sealed['review_id']}/exports/{sealed['export']['export_id']}:verify",
        headers=headers(LEAD, "lead"),
    ).json()

    assert verified["outcome"] == "verified", verified
    assert verified["verified"] is True, verified
    assert verified["manifest_verifiable"] is True
    assert verified["manifest_matches"] is True
    assert verified["seal_unavailable_reason"] is None
    assert verified["sealed_with"] == "content_hash"
    assert verified["verified_with"] == "content_hash"
    assert verified["schema_validated"] is True, "a strongly sealed row verified strongly IS schema-validated"


def test_verification_uses_the_mode_the_row_was_sealed_in_not_todays():
    """The day the schema was published must not have read as a fleet-wide forgery.

    `compute_content_hash` and `canonical_sha256` produce different digests for
    the same object. A verifier that always used the currently-available mode
    would, the moment `EXPORT_SCHEMA_REF` started resolving — which it now does —
    recompute every previously-sealed row the strong way, get a different answer
    and report
    `manifest_matches: false` — the verifier's word for "tampered with". A
    contract release would look like mass tampering with no way to tell it apart
    from the real thing.

    So the mode comes off the row. Asserted on a ref that resolves, with a row
    that claims the weak seal: the strong path is available and must still not be
    taken.
    """
    from research_contracts.canonical.engine import canonical_sha256

    from application.services.flow import _recompute_manifest_hash

    expected = canonical_sha256(VALID_INSTANCE)
    recomputed, mode, unavailable = _recompute_manifest_hash(
        dict(VALID_INSTANCE), RESOLVABLE_SCHEMA_REF, sealed_with="canonical_sha256", expected=expected
    )
    assert recomputed == expected, "a row sealed weakly must be verified weakly even where the strong seal works"
    assert mode == "canonical_sha256"
    assert unavailable is None


def test_a_row_sealed_strongly_reports_unavailable_rather_than_falling_back():
    """The mirror image, and the one that must never silently succeed.

    A row that claims `content_hash` against a ref that no longer resolves cannot
    be checked. Answering with a bare hash would compare two different things and
    report a mismatch as tampering; answering `manifest_matches: true` from a
    fallback would be worse. It reports that it could not verify, and says why.

    Measured on `LEGACY_SCHEMA_REF`. This used to pass `EXPORT_SCHEMA_REF` and
    still passed after the cutover, but for the wrong reason: today's ref
    resolves, so the stub manifest below reached structural validation and was
    REJECTED — an instance fault, which is the other failure entirely. The test
    would have kept reporting green while no longer touching the branch it names.
    """
    from application.services.flow import _recompute_manifest_hash

    recomputed, mode, unavailable = _recompute_manifest_hash(
        {"counts": {"included": 1}}, LEGACY_SCHEMA_REF, sealed_with="content_hash", expected="sha256:" + "0" * 64
    )
    assert recomputed is None
    assert mode is None
    assert unavailable and "SchemaValidationError" in unavailable


def test_an_unknown_backfilled_row_is_resolved_by_recomputing():
    """`unknown` is the honest backfill, and it is not a dead end.

    Rows written before `sealed_with` existed never recorded their mode. Stamping
    `canonical_sha256` on them would have been an inference about which wheel was
    installed at the time, presented as a record — indistinguishable from a
    measurement on exactly the rows where the distinction matters. So the
    migration says `unknown`, and verification answers the question from the row
    by recomputing both ways and reporting the one that reproduces the hash.
    """
    from research_contracts.canonical.engine import canonical_sha256, compute_content_hash

    from application.services.flow import _recompute_manifest_hash

    weak = canonical_sha256(VALID_INSTANCE)
    recomputed, mode, unavailable = _recompute_manifest_hash(
        dict(VALID_INSTANCE), RESOLVABLE_SCHEMA_REF, sealed_with="unknown", expected=weak
    )
    assert (recomputed, mode, unavailable) == (weak, "canonical_sha256", None)

    # The strong side, on an instance whose hash view is NOT the identity, so
    # the two modes genuinely disagree and the answer is attributable.
    divergent = dict(VALID_INSTANCE) | {"resource_scope": "tenant", "resource_tenant_id": str(uuid.uuid4())}
    strong = compute_content_hash(dict(divergent), RESOLVABLE_SCHEMA_REF)
    if strong != canonical_sha256(divergent):
        recomputed, mode, unavailable = _recompute_manifest_hash(
            dict(divergent), RESOLVABLE_SCHEMA_REF, sealed_with="unknown", expected=strong
        )
        assert (recomputed, mode, unavailable) == (strong, "content_hash", None)

    # Neither reproduces it: nothing is claimed. This is the tamper case for a
    # row whose mode was never recorded, and it must not be softened into a
    # match by trying modes until one is close enough.
    _, mode, unavailable = _recompute_manifest_hash(
        dict(VALID_INSTANCE), RESOLVABLE_SCHEMA_REF, sealed_with="unknown", expected="sha256:" + "0" * 64
    )
    assert mode is None
    assert unavailable
