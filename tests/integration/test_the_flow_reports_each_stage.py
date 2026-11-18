"""PRISMA 2020 counts each stage separately — `ER-005.1`, `ER-009.3`.

## The reporting rule this file is about

PRISMA 2020 boxes 8-10 and 16-17 are not one population. Records excluded at
TITLE/ABSTRACT screening are reported as their own number, and reports excluded
at FULL-TEXT eligibility are reported separately AND broken down by reason —
"reports excluded, with reasons" is the literal box. Merging the two is a
recognised reporting failure; journals and HTA bodies reject a diagram that
cannot say which stage an exclusion happened at, because the two numbers answer
different questions. A record dropped from the abstract was never retrieved; a
report dropped at full text was read in full and found ineligible, and the
reason it was found ineligible is the evidence that the eligibility criteria
were applied.

## What this service reported instead

`flow_derivation.derive` built `exclusion_reasons` with a `Counter` filtered on
`bucket == "excluded"` and NOTHING else — no filter on `settled_at`, which is
the field carrying the stage. Every stage's reasons landed in one flat dict, and
`GET /flow` returned it as twelve flat keys with no way back. The sealed export
embedded the same dict verbatim.

The per-record `omissions` array is the one place a consumer could have rebuilt
the split themselves, and it dropped the stage too: `review_record_id`,
`bucket`, `exclusion_reason_code`, `adjudicated`, and not the `settled_at` that
was sitting on the same object.

So the answer was unrecoverable at both levels. That is what makes it a defect
rather than a missing convenience: no consumer of this service, given
everything it emits, could produce a compliant PRISMA diagram for a two-stage
review.

## Why it only became reachable now

Every review was single stage until `fr` #822 let the studio create a second
one on 2026-08-17. On a single-stage review the merged dict and the per-stage
dict are the same numbers, which is why `test_the_common_case_did_not_move`
below is here: the fix must be invisible to every review that existed before.

## What is NOT being changed, and why the tests say so

`counts` keeps its exact twelve members. `inclusion-set-export.v2`'s
`$defs/flow_counts` is `additionalProperties: false` and its `$comment` explains
that the conservation identities are arithmetic over exactly those members, so a
thirteenth tally arriving unnoticed would make the sealed block say something the
identities were never checked against. Adding one would also move
`canonical_counts_hash` for every review and stale every `flow_projection` in
the table. `test_the_flat_counts_and_their_hash_did_not_move` pins the hash as a
LITERAL measured on the unmodified source, which is the only form of that
assertion that can fail.

The per-stage tallies therefore go BESIDE `counts` on the `/flow` body, which
nothing seals and no schema declares, and the stage goes onto each `omissions`
entry, which `:2` deliberately leaves undeclared — `items_hash`'s own `$comment`
says declaring the item arrays is "deliberately deferred". That changes
`items_hash` for NEW exports only;
`test_an_export_sealed_under_the_old_omission_shape_still_verifies` reconstructs
a pre-change row and proves the verifier, which recomputes from
`export.payload`, is indifferent.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-per-stage")
LEAD = "lead-prisma"
REVIEWER_A = "reviewer-anja"
REVIEWER_B = "reviewer-bram"

#: Three records, no shared identifiers — a merge would move a record out of the
#: screening buckets and dedup is not what is under test.
CORPUS = {
    "records": [
        {"title": "Paper one: the abstract already shows the wrong population", "doi": "10.2000/one", "year": 2020},
        {"title": "Paper two: plausible abstract, full text never obtainable", "doi": "10.2000/two", "year": 2021},
        {"title": "Paper three: eligible at both stages", "doi": "10.2000/three", "year": 2022},
    ]
}

#: THE TWO REASONS, one per stage, deliberately disjoint. The whole assertion is
#: that neither appears in the other stage's breakdown, and that is only a
#: statement if no record shares a reason across the stage boundary.
TA_REASON = "wrong_population"
FT_REASON = "not_retrievable"


def headers(subject: str, *, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def lead_headers() -> dict[str, str]:
    return headers(LEAD, role="lead")


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


def _decide(client, review_id, *, stage_id, record_id, reviewer, disposition, reason=None):  # noqa: ANN001, ANN202
    body: dict[str, Any] = {"stage_id": stage_id, "review_record_id": record_id, "disposition": disposition}
    if reason is not None:
        body["exclusion_reason_code"] = reason
    response = client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(reviewer))
    assert response.status_code == 201, response.text
    return response.json()


def _flow(client, review_id) -> dict[str, Any]:  # noqa: ANN001
    response = client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers())
    assert response.status_code == 200, response.text
    return response.json()


def _by_stage(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["stage_key"]: entry for entry in body.get("counts_by_stage") or []}


def _build_review(client, *, stage_definitions: list[dict[str, Any]], title: str) -> str:  # noqa: ANN001
    """A review activated over `CORPUS` with a sealed corpus version."""
    created = client.post(
        "/api/reviews",
        json={"project_id": "prisma", "external_key": f"per-stage-{uuid.uuid4()}", "title": title},
        headers=lead_headers(),
    )
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {"design": "randomised controlled trial"},
            "exclusion_criteria": {"design": "not a randomised controlled trial"},
            "stage_definitions": stage_definitions,
            "exclusion_reason_vocabulary": [TA_REASON, FT_REASON, "wrong_design"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]

    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=lead_headers())
    assert locked.status_code == 200, locked.text

    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "per-stage reporting"},
        headers=lead_headers(),
    )
    assert activated.status_code == 200, activated.text

    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["accepted_count"] == 3

    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())
    assert sealed.status_code == 201, sealed.text
    return review_id


@pytest.fixture(scope="module")
def two_stage_review(app_client):  # noqa: ANN001, ANN201
    """A two-stage review whose two exclusions happened at DIFFERENT stages.

    Module-scoped: every assertion below is about the state this one run
    produced, and rebuilding it per test would be a different review.

    Stage 1 — title/abstract, both reviewers, unanimous:

        P1 exclude(wrong_population)    P2 include    P3 include

    Stage 2 — full text. P1 was excluded at the abstract, was never retrieved,
    and acquires no decision — `create_assignments` no longer assigns it either
    (`ERS #42`: stage order N > 1 is scoped to records that settled `included`
    at stage N-1), so there is no queue slot for it to appear in and nothing to
    decide:

        P2 exclude(not_retrievable)     P3 include

    No conflicts anywhere. This is the plainest possible two-stage review, and
    it is the shape a PRISMA diagram is drawn from: 3 identified, 1 excluded at
    screening, 2 assessed for eligibility, 1 excluded with a reason, 1 included.
    """
    review_id = _build_review(
        app_client,
        stage_definitions=[
            {"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2},
            {"key": "full-text", "name": "Full text", "reviewers_required": 2},
        ],
        title="Two-stage PRISMA reporting",
    )

    stages = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()
    assert [s["stage_key"] for s in stages["items"]] == ["title-abstract", "full-text"], stages
    ta_stage = stages["items"][0]["stage_id"]
    ft_stage = stages["items"][1]["stage_id"]

    assigned = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ta_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned.status_code == 201, assigned.text

    queue = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={ta_stage}", headers=headers(REVIEWER_A)
    ).json()
    records = [item["review_record_id"] for item in queue["items"]]
    assert len(records) == 3, queue
    p1, p2, p3 = records

    plan_ta = [(p1, "exclude", TA_REASON), (p2, "include", None), (p3, "include", None)]
    for reviewer in (REVIEWER_A, REVIEWER_B):
        for record_id, disposition, reason in plan_ta:
            _decide(
                app_client,
                review_id,
                stage_id=ta_stage,
                record_id=record_id,
                reviewer=reviewer,
                disposition=disposition,
                reason=reason,
            )

    assigned_ft = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": ft_stage, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned_ft.status_code == 201, assigned_ft.text

    plan_ft = [(p2, "exclude", FT_REASON), (p3, "include", None)]
    for reviewer in (REVIEWER_A, REVIEWER_B):
        for record_id, disposition, reason in plan_ft:
            _decide(
                app_client,
                review_id,
                stage_id=ft_stage,
                record_id=record_id,
                reviewer=reviewer,
                disposition=disposition,
                reason=reason,
            )

    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    assert conflicts["total"] == 0, f"the fixture must be unanimous throughout; a conflict voids it: {conflicts}"

    return {
        "review_id": review_id,
        "title_abstract_stage_id": ta_stage,
        "full_text_stage_id": ft_stage,
        "records": {"p1": p1, "p2": p2, "p3": p3},
    }


@pytest.fixture(scope="module")
def single_stage_review(app_client):  # noqa: ANN001, ANN201
    """The common case. Every review that existed before `fr` #822 is this one."""
    review_id = _build_review(
        app_client,
        stage_definitions=[{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2}],
        title="Single-stage PRISMA reporting",
    )
    stage_id = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()["items"][0]["stage_id"]
    app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    queue = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}", headers=headers(REVIEWER_A)
    ).json()
    records = [item["review_record_id"] for item in queue["items"]]
    assert len(records) == 3, queue
    plan = [(records[0], "exclude", TA_REASON), (records[1], "exclude", FT_REASON), (records[2], "include", None)]
    for reviewer in (REVIEWER_A, REVIEWER_B):
        for record_id, disposition, reason in plan:
            _decide(
                app_client,
                review_id,
                stage_id=stage_id,
                record_id=record_id,
                reviewer=reviewer,
                disposition=disposition,
                reason=reason,
            )
    return {"review_id": review_id, "stage_id": stage_id, "records": records}


# ---------------------------------------------------------------------------
# 1. THE DEFECT
# ---------------------------------------------------------------------------


def test_the_full_text_breakdown_carries_only_the_full_text_reason(app_client, two_stage_review):
    """THE DEFECT, as PRISMA states it.

    One record was excluded at title/abstract for `wrong_population` and a
    DIFFERENT record was excluded at full text for `not_retrievable`. PRISMA
    reports those as two separate numbers, and the full-text one carries its
    reason. This service reported `{"not_retrievable": 1, "wrong_population": 1}`
    and nothing else — one undifferentiated block from which the required
    diagram cannot be drawn.
    """
    body = _flow(app_client, two_stage_review["review_id"])
    counts = body["counts"]
    by_stage = _by_stage(body)

    # The premise: the merged tally really does hold both stages' reasons.
    assert counts["exclusion_reasons"] == {FT_REASON: 1, TA_REASON: 1}, (
        f"the fixture is void unless both reasons are in the flat tally: {counts}"
    )

    assert "full-text" in by_stage, (
        "GET /flow carries no per-stage breakdown, so the ONLY exclusion tally a PRISMA author "
        f"can read is the merged one: {counts['exclusion_reasons']}. That reports a record "
        f"excluded at TITLE/ABSTRACT ({TA_REASON}) and a record excluded at FULL TEXT "
        f"({FT_REASON}) as one block. PRISMA 2020 requires them separately, with the full-text "
        f"exclusions broken down by reason. body keys={sorted(body)}"
    )

    full_text = by_stage["full-text"]
    assert full_text["exclusion_reasons"] == {FT_REASON: 1}, (
        f"the full-text eligibility breakdown must contain the full-text reason and nothing else; "
        f"got {full_text['exclusion_reasons']}"
    )
    assert TA_REASON not in full_text["exclusion_reasons"], (
        "a record excluded from its ABSTRACT was never retrieved and cannot appear among "
        "'reports excluded' at full text"
    )
    assert full_text["settled_exclude"] == 1, full_text
    assert full_text["settled_include"] == 1, full_text
    assert full_text["settled_uncertain"] == 0, full_text


def test_the_title_abstract_breakdown_carries_only_its_own_reason(app_client, two_stage_review):
    """The other half. Without it the fix could pass by reporting stage 2 only."""
    body = _flow(app_client, two_stage_review["review_id"])
    by_stage = _by_stage(body)

    assert "title-abstract" in by_stage, f"no title/abstract entry: {body.get('counts_by_stage')!r}"
    title_abstract = by_stage["title-abstract"]

    assert title_abstract["exclusion_reasons"] == {TA_REASON: 1}, (
        f"the title/abstract breakdown must contain its own reason and nothing else; "
        f"got {title_abstract['exclusion_reasons']}"
    )
    assert FT_REASON not in title_abstract["exclusion_reasons"], (
        "a record excluded at FULL TEXT passed title/abstract and must not be counted as excluded at title/abstract"
    )
    assert title_abstract["settled_exclude"] == 1, title_abstract
    assert title_abstract["settled_include"] == 0, (
        "a record that PASSED title/abstract is not INCLUDED by the review — it advanced. "
        "Only the stage it settled at may claim it."
    )


def test_reached_is_the_reports_assessed_for_eligibility_number(app_client, two_stage_review):
    """PRISMA box 16 — 'reports assessed for eligibility' — is `reached`.

    Three records were screened at title/abstract; one was excluded there and
    never retrieved, so TWO reports were assessed for eligibility. Without this
    number the diagram has no arrow between the two stages, and it cannot be
    derived from the buckets: a stage's `included + excluded + uncertain` counts
    only the records that SETTLED there, which for stage 1 is the one exclusion.
    """
    body = _flow(app_client, two_stage_review["review_id"])
    by_stage = _by_stage(body)
    assert set(by_stage) == {"title-abstract", "full-text"}, body.get("counts_by_stage")

    assert by_stage["title-abstract"]["reached"] == 3, by_stage["title-abstract"]
    assert by_stage["full-text"]["reached"] == 2, (
        "P1 was excluded from its abstract and never retrieved; it must not be counted among "
        f"the reports assessed for eligibility: {by_stage['full-text']}"
    )


def test_the_stages_come_back_in_stage_order_with_their_names(app_client, two_stage_review):
    """A diagram is drawn top to bottom, so the order is part of the answer."""
    body = _flow(app_client, two_stage_review["review_id"])
    entries = body.get("counts_by_stage")
    assert isinstance(entries, list), f"counts_by_stage must be an ordered list, not {type(entries).__name__}"

    assert [entry["stage_key"] for entry in entries] == ["title-abstract", "full-text"], entries
    assert [entry["stage_order"] for entry in entries] == [1, 2], entries
    assert [entry["name"] for entry in entries] == ["Title/abstract", "Full text"], (
        "the stage's own name is what a diagram is labelled with; a key is not a label"
    )


# ---------------------------------------------------------------------------
# 2. THE CONSERVATION IDENTITIES
# ---------------------------------------------------------------------------


def test_the_per_stage_tallies_add_up_to_the_flat_ones(app_client, two_stage_review):
    """The identity that makes the split trustworthy rather than merely present.

    A per-stage breakdown that does not reconcile with the sealed `counts` is
    worse than none: it is a second set of numbers for the same review, and a
    reader has no way to tell which is the ledger. Every screened record settles
    at exactly ONE stage, so the sums are equalities, not bounds.
    """
    body = _flow(app_client, two_stage_review["review_id"])
    counts = body["counts"]
    entries = body["counts_by_stage"]

    assert sum(e["settled_include"] for e in entries) == counts["included"], (counts, entries)
    assert sum(e["settled_exclude"] for e in entries) == counts["excluded"], (counts, entries)
    assert sum(e["settled_uncertain"] for e in entries) == counts["uncertain"], (counts, entries)
    assert (
        sum(e["settled_include"] + e["settled_exclude"] + e["settled_uncertain"] for e in entries)
        == counts["records_screened"]
    ), (
        counts,
        entries,
    )

    merged: dict[str, int] = {}
    for entry in entries:
        for code, tally in entry["exclusion_reasons"].items():
            merged[code] = merged.get(code, 0) + tally
    assert merged == counts["exclusion_reasons"], (
        f"the per-stage reasons must partition the flat one exactly; {merged} != {counts['exclusion_reasons']}"
    )

    # The identity `flow_counts.$comment` states as prose. It holds here because
    # every exclusion in this fixture carries a reason; it is asserted on the
    # fixture, not claimed as a universal law of the schema.
    assert sum(counts["exclusion_reasons"].values()) == counts["excluded"], counts
    for entry in entries:
        assert sum(entry["exclusion_reasons"].values()) == entry["settled_exclude"], entry


def test_a_records_own_stage_tallies_never_exceed_what_reached_them(app_client, two_stage_review):
    """A stage cannot settle more records than reached it."""
    body = _flow(app_client, two_stage_review["review_id"])
    for entry in body["counts_by_stage"]:
        settled = entry["settled_include"] + entry["settled_exclude"] + entry["settled_uncertain"]
        assert settled <= entry["reached"], entry


# ---------------------------------------------------------------------------
# 3. THE REGRESSION THAT WOULD SILENCE EVERY PROJECTION
# ---------------------------------------------------------------------------

#: MEASURED ON THE UNMODIFIED SOURCE, at 25d29e9, against the fixtures below.
#: These are the values the pre-change derivation produced, pasted here as
#: literals so that the assertion can fail. Comparing the code against itself —
#: recomputing the hash from the counts the same run returned — would agree no
#: matter what either of them said.
#:
#: `canonical_counts_hash` is what `flow_projections` stores and what
#: `flow:rebuild` compares. Moving it stales every cached projection in the
#: table at once and turns `projection_stale` true for every review in the
#: fleet, which is a fleet-wide false alarm about the integrity of the ledger.
TWO_STAGE_COUNTS = {
    "identified_total": 3,
    "quarantined": 0,
    "rejected": 0,
    "records_total": 3,
    "duplicates_removed": 0,
    "records_screened": 3,
    "not_yet_screened": 0,
    "included": 1,
    "excluded": 2,
    "uncertain": 0,
    "exclusion_reasons": {FT_REASON: 1, TA_REASON: 1},
    "adjudicated": 0,
}
TWO_STAGE_COUNTS_HASH = "sha256:71752d693ecd1bb6fc29d17678129959f029569a763d3e0cbef999ddde0a162f"

SINGLE_STAGE_COUNTS = {
    "identified_total": 3,
    "quarantined": 0,
    "rejected": 0,
    "records_total": 3,
    "duplicates_removed": 0,
    "records_screened": 3,
    "not_yet_screened": 0,
    "included": 1,
    "excluded": 2,
    "uncertain": 0,
    "exclusion_reasons": {FT_REASON: 1, TA_REASON: 1},
    "adjudicated": 0,
}
SINGLE_STAGE_COUNTS_HASH = "sha256:71752d693ecd1bb6fc29d17678129959f029569a763d3e0cbef999ddde0a162f"


def test_the_flat_counts_and_their_hash_did_not_move(app_client, two_stage_review):
    """`counts` is byte-identical to what the unmodified source produced.

    Both halves matter and neither implies the other. The dict pins the twelve
    members — a thirteenth would be refused by `flow_counts`'s
    `additionalProperties: false` in every already-sealed export's schema — and
    the hash pins the canonical bytes, which is the value `flow_projections`
    compares and the value the sealed manifest carries.
    """
    body = _flow(app_client, two_stage_review["review_id"])

    assert body["counts"] == TWO_STAGE_COUNTS, (
        "the flat counts moved; every sealed export's counts block and every cached projection "
        f"is now describing a different derivation.\ngot      {body['counts']}\nexpected {TWO_STAGE_COUNTS}"
    )
    assert sorted(body["counts"]) == sorted(TWO_STAGE_COUNTS), "a thirteenth member of `counts` is a contract break"
    assert body["canonical_counts_hash"] == TWO_STAGE_COUNTS_HASH, (
        "canonical_counts_hash moved, so every flow_projection row in the table is now stale and "
        f"every review reports projection_stale: got {body['canonical_counts_hash']}, "
        f"expected {TWO_STAGE_COUNTS_HASH}"
    )


def test_the_common_case_did_not_move(app_client, single_stage_review):
    """A single-stage review — every review that existed before `fr` #822.

    Its counts and its hash are pinned exactly as the two-stage ones are, and
    its per-stage block is the whole review: one stage, reached by everything,
    with the merged tally and the per-stage tally necessarily identical. That
    identity is the reason this defect went unnoticed for as long as it did.
    """
    body = _flow(app_client, single_stage_review["review_id"])

    assert body["counts"] == SINGLE_STAGE_COUNTS, f"got      {body['counts']}\nexpected {SINGLE_STAGE_COUNTS}"
    assert body["canonical_counts_hash"] == SINGLE_STAGE_COUNTS_HASH, body["canonical_counts_hash"]

    entries = body["counts_by_stage"]
    assert len(entries) == 1, entries
    only = entries[0]
    assert only["stage_key"] == "title-abstract", only
    assert only["reached"] == 3, only
    assert only["exclusion_reasons"] == body["counts"]["exclusion_reasons"], (
        "on a one-stage review the split and the merge ARE the same numbers — which is exactly "
        "why nobody noticed the merge until a second stage existed"
    )


def test_the_flat_block_alone_cannot_tell_the_two_reviews_apart(app_client, two_stage_review, single_stage_review):
    """The defect stated as an indistinguishability, which is the sharpest form.

    One review screened three records in a single pass. The other screened them
    through two stages and excluded one at each. Those are different studies
    with different PRISMA diagrams, and the flat block — every one of its twelve
    members, and therefore its canonical hash — is byte-identical for both. A
    consumer handed only `counts` cannot draw either diagram, because it cannot
    tell which review it is holding.

    `counts_by_stage` is what separates them, and it must, or the fix is
    cosmetic.
    """
    two_stage = _flow(app_client, two_stage_review["review_id"])
    single_stage = _flow(app_client, single_stage_review["review_id"])

    assert two_stage["counts"] == single_stage["counts"], (
        "the premise: two genuinely different reviews whose flat blocks agree exactly"
    )
    assert two_stage["canonical_counts_hash"] == single_stage["canonical_counts_hash"], (
        two_stage["canonical_counts_hash"],
        single_stage["canonical_counts_hash"],
    )

    assert two_stage["counts_by_stage"] != single_stage["counts_by_stage"], (
        "a per-stage block that agrees for a one-stage and a two-stage review is not "
        f"reporting the stages at all: {two_stage['counts_by_stage']}"
    )
    assert len(two_stage["counts_by_stage"]) == 2, two_stage["counts_by_stage"]
    assert len(single_stage["counts_by_stage"]) == 1, single_stage["counts_by_stage"]


def test_rebuild_still_reproduces_the_same_hash(app_client, two_stage_review):
    """`flow:rebuild` recomputes from zero; the hash must reproduce (`01-…:510`).

    Adding a sibling key to the response must not reach the projection cache.
    """
    response = app_client.post(f"/api/reviews/{two_stage_review['review_id']}/flow:rebuild", headers=lead_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["canonical_counts_hash"] == TWO_STAGE_COUNTS_HASH, body
    assert body["matches_previous_projection"] is True, body
    assert body["counts"] == TWO_STAGE_COUNTS, body


# ---------------------------------------------------------------------------
# 4. THE OMISSIONS ARRAY
# ---------------------------------------------------------------------------


def _seal(client, review_id) -> dict[str, Any]:  # noqa: ANN001
    created = client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert created.status_code == 201, created.text
    return created.json()


def test_every_omission_names_the_stage_it_was_settled_at(app_client, two_stage_review):
    """`ER-009.3` — the one place a consumer can rebuild the split themselves.

    The stage was already on the object: `RecordDisposition.settled_at` is
    populated by `derive` and, before this change, `grep -rn settled_at src/`
    found no consumer at all. The export wrote four of the five fields beside it
    and dropped the one that carries the stage.
    """
    sealed = _seal(app_client, two_stage_review["review_id"])
    export = app_client.get(
        f"/api/reviews/{two_stage_review['review_id']}/exports/{sealed['export_id']}", headers=lead_headers()
    )
    assert export.status_code == 200, export.text
    omissions = export.json()["payload"]["omissions"]

    records = two_stage_review["records"]
    by_record = {o["review_record_id"]: o for o in omissions if "review_record_id" in o}
    assert set(by_record) == {records["p1"], records["p2"]}, omissions

    p1 = by_record[records["p1"]]
    assert p1["exclusion_reason_code"] == TA_REASON, p1
    assert p1["settled_at_stage_key"] == "title-abstract", (
        f"the omission for a record excluded at title/abstract must say so; got {p1}"
    )
    assert p1["settled_at_stage_order"] == 1, p1

    p2 = by_record[records["p2"]]
    assert p2["exclusion_reason_code"] == FT_REASON, p2
    assert p2["settled_at_stage_key"] == "full-text", (
        f"the omission for a record excluded at full text must say so; got {p2}"
    )
    assert p2["settled_at_stage_order"] == 2, p2

    # And the four fields that were already there are untouched.
    assert set(p1) == {
        "review_record_id",
        "bucket",
        "exclusion_reason_code",
        "adjudicated",
        "settled_at_stage_key",
        "settled_at_stage_order",
    }, p1


def test_a_freshly_sealed_export_still_verifies(app_client, two_stage_review):
    """The negative control for the section below: the NEW shape seals soundly."""
    sealed = _seal(app_client, two_stage_review["review_id"])
    verified = app_client.get(
        f"/api/reviews/{two_stage_review['review_id']}/exports/{sealed['export_id']}:verify", headers=lead_headers()
    )
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["outcome"] == "verified", body
    assert body["items_match"] is True, body
    assert body["verified"] is True, body


def test_an_export_sealed_under_the_old_omission_shape_still_verifies(app_client, two_stage_review):
    """Already-sealed exports are not retroactively broken by the new shape.

    Not reasoned about — reconstructed. The stored payload is put back to the
    PRE-change omission shape (the four fields, no stage), `items_hash` is
    recomputed over that older cargo through the same one function the writer
    and the verifier share, the manifest is re-sealed through the real
    `_seal_manifest`, and the row is inserted as a second, independent export.
    That is genuinely what a row written last week looks like.

    It verifies because `verify_export` recomputes `items_hash` from
    `export.payload` rather than from a fresh derivation, and `counts_match`
    compares array LENGTHS, which the added members do not move. If either of
    those ever stopped being true, every export sealed before this change would
    start reporting `tampered` — an accusation about an evidence artefact in a
    regulated review — and this test is what says so.

    It lands in its OWN review because `uq_export_manifest_per_review` is UNIQUE
    on `(review_id, manifest_hash)` and a differing `items_hash` is the only
    thing separating this manifest from the live one.
    """
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from sqlalchemy import create_engine, text

    from application.services.flow import _seal_manifest, items_hash

    sealed = _seal(app_client, two_stage_review["review_id"])
    stored = app_client.get(
        f"/api/reviews/{two_stage_review['review_id']}/exports/{sealed['export_id']}", headers=lead_headers()
    ).json()
    payload = deepcopy(stored["payload"])

    #: The pre-change omission shape, exactly: the four members the writer
    #: emitted before the stage was added, in the order it emitted them.
    old_shape = [
        {key: value for key, value in entry.items() if not key.startswith("settled_at_stage_")}
        for entry in payload["omissions"]
    ]
    assert old_shape != payload["omissions"], (
        "this fixture reconstructs a row from BEFORE the stage was added to each omission; "
        "if the two shapes are identical it is reconstructing nothing"
    )
    payload["omissions"] = old_shape
    payload["manifest"]["items_hash"] = items_hash(payload["included"], old_shape)
    assert payload["manifest"]["items_hash"] != stored["payload"]["manifest"]["items_hash"], (
        "the item seal must genuinely differ between the two shapes, otherwise this proves nothing"
    )

    manifest_hash, sealed_with = _seal_manifest(payload["manifest"], payload["manifest"]["schema_ref"])
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_digest = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"

    home_review_id = _build_review(
        app_client,
        stage_definitions=[{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2}],
        title="Home for the reconstructed pre-change export",
    )
    export_id = str(uuid.uuid4())

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        model = (
            conn.execute(
                text(
                    "SELECT flow_revision_id, corpus_version_id, tenant_id, created_by_subject_id "
                    "FROM inclusion_exports WHERE export_id = :e"
                ),
                {"e": sealed["export_id"]},
            )
            .mappings()
            .one()
        )
        # FORCE ROW LEVEL SECURITY is on every table here: a read with no
        # `app.current_tenant` matches nothing and reports success. This row
        # existing is what proves the GUC above actually bound.
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
                "schema_ref": payload["manifest"]["schema_ref"],
                "manifest_hash": manifest_hash,
                "sealed_with": sealed_with,
                "payload_digest": payload_digest,
                "payload": json.dumps(payload),
                "omissions": json.dumps(old_shape),
                "subject": model["created_by_subject_id"],
            },
        )

    verified = app_client.get(f"/api/reviews/{home_review_id}/exports/{export_id}:verify", headers=lead_headers())
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["items_match"] is True, f"a pre-change export must not be reported as substituted: {body}"
    assert body["counts_match"] is True, f"the added members must not move omissions_count: {body}"
    assert body["payload_matches"] is True, body
    assert body["manifest_matches"] is True, body
    assert body["outcome"] == "verified", body
    assert body["verified"] is True, body


def test_the_omission_shape_change_mints_a_second_export_across_the_deploy_boundary(app_client, monkeypatch):
    """A RECORDED CONSEQUENCE OF THIS CHANGE, not an oversight and not a bug to fix.

    `POST /exports` collapses a re-seal that would add nothing, keyed on
    `manifest_hash` — `test_an_unchanged_review_seals_one_export` is the test
    that pins it. `items_hash` is a member of the manifest, and this change moves
    the omissions cargo, so a review sealed by the PREVIOUS release and re-sealed
    by this one produces a different manifest and therefore a SECOND permanent,
    undeletable export rather than a replay.

    No existing test could see it: every one of them seals and re-seals inside a
    single process, where both seals use the same code. This one crosses the
    boundary by substituting the previous release's `_omission_row` — the real
    previous expression, verbatim, not an approximation — sealing, restoring the
    current one, and sealing again.

    ## Why it is not "fixed"

    Holding `items_hash` stable over changed bytes would mean an item seal that
    no longer describes its cargo, which is the entire defect `:2` was released
    to close. The honest options are "the hash moves" or "the shape does not
    change", and the shape change is what makes the export PRISMA-reportable.

    ## Why that makes the case for landing it NOW

    The cost is paid once, at the boundary, in proportion to how many exports
    already exist — and production holds ZERO: `inclusion_exports 0`,
    `screening_decisions 0`, `reviews 0`, `review_records 0`. Nothing has ever
    been sealed. Every day this waits the boundary gets more expensive, which is
    an argument for shipping rather than against it.

    The half that would have been serious is asserted too: the row sealed under
    the old shape still VERIFIES after the deploy. A shape change that
    invalidated existing seals would be an accusation of tampering against an
    evidence artefact, and that is the thing that must never happen.
    """
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from application.services import flow as flow_svc

    def previous_release_omission_row(disposition):  # noqa: ANN001, ANN202
        """`main`'s expression, verbatim — the four members it wrote."""
        return {
            "review_record_id": str(disposition.review_record_id),
            "bucket": disposition.bucket,
            "exclusion_reason_code": disposition.exclusion_reason_code,
            "adjudicated": disposition.adjudicated,
        }

    review_id = _build_review(
        app_client,
        stage_definitions=[{"key": "title-abstract", "name": "Title/abstract", "reviewers_required": 2}],
        title="Deploy boundary",
    )
    stage_id = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()["items"][0]["stage_id"]
    app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    queue = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}", headers=headers(REVIEWER_A)
    ).json()
    records = [item["review_record_id"] for item in queue["items"]]
    for reviewer in (REVIEWER_A, REVIEWER_B):
        _decide(
            app_client,
            review_id,
            stage_id=stage_id,
            record_id=records[0],
            reviewer=reviewer,
            disposition="exclude",
            reason=TA_REASON,
        )
        _decide(
            app_client, review_id, stage_id=stage_id, record_id=records[1], reviewer=reviewer, disposition="include"
        )
        _decide(
            app_client, review_id, stage_id=stage_id, record_id=records[2], reviewer=reviewer, disposition="include"
        )

    # ---- sealed by the PREVIOUS release ----------------------------------
    monkeypatch.setattr(flow_svc, "_omission_row", previous_release_omission_row)
    before = _seal(app_client, review_id)
    assert before["replayed"] is False, before
    old_payload = app_client.get(
        f"/api/reviews/{review_id}/exports/{before['export_id']}", headers=lead_headers()
    ).json()["payload"]
    assert [sorted(o) for o in old_payload["omissions"]] == [
        ["adjudicated", "bucket", "exclusion_reason_code", "review_record_id"]
    ], f"the fixture must seal the PREVIOUS shape or it proves nothing: {old_payload['omissions']}"

    # A replay under the SAME release is still a replay — the control that says
    # the second seal below is caused by the deploy and not by re-sealing at all.
    again = _seal(app_client, review_id)
    assert again["replayed"] is True, again
    assert again["export_id"] == before["export_id"], again

    # ---- the deploy ------------------------------------------------------
    monkeypatch.undo()
    after = _seal(app_client, review_id)

    assert after["replayed"] is False, (
        "MEASURED CONSEQUENCE: the omissions cargo changed, so `items_hash` changed, so "
        f"`manifest_hash` changed, so the replay key missed. {after}"
    )
    assert after["export_id"] != before["export_id"], after
    assert after["manifest_hash"] != before["manifest_hash"], (before["manifest_hash"], after["manifest_hash"])

    listed = app_client.get(f"/api/reviews/{review_id}/exports", headers=lead_headers()).json()
    assert len(listed["items"]) == 2, (
        f"one review, one unchanged inclusion set, two permanent append-only seals: {listed}"
    )

    # ---- and the pre-deploy row is NOT retroactively broken ---------------
    for export_id in (before["export_id"], after["export_id"]):
        body = app_client.get(f"/api/reviews/{review_id}/exports/{export_id}:verify", headers=lead_headers()).json()
        assert body["outcome"] == "verified", (export_id, body)
        assert body["items_match"] is True, (export_id, body)
        assert body["counts_match"] is True, (export_id, body)
