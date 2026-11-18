"""`ER-006` — what a review ACCEPTS, end to end, and every refusal around it.

`test_e2e_acceptance.py` proves a review can say which reports are *in*. This
one proves the next sentence: what those reports SAY, who stands behind each
value, and who is allowed to accept it. The drive is the full one — locked
protocol, sealed corpus, dual screening to a settled `included` — because every
guarantee here is stated in terms of that settled state and cannot be reached
from a stub:

* a research object may only be built from a record the flow ledger INCLUDED,
  and that ledger is derived from the immutable decision history;
* an observation re-recorded under the same `variable_key` is a new VERSION of
  one observation, which is `uq_observation_variable` plus a supersession chain,
  not an application convention;
* acceptance is the lead's act alone, it witnesses the subject's content hash
  (`evidence_acceptances_witnesses_its_subject` refuses a witness that does not
  match), and re-accepting after a withdrawal must CONTINUE the chain —
  `uq_acceptance_one_chain_per_subject` is a partial unique index.

Every one of those is a Postgres constraint, trigger or index. A SQLite or
in-memory double would run this file green while enforcing none of them, so the
module is skipped rather than faked when `DATABASE_URL` is not postgresql. The
runtime role (`evidence_review_app`, a non-owner) is the one `conftest.py`
points `DATABASE_URL` at, so the refusals below are the ones production takes.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: Its own tenant. Nothing truncates between modules, so sharing `tenant-e2e`
#: with the acceptance run would put two suites' rows behind one RLS predicate
#: and make either one's counts depend on whether the other ran.
TENANT = tenant("tenant-accepted-evidence")
LEAD = "lead-carol"
REVIEWER_A = "reviewer-alice"
REVIEWER_B = "reviewer-bob"

#: Every hash this service writes goes through `research_contracts`' RFC 8785
#: canonicaliser, so the shape is part of the contract and not an implementation
#: detail — an export or a downstream witness parses it.
HASH_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Three clean records: two the reviewers will agree to include (the study and
#: its companion report), one they will agree to exclude. The excluded one is
#: not spare corpus — it is the negative control for the inclusion rule, and it
#: has to be a record that genuinely exists and genuinely is not included.
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

    Module-scoped for the reason `test_e2e_acceptance`'s is: the assertions below
    are about the state THIS run produced, and a per-test rebuild would be both
    slow and a different review.
    """
    created = app_client.post(
        "/api/reviews",
        json={
            "project_id": "dogfood",
            "external_key": f"evidence-{uuid.uuid4()}",
            "title": "What the studies say",
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

    locked = app_client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=lead_headers())
    assert locked.status_code == 200 and locked.json()["locked"] is True, locked.text

    activated = app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "recording accepted evidence"},
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

    Agreement on every record is deliberate — a conflict would leave a record
    `uncertain`, and `included_record_ids` reads the DERIVED ledger, so an
    unadjudicated disagreement would make the inclusion rule below refuse a
    record this fixture believes it included.
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
    # The excluded one is chosen by title, not by position: `list_records` orders
    # by `created_at`, and pinning an index here would silently re-point the
    # negative control at an included record if that ordering ever changed.
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
    # The floor the whole module rests on. Read from `/flow` rather than inferred
    # from the decisions posted above, because the ledger is the one authority
    # `create_research_object` consults — asserting on what we SENT would pass
    # against a derivation that dropped the record.
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
    """One study, reported by two included reports — the point of `ER-006.1`."""
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
    """The lead accepts one exact observation version. The authority act."""
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


# ---------------------------------------------------------------------------
# ER-006.1 / ER-006.2 / ER-006.4 / ER-006.5 — recording what the studies say
# ---------------------------------------------------------------------------


def test_two_included_reports_become_one_research_object_with_a_reproducible_hash(research_object):
    """`ER-006.1`. "Studies included" is finally countable apart from "reports included"."""
    assert HASH_SHAPE.match(research_object["current_version"]["content_hash"]), research_object["current_version"]
    assert research_object["current_version"]["version_number"] == 1
    assert research_object["current_version"]["revision_kind"] == "initial"
    roles = sorted(link["link_role"] for link in research_object["records"])
    assert roles == ["companion", "primary"], research_object["records"]


def test_every_extracted_observation_is_a_first_version_carrying_an_expert_basis(extraction):
    """`16-…:§3.7` arm 2. Arm 1 is refused outright — see the negative below.

    `basis_kind` is asserted on EVERY observation, not on the first: the writer
    sets it per row, so a check on one row would pass against a loop that got it
    right once.
    """
    assert len(extraction["observations"]) == 2, extraction
    assert extraction["current_version"]["version_number"] == 1
    assert extraction["current_version"]["extraction_method"] == "manual"
    for observed in extraction["observations"]:
        assert observed["version_number"] == 1, observed
        assert observed["basis_kind"] == "expert_assertion", observed
        assert HASH_SHAPE.match(observed["content_hash"]), observed


def test_a_re_extracted_variable_becomes_a_second_version_of_one_observation(app_client, review, extraction):
    """`ER-006.4` — a correction, not a duplicate row.

    The identity is what must survive: same `observation_id`, next
    `version_number`, different content hash. Asserting only on the version
    number would pass against a writer that minted a SECOND observation and
    started it at 1 — which is precisely the shape `uq_observation_variable`
    exists to make impossible, and precisely what a reader counting variables
    would then double-count.

    The dataset version increments too: the observation belongs to a dataset
    version, so a correction produces a new one rather than editing the sealed
    one the first extraction wrote.
    """
    original = _observation(extraction, "mortality_28d_pct")

    corrected = app_client.post(
        f"/api/reviews/{review['review_id']}/research-objects/{extraction['research_object_id']}/extractions",
        json={
            "dataset_key": extraction["dataset_key"],
            "rationale": "the first reading took the control arm by mistake",
            "observations": [
                {
                    "variable_key": "mortality_28d_pct",
                    "value": 29.0,
                    "unit": "percent",
                    "expert_rationale": "table 2, intervention arm — re-read after checking the column header",
                }
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert corrected.status_code == 201, corrected.text
    body = corrected.json()

    assert body["extraction_dataset_id"] == extraction["extraction_dataset_id"], (
        "re-posting the same dataset_key must version the existing dataset, not open a second one"
    )
    assert body["current_version"]["version_number"] == 2, body["current_version"]

    revised = _observation(body, "mortality_28d_pct")
    assert revised["observation_id"] == original["observation_id"], "a correction must keep the observation's identity"
    assert revised["version_number"] == 2, revised
    assert revised["value"] == 29.0
    assert revised["content_hash"] != original["content_hash"], (
        "two versions carrying different values must not share a content hash"
    )


def test_an_assertion_cites_the_observation_against_it_as_readily_as_the_one_for_it(app_client, review, extraction):
    """`ER-006.5`. `contradicts` is a first-class stance, not an omission.

    A claim that can only cite its confirming evidence is the specific dishonesty
    a systematic review exists to prevent, so both stances are exercised in one
    call — a schema that accepted `supports` and quietly dropped `contradicts`
    would pass a test that only ever sent the former.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/assertions",
        json={
            "assertion_key": "mortality_benefit",
            "statement": "Tocilizumab reduces 28-day mortality in hospitalised adults.",
            "content": {"certainty": "moderate"},
            "support": [
                {
                    "observation_version_id": _observation(extraction, "n_randomised")["observation_version_id"],
                    "stance": "supports",
                },
                {
                    "observation_version_id": _observation(extraction, "mortality_28d_pct")["observation_version_id"],
                    "stance": "contradicts",
                },
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["current_version"]["version_number"] == 1
    assert HASH_SHAPE.match(body["current_version"]["content_hash"]), body["current_version"]
    assert sorted(s["stance"] for s in body["support"]) == ["contradicts", "supports"], body["support"]


# ---------------------------------------------------------------------------
# ER-006 — acceptance, the authority act
# ---------------------------------------------------------------------------


def test_the_lead_s_acceptance_witnesses_the_exact_content_it_accepted(app_client, review, extraction, acceptance):
    """The witness is the whole point of the row.

    `subject_content_hash` is what makes "accepted" still mean "accepted THIS
    content" once the decision has travelled into an export and left the version
    table behind, and `evidence_acceptances_witnesses_its_subject` refuses a
    witness that does not match its subject. Asserted against the hash the
    EXTRACTION returned rather than against the acceptance's own echo, because
    comparing a value to itself proves nothing about the trigger.
    """
    observation = _observation(extraction, "n_randomised")
    assert acceptance["decision"] == "accept"
    assert acceptance["subject_kind"] == "observation_version"
    assert acceptance["subject_version_id"] == observation["observation_version_id"]
    assert acceptance["subject_content_hash"] == observation["content_hash"], acceptance
    assert acceptance["supersedes_acceptance_id"] is None, "the first decision about a version is a chain root"


def test_the_acceptance_is_readable_back_as_a_chain(app_client, review, acceptance):
    """`GET /acceptances` returns history, not a snapshot — the tail is the fact."""
    listed = app_client.get(f"/api/reviews/{review['review_id']}/acceptances", headers=lead_headers())
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    mine = [row for row in items if row["acceptance_id"] == acceptance["acceptance_id"]]
    assert len(mine) == 1, items
    assert mine[0]["subject_content_hash"] == acceptance["subject_content_hash"]
    assert mine[0]["decided_by"] == LEAD
    assert mine[0]["decided_at"], "an authority act with no timestamp is unattributable in time"


def test_a_withdrawn_version_can_be_accepted_again_by_citing_the_withdrawal(app_client, review, extraction, acceptance):
    """The chain continues; it does not fork.

    `ck_acceptance_withdrawal_cites_what_it_withdraws` forces the withdrawal to
    name what it withdraws, and `uq_acceptance_one_chain_per_subject` (a partial
    unique index on the NULL-predecessor rows) means the re-acceptance has to
    cite the withdrawal rather than start a second root. An earlier version of
    that constraint also forced every `accept` to have a NULL predecessor, which
    made a withdrawn version permanently unacceptable — this test is the shape
    that finds that regression if it returns.
    """
    observation = _observation(extraction, "n_randomised")
    review_id = review["review_id"]

    withdrawn = app_client.post(
        f"/api/reviews/{review_id}/acceptances",
        json={
            "subject_kind": "observation_version",
            "subject_version_id": observation["observation_version_id"],
            "decision": "withdraw",
            "rationale": "the source table was superseded by a corrigendum",
            "supersedes_acceptance_id": acceptance["acceptance_id"],
        },
        headers=lead_headers(),
    )
    assert withdrawn.status_code == 201, withdrawn.text
    assert withdrawn.json()["supersedes_acceptance_id"] == acceptance["acceptance_id"]

    reaccepted = app_client.post(
        f"/api/reviews/{review_id}/acceptances",
        json={
            "subject_kind": "observation_version",
            "subject_version_id": observation["observation_version_id"],
            "decision": "accept",
            "rationale": "the corrigendum confirms the original figure",
            "supersedes_acceptance_id": withdrawn.json()["acceptance_id"],
        },
        headers=lead_headers(),
    )
    assert reaccepted.status_code == 201, (
        "a withdrawal must not make a version permanently unacceptable: " + reaccepted.text
    )
    assert reaccepted.json()["supersedes_acceptance_id"] == withdrawn.json()["acceptance_id"]

    # Three rows, one chain, in order. Read back from the route rather than
    # inferred from the three responses, because "the chain is readable" is the
    # guarantee an auditor actually depends on.
    chain = [
        row
        for row in app_client.get(f"/api/reviews/{review_id}/acceptances", headers=lead_headers()).json()["items"]
        if row["subject_version_id"] == observation["observation_version_id"]
    ]
    assert [row["decision"] for row in chain] == ["accept", "withdraw", "accept"], chain


# ---------------------------------------------------------------------------
# Negative controls — each must FAIL, and for its own reason
# ---------------------------------------------------------------------------


def test_a_research_object_cannot_be_built_from_a_record_the_review_excluded(app_client, review, settled):
    """The inclusion rule, checked against the derived ledger and nothing else.

    The excluded record is real, its version is real and its corpus seal is the
    review's own — so the ONLY thing that can refuse this is the flow ledger.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/research-objects",
        json={
            "object_key": f"excluded-{uuid.uuid4()}",
            "content": {"design": "observational"},
            "rationale": "trying to extract from a report the review dropped",
            "records": [_link(settled["excluded"], review, "primary")],
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 422, response.text
    assert "included" in response.text, "the refusal must name inclusion, not merely say no"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_research_object_needs_exactly_one_primary_report(app_client, review, settled):
    """With none the study cannot be cited; with two there is no single citation."""
    review_id = review["review_id"]

    none_primary = app_client.post(
        f"/api/reviews/{review_id}/research-objects",
        json={
            "object_key": f"no-primary-{uuid.uuid4()}",
            "content": {},
            "rationale": "two companions and no report to cite",
            "records": [
                _link(settled["included"][0], review, "companion"),
                _link(settled["included"][1], review, "companion"),
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert none_primary.status_code == 422, none_primary.text
    assert "primary" in none_primary.text

    two_primary = app_client.post(
        f"/api/reviews/{review_id}/research-objects",
        json={
            "object_key": f"two-primary-{uuid.uuid4()}",
            "content": {},
            "rationale": "two reports both claiming to be the citation",
            "records": [
                _link(settled["included"][0], review, "primary"),
                _link(settled["included"][1], review, "primary"),
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert two_primary.status_code == 422, two_primary.text
    assert "primary" in two_primary.text


def test_an_observation_claiming_a_located_evidence_basis_fails_closed(app_client, review, research_object):
    """`DM-AT-25`. An unverifiable pinned reference is refused, not stored.

    `EvidenceLocation` is owned by Ingestion/Documents and 8810 has no client
    that can verify a reference into it, so storing one anyway and calling it
    evidence is the exact failure this service exists to prevent. The slug is
    asserted, not just the status: a caller has to be able to branch on it.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/research-objects/{research_object['research_object_id']}/extractions",
        json={
            "dataset_key": f"located-{uuid.uuid4()}",
            "rationale": "pinning a value to a page in a document we cannot resolve",
            "observations": [
                {
                    "variable_key": "n_randomised",
                    "value": 4116,
                    "expert_rationale": "page 4, table 1",
                    "basis_kind": "evidence_located",
                }
            ],
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 422, response.text
    assert "evidence_location_unverifiable" in response.json()["detail"], response.json()


def test_a_reviewer_cannot_accept_evidence(app_client, review, research_object):
    """The one lead-only act in `ER-006`.

    Recording is a reviewer's work — every fixture above posts as `REVIEWER_A`
    and gets 201 — so this refusal is about the ACCEPTANCE and not about the
    package being locked to leads. Both halves have to hold or the split is not
    a split.
    """
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/acceptances",
        json={
            "subject_kind": "research_object_version",
            "subject_version_id": research_object["current_version"]["research_object_version_id"],
            "decision": "accept",
            "rationale": "I extracted it so I will accept it",
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_a_second_acceptance_of_one_version_must_supersede_the_first(app_client, review, extraction, acceptance):
    """`uq_acceptance_one_chain_per_subject` — one chain per subject, not two.

    The status code is deliberately not pinned to an exact value: this refusal
    comes from a partial unique index, so it arrives as an `IntegrityError` and
    is typed by `app._integrity_handler` (a 409 `state_conflict` today). What
    must never happen is a SECOND chain root being created, and `201` is the only
    answer that would mean it was.
    """
    observation = _observation(extraction, "n_randomised")
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/acceptances",
        json={
            "subject_kind": "observation_version",
            "subject_version_id": observation["observation_version_id"],
            "decision": "accept",
            "rationale": "a second opinion, filed as if it were the first",
        },
        headers=lead_headers(),
    )
    assert response.status_code != 201, "a second chain root was created for one subject version"
    assert response.status_code >= 400, response.text


# ---------------------------------------------------------------------------
# ER-V013 / DM-AT-18 — the pre-ADR regulatory denylist, on every write here
# ---------------------------------------------------------------------------


def _denylist_probes(review: dict[str, Any], research_object: dict[str, Any], extraction: dict[str, Any]):
    """Every free-form-JSON write in `routers/evidence.py`, each otherwise VALID.

    Every id, key and link below is real, so each of these bodies would succeed
    if the guard were removed. That is what makes them negative controls rather
    than four ways of spelling 422: a probe built from dummy uuids would be
    refused by `load_review` or a foreign key and look identical from outside.

    The casing varies on purpose. `work_type`, `workType` and `x-work-type` fold
    to the same token under `denylist._norm`, and a guard matching the literal
    string only is bypassed by the first JavaScript client that touches it.
    """
    review_id = review["review_id"]
    object_id = research_object["research_object_id"]
    return {
        "create_research_object": (
            f"/api/reviews/{review_id}/research-objects",
            {
                "object_key": f"denylist-{uuid.uuid4()}",
                # Nested, not top level: "at any depth" is the requirement, and a
                # top-level-only check is satisfied by moving the key down one.
                "content": {"design": {"work_type": "regulatory_instrument"}},
                "rationale": "a regulatory activation dressed as a study description",
                "records": [
                    {
                        "review_record_id": research_object["records"][0]["review_record_id"],
                        "review_record_version_id": research_object["records"][0]["review_record_version_id"],
                        "corpus_version_id": review["corpus_version_id"],
                        "link_role": "primary",
                    }
                ],
            },
        ),
        "revise_research_object": (
            f"/api/reviews/{review_id}/research-objects/{object_id}:revise",
            {
                "content": {"design": {"workType": "regulatory_instrument"}},
                "rationale": "a revision carrying the discriminator in camelCase",
                "revision_kind": "correction",
            },
        ),
        "record_extraction": (
            f"/api/reviews/{review_id}/research-objects/{object_id}/extractions",
            {
                "dataset_key": f"denylist-{uuid.uuid4()}",
                "rationale": "the discriminator hiding in an observation's value",
                "observations": [
                    {
                        "variable_key": "instrument",
                        # `record_extraction` guards `{"value": ...}` per
                        # observation, so this is the only place in that body a
                        # caller-shaped object reaches JSONB at all.
                        "value": {"x-work-type": "regulatory_instrument"},
                        "expert_rationale": "recording what the document is",
                    }
                ],
            },
        ),
        "record_assertion": (
            f"/api/reviews/{review_id}/assertions",
            {
                # `assertion_key` is pattern-bound (`urn:datacompany:review-assertion-recorded:1`)
                # to lowercase dot-separated segments — no hyphens — so this
                # can't reuse the raw uuid the way `dataset_key` above does.
                "assertion_key": f"denylist_{uuid.uuid4().hex}",
                "statement": "The instrument applies to hospitalised adults.",
                "content": {"scope": {"regulatory_applicability": "member states"}},
                "support": [
                    {
                        "observation_version_id": extraction["observations"][0]["observation_version_id"],
                        "stance": "supports",
                    }
                ],
            },
        ),
    }


@pytest.mark.parametrize(
    "route",
    ["create_research_object", "revise_research_object", "record_extraction", "record_assertion"],
)
def test_a_body_carrying_a_guarded_discriminator_is_rejected_at_any_depth(
    app_client, review, research_object, extraction, route
):
    """`ER-002.7` / `ER-V013` / `DM-AT-18` — reject, never silently drop.

    Dropping the field would return `201` while the caller believes they typed a
    canonical regulatory entity, and nothing downstream could tell the two apart.
    So the assertion is on the SLUG and the path, not merely on "some 4xx": a 422
    from a foreign key would mean the guard never ran.

    Parametrised over every route in `routers/evidence.py` that takes free-form
    JSON, because `guard_body` is imported per router rather than installed as
    middleware — a handler that forgets the call is a handler with no denylist,
    which is exactly how `DedupRunBody.config` came to accept one and return 201.
    """
    path, body = _denylist_probes(review, research_object, extraction)[route]
    response = app_client.post(path, json=body, headers=headers(REVIEWER_A))

    assert response.status_code == 400, f"{route}: {response.text}"
    problem = response.json()
    assert problem["code"] == "forbidden_discriminator", problem
    assert problem["forbidden_paths"], problem
    # The path has to point INTO the body, not at its root — that is what tells
    # the caller where to look, and it is the observable proving the walk
    # descended rather than pattern-matching the top level.
    assert all(p.count(".") >= 2 for p in problem["forbidden_paths"]), problem["forbidden_paths"]


def test_the_acceptance_route_offers_a_discriminator_nowhere_to_hide(app_client, review, research_object):
    """The fifth POST route, and the only one with no `guard_body` call.

    That is not an omission, and asserting a 4xx here would be asserting a
    fiction: `AcceptanceBody` has no free-form JSON field at all. Its five fields
    are two pattern-constrained enums, a uuid, an optional uuid and a bounded
    string, so there is no object for a guarded key to be nested inside and
    nothing on this route reaches JSONB or RFC 8785 canonicalisation. Pydantic
    ignores an unknown key rather than storing it.

    So this test states the structural fact the other four rely on, in the form
    that goes RED the day it stops being true: if anyone adds a `dict`- or
    `Any`-typed field to `AcceptanceBody`, this route acquires a free-form body
    with no guard behind it and the assertion below fails. A behavioural probe
    could not notice that — it would keep passing while the hole opened.
    """
    from api.routers.evidence import AcceptanceBody

    free_form = [
        name
        for name, field in AcceptanceBody.model_fields.items()
        if field.annotation is Any or getattr(field.annotation, "__origin__", None) in (dict, list)
    ]
    assert free_form == [], (
        f"AcceptanceBody now carries free-form JSON in {free_form}; POST /acceptances is the one "
        "route in routers/evidence.py with no guard_body call, so that field reaches JSONB "
        "un-denylisted. Add guard_body to decide_acceptance and make this a behavioural probe."
    )

    # And the unknown key is dropped rather than persisted: it cannot be read
    # back, so it never became a fact about this review.
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/acceptances",
        json={
            "subject_kind": "research_object_version",
            "subject_version_id": research_object["current_version"]["research_object_version_id"],
            "decision": "accept",
            "rationale": "accepting the study grouping",
            "work_type": "regulatory_instrument",
        },
        headers=lead_headers(),
    )
    assert response.status_code == 201, response.text
    assert "work_type" not in response.text
    listed = app_client.get(f"/api/reviews/{review['review_id']}/acceptances", headers=lead_headers())
    assert "work_type" not in listed.text, "an ignored key must not have reached a stored row"
