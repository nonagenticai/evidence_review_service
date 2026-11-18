"""`ER-007` — a snapshot pins exact versions, reproduces byte-identically, or refuses.

`14-…:362-367` states four completion criteria for evidence snapshots, and this
module is the evidence for them:

* "the snapshot reconstructs byte-identical canonical JSON/declared tabular
  artifacts under the same serializer version" — driven twice through
  `GET …/evidence-snapshots/{id}`, which does not serve a stored blob but
  re-derives the manifest from the member rows on every read
  (`snapshots.reproduce_manifest`), so a member that drifted surfaces as a
  refusal at the moment somebody tries to rely on it;
* "a downstream run cannot bind an unresolved or non-publication snapshot" —
  the refusal arms: a version the review does not own, a member kind this
  service cannot pin, an empty member list, and one entity named twice;
* "snapshot construction/replay across worker restart creates one version" —
  the same request posted twice yields one snapshot, one job and one version,
  because idempotency belongs to the durable job queue rather than to a
  convention in the command.

## Why this is an integration test and is skipped without postgres

Nearly every guarantee here is a server-side one. `ER-M021` gives the snapshot
chain a partial unique index for "one root per review" and "one successor per
node", `ck_evidence_snapshots_gate_ledger_is_honest` is a closed CHECK over the
gate names, and the outbox row in the last test is held to the same transaction
as its cause by `13-…:§5.3`. A SQLite or in-memory double would run this whole
file green while enforcing none of them, so the module skips rather than fakes.

The runtime role `conftest.py` points `DATABASE_URL` at is `evidence_review_app`
— a non-owner — so the refusals below are the ones production takes.

## The gate ledger is asserted as it IS, not as `14-…:345` wishes

Three of the five gates that document names have no subject matter in this
service, and `domain/policies/snapshot_gates.py` records which package owes each
one. So `fully_gated` is asserted **False** here. Writing `is True` would be
asserting a fiction and would have to be un-asserted the day a third gate lands.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: ⚠️ A PER-RUN tenant, not merely a per-module one, and the nonce is the whole
#: point. Nothing in this suite truncates between runs: the rows a run writes are
#: still there for the next one. Every other module can live with that because it
#: asserts on rows it can identify. This one cannot — `version_number == 1`,
#: `supersedes_snapshot_id is None` and "the list returns exactly ONE snapshot"
#: are assertions about the ENTIRE snapshot chain of a review inside a tenant,
#: and a stable label would let a second run see the first run's chain and read
#: version 3 where it asserted 1. The nonce makes each run its own tenant, so the
#: chain those three tests reason about is one this run built end to end.
TENANT = tenant(f"tenant-evidence-snapshot-{uuid.uuid4().hex[:12]}")
LEAD = "lead-carol"
REVIEWER_A = "reviewer-alice"
REVIEWER_B = "reviewer-bob"

#: Every hash this service writes goes through `research_contracts`' RFC 8785
#: canonicaliser. The shape is part of the contract, not an implementation
#: detail — a downstream witness parses it.
HASH_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: `domain/policies/snapshot_gates.py`, spelled independently. Held equal to the
#: service's own answer by the assertions below rather than imported from it:
#: importing would make the test and the producer agree by construction, which is
#: exactly the arrangement that cannot notice an edit to either.
GATES_THAT_RAN = ["uncommitted_protocol", "unresolved_conflicts"]
GATES_THAT_DID_NOT = ["denied_rights", "stale_mandatory_evidence", "unresolved_critical_proposals"]

#: Three clean records: two the reviewers agree to include, one they agree to
#: exclude. Agreement on all three is deliberate — an unadjudicated disagreement
#: would leave a record `uncertain`, and the `unresolved_conflicts` gate would
#: then refuse every snapshot in this file for a reason no test named.
CORPUS = {
    "records": [
        {
            "title": "Tocilizumab in hospitalised patients with COVID-19",
            "doi": "10.1016/S0140-6736(21)00676-0",
            "year": 2021,
            "abstract": "A randomised controlled trial.",
            "journal": "The Lancet",
        },
        {
            "title": "Tocilizumab trial: 6-month follow-up report",
            "doi": "10.1016/S0140-6736(22)00001-1",
            "year": 2022,
            "journal": "The Lancet",
        },
        {
            "title": "An observational cohort of outpatients",
            "doi": "10.1056/NEJMoa2021436",
            "year": 2021,
        },
    ]
}


@pytest.fixture(scope="module")
def app_client():  # noqa: ANN201
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    os.environ["ENABLE_AUTH"] = "false"
    os.environ["ENVIRONMENT"] = "test"

    from app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def headers(subject: str, *, tenant: str = TENANT, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": tenant, "X-Subject-Id": subject, "X-Actor-Role": role}


def lead_headers(tenant: str = TENANT) -> dict[str, str]:
    return headers(LEAD, tenant=tenant, role="lead")


@pytest.fixture(scope="module")
def review(app_client):  # noqa: ANN201, ANN001
    """A locked, activated review with a sealed three-record corpus.

    Module-scoped because every assertion below is about the state THIS run
    produced. A per-test rebuild would be slow and, worse, a different review —
    and `version_number` is per review.
    """
    created = app_client.post(
        "/api/reviews",
        json={
            "project_id": "dogfood",
            "external_key": f"snapshot-{uuid.uuid4()}",
            "title": "A snapshot reproduces or refuses",
        },
        headers=lead_headers(),
    )
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {"population": "hospitalised adults with COVID-19"},
            "exclusion_criteria": {"design": "not a randomised controlled trial"},
            "stage_definitions": [{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2}],
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]

    # The `uncommitted_protocol` gate reads exactly this lock. Without it every
    # POST in this file would be a 422 and the refusal tests would pass for the
    # wrong reason.
    locked = app_client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=lead_headers())
    assert locked.status_code == 200 and locked.json()["locked"] is True, locked.text

    activated = app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "preparing a publication snapshot"},
        headers=lead_headers(),
    )
    assert activated.status_code == 200, activated.text

    imported = app_client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["quarantined_count"] == 0, "this fixture needs three screenable records"

    sealed = app_client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())
    assert sealed.status_code == 201, sealed.text

    stages = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()

    return {
        "review_id": review_id,
        "protocol_version_id": protocol_id,
        "stage_id": stages["items"][0]["stage_id"],
        "corpus_version_id": sealed.json()["corpus_version_id"],
    }


@pytest.fixture(scope="module")
def settled(app_client, review):  # noqa: ANN201, ANN001
    """Both reviewers screen all three records: two agreed `include`, one agreed `exclude`.

    Read back from `/flow` rather than inferred from the decisions posted here,
    because the DERIVED ledger is the only authority `create_research_object`
    consults — asserting on what we SENT would pass against a derivation that
    dropped a record.
    """
    review_id, stage_id = review["review_id"], review["stage_id"]

    assigned = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["assignments_created"] == 6, "3 records x 2 reviewers"

    records = app_client.get(f"/api/reviews/{review_id}/records", headers=lead_headers()).json()["items"]
    assert len(records) == 3, records
    # Chosen by title, not by position: `list_records` orders by `created_at`,
    # and a pinned index would silently re-point the negative control at an
    # included record if that ordering ever changed.
    excluded = next(r for r in records if "observational cohort" in r["title"].lower())
    included = [r for r in records if r["review_record_id"] != excluded["review_record_id"]]

    for reviewer in (REVIEWER_A, REVIEWER_B):
        for record in records:
            body: dict[str, Any] = {
                "stage_id": stage_id,
                "review_record_id": record["review_record_id"],
                "disposition": "include",
            }
            if record["review_record_id"] == excluded["review_record_id"]:
                body["disposition"] = "exclude"
                body["exclusion_reason_code"] = "wrong_population"
            response = app_client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(reviewer))
            assert response.status_code == 201, response.text

    counts = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers()).json()["counts"]
    assert counts["included"] == 2, counts
    assert counts["excluded"] == 1, counts
    assert counts["not_yet_screened"] == 0, counts

    return {"included": included, "excluded": excluded}


def _link(record: dict[str, Any], review: dict[str, Any], role: str) -> dict[str, Any]:
    """One `RecordLink`, pinned to the exact record version and corpus seal."""
    return {
        "review_record_id": record["review_record_id"],
        "review_record_version_id": record["review_record_version_id"],
        "corpus_version_id": review["corpus_version_id"],
        "link_role": role,
    }


@pytest.fixture(scope="module")
def research_object(app_client, review, settled):  # noqa: ANN201, ANN001
    """One study, reported by two included reports."""
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/research-objects",
        json={
            "object_key": "recovery-tocilizumab",
            "content": {"design": "randomised controlled trial", "population": "hospitalised adults"},
            "rationale": "the trial and its follow-up report describe one study",
            "records": [
                _link(settled["included"][0], review, "primary"),
                _link(settled["included"][1], review, "companion"),
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="module")
def extraction(app_client, review, research_object):  # noqa: ANN201, ANN001
    """Two variables, each recorded as an expert assertion with its rationale."""
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/research-objects/{research_object['research_object_id']}/extractions",
        json={
            "dataset_key": "primary-outcomes",
            "rationale": "extracted from table 2",
            "observations": [
                {
                    "variable_key": "n_randomised",
                    "value": 4116,
                    "expert_rationale": "table 1, total randomised",
                },
                {
                    "variable_key": "mortality_28d_pct",
                    "value": 31.0,
                    "unit": "percent",
                    "expert_rationale": "table 2, 28-day mortality in the intervention arm",
                },
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _observation(extraction: dict[str, Any], variable_key: str) -> dict[str, Any]:
    return next(o for o in extraction["observations"] if o["variable_key"] == variable_key)


@pytest.fixture(scope="module")
def acceptance(app_client, review, extraction):  # noqa: ANN201, ANN001
    """The lead accepts one exact observation version — the version the snapshots pin.

    `09-…:329` refuses "unaccepted inputs" from a snapshot. The pinned member
    below is therefore an ACCEPTED one, so that nothing in this file passes
    because the acceptance requirement happened not to be reached.
    """
    observation = _observation(extraction, "n_randomised")
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/acceptances",
        json={
            "subject_kind": "observation_version",
            "subject_version_id": observation["observation_version_id"],
            "decision": "accept",
            "rationale": "checked against the published table",
        },
        headers=lead_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _member(extraction: dict[str, Any], variable_key: str) -> dict[str, Any]:
    """One requested pin: the KIND and the exact version id, and nothing else.

    Deliberately no `content_hash` and no `version_number`. `14-…:353` — the
    operation "never infers mutable 'latest' inputs" — and the mirror of that
    rule is that the caller never supplies the immutable facts either: a caller
    who could pass a hash could record a member that never existed, which is
    worse than recording none because it looks like verification.
    """
    return {
        "entity_type": "observation_version",
        "version_id": _observation(extraction, variable_key)["observation_version_id"],
    }


def _first_body(extraction: dict[str, Any]) -> dict[str, Any]:
    """The exact request the first snapshot is built from, byte for byte.

    A module-level function rather than a literal in one test, because
    `test_the_same_request_twice_is_one_snapshot_and_one_version` has to send an
    IDENTICAL body — the idempotency it proves is over the command's content,
    and a body that differed by a character would prove nothing.
    """
    return {
        "intended_use": "publication",
        "members": [_member(extraction, "n_randomised")],
    }


@pytest.fixture(scope="module")
def first_snapshot(app_client, review, extraction, acceptance):  # noqa: ANN201, ANN001
    """Version 1 of this review's snapshot chain, built once for the whole module.

    A fixture rather than a value carried between tests: several tests below read
    this snapshot and only two of them are allowed to change the chain, so
    building it here makes "what version is this review on" a property of the
    fixture graph instead of a property of pytest's collection order.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json=_first_body(extraction),
        headers=lead_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# ER-007 — what a snapshot pins, and that it reproduces
# ---------------------------------------------------------------------------


def test_a_snapshot_pins_the_exact_versions_it_was_given(first_snapshot, extraction):
    """`14-…:353`. The snapshot names versions, not queries.

    A failure here means the analysis input set is not actually pinned. Either
    the member count drifted from what the caller asked for — so the run would
    read evidence nobody named — or one of the two digests is missing or
    malformed, in which case nothing downstream can verify that the evidence it
    fetched is the evidence that was sealed. `member_ordering_rule` is asserted
    because two producers holding identical evidence must derive identical
    bytes, and they can only do that if they agree on member order; a snapshot
    that recorded a different rule is a snapshot whose hash nobody else can
    reproduce. And `version_number == 1` with `supersedes_snapshot_id is None`
    is the claim that this is the ROOT of the review's chain — if it is not,
    this run is reading a chain some earlier run built, and the tenant nonce at
    the top of this module has stopped working.
    """
    assert first_snapshot["member_count"] == 1, first_snapshot
    assert first_snapshot["member_count"] == len(_first_body(extraction)["members"])
    assert HASH_SHAPE.match(first_snapshot["member_manifest_hash"] or ""), first_snapshot
    assert HASH_SHAPE.match(first_snapshot["content_hash"] or ""), first_snapshot
    assert first_snapshot["member_ordering_rule"] == "pinned_ref_key_asc", first_snapshot
    assert first_snapshot["version_number"] == 1, first_snapshot
    assert first_snapshot["supersedes_snapshot_id"] is None, first_snapshot
    assert first_snapshot["intended_use"] == "publication", first_snapshot
    # The durable job is not decoration: it is where the idempotency proved by
    # `test_the_same_request_twice_…` actually lives.
    assert uuid.UUID(first_snapshot["job_id"]), first_snapshot


def test_the_snapshot_reproduces_byte_identically(app_client, review, first_snapshot):
    """`14-…:364`, criterion one, driven twice through the read that re-derives it.

    `GET` does not hand back a stored blob; `snapshots.reproduce_manifest`
    rebuilds the manifest from the member rows and refuses with a `409` if the
    recomputed hash differs from the sealed one. So two reads returning EQUAL
    manifests is the reconstruction claim, and `manifest["content_hash"] ==
    body["content_hash"]` is the claim that what was reconstructed is what was
    sealed rather than merely something self-consistent.

    A failure means one of two things, and both are serious. If the two reads
    disagree, the manifest carries something non-deterministic — a timestamp
    read at request time, a set iterated in hash order — and no two consumers
    verifying the same snapshot will ever agree. If the reads agree but the
    hashes do not, a member row has drifted since the seal and anything already
    published against this snapshot is unreproducible.

    `serializer_version` is asserted because "byte-identical" is only meaningful
    relative to a declared serializer: the same members under a different
    canonicaliser are different bytes, and a consumer that could not see which
    one was used would have no way to tell a real mismatch from a version skew.
    """
    url = f"/api/reviews/{review['review_id']}/evidence-snapshots/{first_snapshot['snapshot_id']}"
    one = app_client.get(url, headers=lead_headers())
    two = app_client.get(url, headers=lead_headers())
    assert one.status_code == 200, one.text
    assert two.status_code == 200, two.text

    body, again = one.json(), two.json()
    assert body["manifest"] == again["manifest"], "two reads of one snapshot produced different manifests"
    assert body["manifest"]["content_hash"] == body["content_hash"], (
        "the reproduced manifest does not carry the hash the snapshot was sealed with"
    )
    assert body["content_hash"] == first_snapshot["content_hash"], (
        "the hash returned by the read differs from the hash returned by the build"
    )

    artifact = next(a for a in body["artifacts"] if a["artifact_kind"] == "canonical_manifest")
    assert artifact["serializer_version"] == "rfc8785/1", artifact
    assert HASH_SHAPE.match(artifact["payload_digest"] or ""), artifact


def test_the_same_request_twice_is_one_snapshot_and_one_version(app_client, review, extraction, first_snapshot):
    """`14-…:367`'s fourth criterion — "replay across worker restart creates one version".

    A worker that dies mid-build and is restarted replays its command. So does a
    retried HTTP call, and so does a second worker that claimed the same work.
    All three arrive here as the same request sent twice, and all three must
    leave the review with ONE snapshot: idempotency is the durable job queue's,
    keyed on a `command_hash` over the sorted member identities and the declared
    use, not a convention the command layer remembers to follow.

    A failure means a replay branches the review's snapshot chain. That is not a
    cosmetic duplicate — two snapshots of the same evidence carry two different
    ids and two different version numbers, so a downstream run that pinned the
    first one is now pinned to a version the review no longer considers current,
    and the second one supersedes a sibling rather than a predecessor.

    Asserting the same `job_id` as well as the same `snapshot_id` is what makes
    this non-vacuous: equal snapshot ids alone would also pass if the command
    layer had deduplicated on its own and the queue had minted a second job,
    which is the arrangement that breaks the moment the build moves to a worker.
    """
    body = _first_body(extraction)
    replay = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json=body,
        headers=lead_headers(),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["snapshot_id"] == first_snapshot["snapshot_id"], (
        "an identical request built a SECOND snapshot; the review's chain has branched on a replay"
    )
    assert replay.json()["job_id"] == first_snapshot["job_id"], (
        "an identical request minted a second durable job; the idempotency is not the queue's"
    )
    assert replay.json()["version_number"] == first_snapshot["version_number"] == 1, replay.text

    listed = app_client.get(f"/api/reviews/{review['review_id']}/evidence-snapshots", headers=lead_headers())
    assert listed.status_code == 200, listed.text
    # ⚠️ Runs BEFORE `test_a_second_snapshot_supersedes_the_first`, which is the
    # only test in this module that deliberately adds a second version. Keep it
    # above that one.
    assert [item["snapshot_id"] for item in listed.json()["items"]] == [first_snapshot["snapshot_id"]], listed.text


def test_a_second_snapshot_supersedes_the_first(app_client, review, extraction, first_snapshot):
    """`14-…:356`. A DIFFERENT request is a new version of one thing, not a new thing.

    The members are identical and only `intended_use` differs, which is the
    sharpest form of the claim: the command hash is over the declared use as
    well as the members, so this is genuinely a different command, and the
    result must still join the same chain rather than start its own.

    A failure in `version_number` means the chain is not counting — two
    snapshots both calling themselves version 1, which `ER-M021`'s one-root-per-
    review index should have refused outright. A failure in
    `supersedes_snapshot_id` is worse and quieter: the second snapshot exists,
    looks complete, and points at nothing, so a consumer walking backwards from
    it to find what it replaced finds a root and concludes there was no earlier
    snapshot to reconcile against.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={
            # UNDERSCORE. `intended_use` is a `canonical_type_token`
            # (`^[a-z][a-z0-9_]*$`), while a member's `owner_context` next door
            # is an `identity-ref` context (`^[a-z][a-z0-9-]*$`) and takes a
            # HYPHEN. Two released primitives, two conventions, one character
            # apart — and the hyphen version of this line failed schema
            # validation rather than the CHECK, so the error named five innocent
            # sibling fields first.
            "intended_use": "interim_analysis",
            "members": [_member(extraction, "n_randomised")],
        },
        headers=lead_headers(),
    )
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["snapshot_id"] != first_snapshot["snapshot_id"], second
    assert second["version_number"] == 2, second
    assert second["supersedes_snapshot_id"] == first_snapshot["snapshot_id"], second


def test_a_snapshot_records_which_gates_actually_ran(first_snapshot):
    """`14-…:345` names five gates. Two exist. The snapshot says WHICH two.

    A consumer deciding whether it may bind a snapshot reads the ledger; it
    never assumes five. That is the entire reason `gates_evaluated` is a column
    and not a comment — the answer has to stay correct on the day `ER-006B`
    lands a third gate, and every snapshot written before it must keep saying
    two rather than retroactively claiming three.

    So `fully_gated` is asserted **False**, deliberately. Asserting `True` today
    would be asserting a fiction: three of the five gates have no subject matter
    in this service at all — there is no `proposal_decisions` table, no rights
    model, and nothing that gives evidence a freshness horizon — so writing them
    as passing no-ops would make every snapshot report five gates cleared, which
    is indistinguishable from a snapshot five real gates examined. That is the
    failure mode where a control whose premise is unmet manufactures evidence:
    the check passes, the artifact looks fully vetted, and nothing was
    inspected.

    A failure here means the ledger and the reality have parted. If
    `gates_evaluated` grew without `gates_not_run` shrinking, a gate is being
    claimed that did not run. If it shrank, a gate that exists stopped running
    and no snapshot says so.
    """
    assert sorted(first_snapshot["gates_evaluated"]) == GATES_THAT_RAN, first_snapshot
    assert first_snapshot["fully_gated"] is False, (
        "this service reports every gate as run. Three of the five have no subject matter here, "
        "so either a gate landed without this test being updated, or a no-op is being counted as a pass"
    )
    assert sorted(first_snapshot["gates_not_run"]) == GATES_THAT_DID_NOT, first_snapshot
    assert not set(first_snapshot["gates_evaluated"]) & set(first_snapshot["gates_not_run"]), first_snapshot


# ---------------------------------------------------------------------------
# ER-007 — "a downstream run cannot bind an unresolved snapshot": the refusals
# ---------------------------------------------------------------------------


def test_a_member_the_review_does_not_own_is_refused(app_client, review, acceptance):
    """A pin naming a version that is not part of this review is `422`, not `404`.

    The review itself was resolved and is visible, so the failure is not about
    routing — it is the caller naming a version this review does not contain.
    `422` says "your request cannot be carried out", which is the truth; a `404`
    would send the caller looking for a broken URL.

    A failure that returns `201` is the one that matters: it means a snapshot
    can pin a version the review never screened, extracted or accepted, and a
    publication run binding that snapshot would read evidence from outside its
    own review while every hash in the manifest still verified. The manifest
    would be internally consistent and externally a lie.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={
            "intended_use": "publication",
            "members": [{"entity_type": "observation_version", "version_id": str(uuid.uuid4())}],
        },
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text


def test_a_member_kind_this_service_cannot_pin_is_refused(app_client, review, acceptance):
    """`research_assertion_version` is not a pinnable kind, and that is the point.

    The four kinds this service can pin are exactly the kinds it can read a
    `content_hash` and a `version_number` off a row for, and three of those four
    are the members of `ACCEPTANCE_SUBJECTS` — the things a lead can actually
    accept. A research assertion version is in neither set. Pinning one would
    put an input into the analysis set that nobody accepted and that no
    acceptance chain could ever be produced for, which is precisely the
    "unaccepted inputs" `09-…:329` refuses.

    A failure returning `201` means the snapshot's member table has grown a kind
    whose acceptance story nobody wrote. The row would look identical to a
    legitimate member — same shape, same digest — so the gap would be invisible
    to the consumer it endangers.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={
            "intended_use": "publication",
            "members": [{"entity_type": "research_assertion_version", "version_id": str(uuid.uuid4())}],
        },
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text


def test_a_snapshot_with_no_members_is_refused(app_client, review, acceptance):
    """An empty snapshot pins nothing and cannot make any run reproducible.

    It is not a harmless degenerate case. An empty snapshot has a well-formed id,
    a real content hash and a gate ledger, so it satisfies every structural check
    a consumer can apply — and binding it would let a run claim it was
    reproducible against a set that constrains nothing at all. Refusing at
    creation is the only place this can be caught, because after creation the
    snapshot is immutable and indistinguishable from a legitimate one.

    A failure returning `201` therefore means the service can mint a snapshot
    whose only property is the appearance of rigour.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={"intended_use": "publication", "members": []},
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text


def test_the_same_entity_twice_in_one_snapshot_is_refused(app_client, review, extraction, acceptance):
    """One entity, named twice, makes the manifest ambiguous about what a run may read.

    The uniqueness is over the ENTITY, not over the version: two versions of one
    observation in a single snapshot is the same defect as one version listed
    twice, because in both cases a run asking "what does this snapshot say about
    this observation" has two answers and no rule for choosing.

    A failure returning `201` means two runs can bind the identical snapshot,
    read the same entity, and legitimately reach different conclusions — with
    every hash verifying in both. That is worse than a wrong answer, because
    nothing in the artifact distinguishes it from a right one.
    """
    duplicate = _member(extraction, "n_randomised")
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={"intended_use": "publication", "members": [duplicate, dict(duplicate)]},
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text


def test_a_reviewer_cannot_take_a_snapshot(app_client, review, extraction, acceptance):
    """`14-…:352` requires the snapshot-preparer permission; `require_review_lead` is it.

    `403` and not `404` here, deliberately: the reviewer is assigned to this
    review and already knows it exists, so nothing is disclosed by admitting it.

    The refusal is not only about who may write. A snapshot's MEMBER LIST is a
    complete statement of what survived screening, arriving as a list of ids
    instead of a number — the same inference `ER-004.2` blinds counts against.
    A reviewer able to build one could read the review's inclusion set out of
    the response to their own request, so a failure here is a blinding breach as
    well as an authority one.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json=_first_body(extraction),
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# FND-003 — the announcement and the thing announced are one commit
# ---------------------------------------------------------------------------


def test_the_snapshot_commits_its_event_with_itself(app_client, first_snapshot):
    """`09-…:332` — "only terminal application of the complete immutable manifest emits".

    Read straight out of `domain_outbox` rather than from any API, because the
    claim is about a transaction boundary and no route can show one. `13-…:§5.3`
    step 4 puts the event in the same transaction as its cause, so there is no
    ordering of failures that leaves a committed snapshot nobody was told about,
    or an announcement of a snapshot that was rolled back.

    Exactly ONE row, and that count is the interesting half: the replay in
    `test_the_same_request_twice_…` returned the existing snapshot without
    rebuilding it, so a second event here would mean a retry announces a
    snapshot creation that did not happen, and every consumer downstream would
    process the same manifest twice.

    `content_hash` on the envelope is asserted because `14-…:359` requires
    consumers to "pin the snapshot ID/hash and never dereference 'latest'". An
    event carrying only the id would force every consumer to fetch before it
    could pin, and a consumer that fetched a different version than the one
    announced would have no way to notice. `gates_evaluated` rides along for the
    reason it is a column at all: the audience for incomplete gating is exactly
    the consumer deciding whether to bind, and sending the id alone would make
    the gap invisible to them.
    """
    from actor import Actor, ActorRole
    from database import actor_scope

    aggregate_id = uuid.UUID(first_snapshot["snapshot_id"])
    lead = Actor(tenant_id=uuid.UUID(TENANT), subject_id=LEAD, role=ActorRole("lead"))

    with actor_scope(lead) as session:
        rows = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT event_type, aggregate_type, aggregate_version, content_hash, payload "
                    "  FROM domain_outbox WHERE aggregate_id = :aid ORDER BY occurred_at"
                ),
                {"aid": aggregate_id},
            ).mappings()
        ]

    assert [row["event_type"] for row in rows] == ["review.evidence_snapshot.created.v1"], rows
    row = rows[0]
    assert row["aggregate_type"] == "evidence_snapshot", row
    assert row["aggregate_version"] == first_snapshot["version_number"], row
    assert row["content_hash"] == first_snapshot["content_hash"], (
        "the event announces a different hash than the snapshot was sealed with; a consumer pinning "
        "the announced hash would pin a manifest that does not exist"
    )
    assert sorted(row["payload"]["gates_evaluated"]) == GATES_THAT_RAN, row["payload"]
    assert row["payload"]["snapshot_id"] == first_snapshot["snapshot_id"], row["payload"]
    assert row["payload"]["member_manifest_hash"] == first_snapshot["member_manifest_hash"], row["payload"]


# ---------------------------------------------------------------------------
# What an adversarial review found. Each of these was a real defect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_use", ["meta-analysis", "Publication Run", "regulatory_submission"])
def test_a_badly_spelled_intended_use_is_a_422_and_not_a_500(app_client, review, extraction, bad_use: str) -> None:
    """The released manifest types `intended_use` as a `canonical_type_token`.

    All three of these used to return **500**. `compute_content_hash` validates
    before it hashes and raises `SchemaValidationError`, which is a bare
    `ValueError` subclass that `app.py` registers no handler for — so a request
    a person would obviously write (`meta-analysis`) produced an unhandled
    exception whose body was the "unevaluated properties" pile naming eleven
    innocent fields.

    The database CHECKs could never have caught it either: `seal` runs before
    `session.add`, so `ck_evidence_snapshots_intended_use_shape` and
    `..._not_regulatory` were unreachable from this route. The boundary is the
    only place this can be refused legibly.
    """
    body = _first_body(extraction)
    body["intended_use"] = bad_use
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json=body,
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text
    assert "intended_use" in response.text


def test_one_instant_has_one_spelling_across_post_get_and_the_manifest(app_client, review, first_snapshot) -> None:
    """`created_at` is trailing-`Z` UTC everywhere, envelope and manifest alike.

    Three spellings of one instant existed in this service before `instants.py`:
    `flow._utc_z` rendered `Z` because `urn:datacompany:stable-identity:1`'s
    `timestamp` primitive admits ONLY that form; `review_jobs._iso` rendered
    `+00:00`, fixing a real measured defect in the wrong direction; and
    everything else called `.isoformat()` bare and rendered whatever offset the
    value carried.

    `ER-007` then put two of them in ONE response body — `created_at` as
    `+00:00` in the envelope, `created_at` as `Z` inside the manifest it
    contained — and the first version of THIS test asserted that as intended.
    It was not intended. It was two mistakes agreeing with each other, which is
    the most expensive kind of green test.

    So the assertion is now equality, not "both are valid": one instant, one
    string, wherever it appears.
    """
    fetched = app_client.get(
        f"/api/reviews/{review['review_id']}/evidence-snapshots/{first_snapshot['snapshot_id']}",
        headers=lead_headers(),
    )
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()

    assert body["created_at"] == first_snapshot["created_at"], "POST and GET render the same instant differently"
    assert body["created_at"].endswith("Z"), body["created_at"]
    assert body["created_at"] == body["manifest"]["created_at"], (
        "the envelope and the manifest it carries disagree about one instant"
    )
    assert datetime.fromisoformat(body["created_at"].replace("Z", "+00:00")).tzinfo is not None


def _lead_actor():  # noqa: ANN202
    from actor import Actor, ActorRole

    return Actor(tenant_id=uuid.UUID(TENANT), subject_id=LEAD, role=ActorRole("lead"))


def test_a_chain_cannot_cross_two_reviews(app_client, review, first_snapshot) -> None:
    """`0018`'s idiom is two partial uniques PLUS a trigger. This is the trigger.

    The uniques give "one root per review" and "one successor per node"; neither
    says the successor is in the SAME review. A row in review R2 superseding
    R1's tail satisfies both, leaves R2 with no root at all, and `_entity_of` —
    which walks the chain unfiltered — then hands R2's chain R1's root as its
    `entity_id`. That is exactly the identity collision `entity_id` exists to
    prevent, arriving by the one route the uniques do not cover.

    No service path can do this, which is why it is asserted against the
    database directly: unenforced is not the same as unreachable.
    """
    import uuid as _uuid

    from sqlalchemy import text as _text

    from database import actor_scope

    other = _uuid.uuid4()
    with actor_scope(_lead_actor()) as session:
        session.execute(
            _text(
                "INSERT INTO reviews (review_id, tenant_id, project_id, external_key, title, "
                "created_by_subject_id) VALUES (:rid, :tid, :pid, :key, 'other', :sub)"
            ),
            {
                "rid": other,
                "tid": _uuid.UUID(TENANT),
                "pid": _uuid.uuid4(),
                "key": f"cross-{_uuid.uuid4()}",
                "sub": LEAD,
            },
        )

    with actor_scope(_lead_actor()) as session, pytest.raises(Exception) as excinfo:
        session.execute(
            _text(
                "INSERT INTO evidence_snapshots (snapshot_id, tenant_id, review_id, version_number,"
                " supersedes_snapshot_id, schema_ref, intended_use, protocol_version_id,"
                " member_count, member_manifest_hash, member_ordering_rule, content_hash,"
                " gates_evaluated, job_id, created_by_subject_id)"
                " SELECT :new, tenant_id, :other, 2, snapshot_id, schema_ref, intended_use,"
                "        protocol_version_id, 0, member_manifest_hash, member_ordering_rule,"
                "        content_hash, gates_evaluated, job_id, created_by_subject_id"
                "   FROM evidence_snapshots WHERE snapshot_id = :tail"
            ),
            {"new": _uuid.uuid4(), "other": other, "tail": first_snapshot["snapshot_id"]},
        )
    assert "one review" in str(excinfo.value), str(excinfo.value)


def test_a_withdrawal_written_in_the_same_transaction_still_wins(app_client, review, extraction, acceptance) -> None:
    """The chain TAIL, not the newest `decided_at`. This is the tie the review found.

    `_require_accepted` first read the chain with
    `ORDER BY decided_at DESC LIMIT 1`. That coincides with the tail only by
    luck: `decided_at` defaults to `now()`, which in Postgres is
    `transaction_timestamp()` and is **constant within a transaction**. Two
    chain nodes written together therefore tie, and `LIMIT 1` picks arbitrarily
    — so a `withdraw` written beside its `accept` could return `accept`, and a
    snapshot would pin a version the review had already withdrawn.

    The review rated this latent because no service path writes two nodes per
    transaction today. "No path does it today" is exactly the assurance that
    stops being true without anyone noticing, so this constructs the tie
    directly and requires the refusal.

    ⚠️ The first version of this test reused the `acceptance` fixture's row as
    the chain root. That row is written over HTTP, in its OWN transaction, so
    the two nodes had two distinct `decided_at` values and did NOT tie — the
    test would have exercised the easy case and passed against the unfixed
    code. The premise assertion below is what caught that, and it stays.

    `NOT EXISTS (successor)` is the tail by definition and cannot tie.
    """
    from sqlalchemy import text as _text

    from database import actor_scope

    # A DIFFERENT observation from the one `acceptance` accepted, so this test
    # owns its whole chain and both nodes can be written together.
    observation = _observation(extraction, "mortality_28d_pct")
    version_id = uuid.UUID(observation["observation_version_id"])

    # `subject_version_id` is GENERATED from the per-kind FK columns, so it is
    # set by writing `observation_version_id` and cannot be inserted directly.
    with actor_scope(_lead_actor()) as session:
        template = (
            session.execute(
                _text("SELECT tenant_id, protocol_version_id FROM evidence_acceptances  WHERE acceptance_id = :a"),
                {"a": uuid.UUID(acceptance["acceptance_id"])},
            )
            .mappings()
            .one()
        )
        content_hash = session.execute(
            _text("SELECT content_hash FROM observation_versions WHERE observation_version_id = :v"),
            {"v": version_id},
        ).scalar_one()

        root = uuid.uuid4()
        insert = _text(
            "INSERT INTO evidence_acceptances (acceptance_id, review_id, tenant_id,"
            " subject_kind, observation_version_id, subject_content_hash,"
            " protocol_version_id, decision, rationale, supersedes_acceptance_id,"
            " decided_by_subject_id)"
            " VALUES (:id, :r, :t, 'observation_version', :v, :h, :pv, :d, :why, :prev, :sub)"
        )
        common = {
            "r": uuid.UUID(review["review_id"]),
            "t": template["tenant_id"],
            "v": version_id,
            "h": content_hash,
            "pv": template["protocol_version_id"],
            "sub": LEAD,
        }
        session.execute(
            insert,
            {**common, "id": root, "d": "accept", "why": "checked against the table", "prev": None},
        )
        session.execute(
            insert,
            {
                **common,
                "id": uuid.uuid4(),
                "d": "withdraw",
                "why": "retracted after source check",
                "prev": root,
            },
        )
        tied = session.execute(
            _text("SELECT count(DISTINCT decided_at) FROM evidence_acceptances WHERE subject_version_id = :v"),
            {"v": version_id},
        ).scalar_one()

    # The premise, asserted rather than assumed: if these did NOT tie, the test
    # would be exercising the easy case and would pass against the old code.
    assert tied == 1, (
        f"the two acceptance rows have {tied} distinct `decided_at` values, so they do not "
        "tie and this test no longer reproduces the defect it exists for"
    )

    response = app_client.post(
        f"/api/reviews/{review['review_id']}/evidence-snapshots",
        json={
            "intended_use": "publication",
            "members": [
                {
                    "entity_type": "observation_version",
                    "version_id": observation["observation_version_id"],
                }
            ],
        },
        headers=lead_headers(),
    )
    assert response.status_code == 422, response.text
    assert "not currently accepted" in response.text, response.text
    assert "withdraw" in response.text, response.text
