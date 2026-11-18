"""E2E-002 — the acceptance run, and every negative control.

`01-…` and the plan's verification section: import -> reversible dedup -> blinded
dual screening -> conflict and adjudication -> flow and export, **accounting for
every record**, with each negative control shown to fail.

This is the suite the shadow-smoke run's evidence bundle points at. It runs
against a real PostgreSQL because every guarantee it checks is a database
guarantee — a SQLite or in-memory double would pass while proving nothing.

The runtime role is used deliberately (`evidence_review_app`, a NON-owner) so the
blinding assertions are made against the same privileges production runs with.
An owner connection would still be subject to `FORCE ROW LEVEL SECURITY`, but it
could disable it, and a test that could disable the thing it is testing is not a
test.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.tenants import tenant

#: The RUNTIME role — a non-owner. Every probe that asserts a constraint holds
#: uses this one, because it is the role production runs as.
DB_URL = os.getenv("DATABASE_URL", "")
#: The OWNER role. Used by exactly one probe — the export tamper — which has to
#: disable a trigger to simulate a tamper the API cannot perform.
OWNER_DB_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-e2e")
OTHER_TENANT = tenant("tenant-other")
LEAD = "lead-carol"
REVIEWER_A = "reviewer-alice"
REVIEWER_B = "reviewer-bob"

#: Two records that are genuinely the same work (shared DOI), one unique record,
#: one row with no title (quarantine), and one regulatory PDF as an ordinary
#: report — the `DM-AT-18` POSITIVE control.
CORPUS = {
    "records": [
        {
            "title": "Tocilizumab in hospitalised patients with COVID-19",
            "doi": "10.1016/S0140-6736(21)00676-0",
            "pmid": "33933206",
            "year": 2021,
            "abstract": "A randomised controlled trial.",
            "authors": ["RECOVERY Collaborative Group"],
            "journal": "The Lancet",
        },
        {
            # Same DOI, different casing and a URL prefix — the dedup baseline
            # must normalize both into one cluster.
            "title": "Tocilizumab in Hospitalised Patients With COVID-19",
            "doi": "https://doi.org/10.1016/S0140-6736(21)00676-0",
            "year": 2021,
            "journal": "Lancet",
        },
        {
            "title": "Dexamethasone in hospitalised patients with COVID-19",
            "doi": "10.1056/NEJMoa2021436",
            "pmid": "32678530",
            "year": 2021,
        },
        {
            "title": "A health technology assessment of remdesivir",
            "record_type": "report",
            "year": 2022,
        },
        {"doi": "10.9999/no-title", "year": 2020},
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
    """A locked, activated review with a sealed corpus, deduped and screened.

    Module-scoped: the later assertions are about the *state this run produced*,
    so rebuilding it per test would be both slow and a different review.
    """
    external_key = f"e2e-{uuid.uuid4()}"
    created = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": external_key, "title": "Shadow smoke"},
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
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population", "not_retrievable"],
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
        json={"protocol_version_id": protocol_id, "rationale": "shadow smoke run"},
        headers=lead_headers(),
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "active"

    imported = app_client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text

    sealed = app_client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())
    assert sealed.status_code == 201, sealed.text

    stages = app_client.get(f"/api/reviews/{review_id}/stages", headers=lead_headers()).json()
    stage_id = stages["items"][0]["stage_id"]

    return {
        "review_id": review_id,
        "protocol_version_id": protocol_id,
        "stage_id": stage_id,
        "import": imported.json(),
        "corpus": sealed.json(),
    }


# ---------------------------------------------------------------------------
# ER-002 — import, quarantine, sealed corpus
# ---------------------------------------------------------------------------


def test_import_accepts_valid_rows_and_quarantines_the_malformed_one(review):
    batch = review["import"]
    assert batch["accepted_count"] == 4
    assert batch["quarantined_count"] == 1, "the row with no title must be quarantined, not dropped"


def test_a_malformed_row_does_not_discard_the_valid_ones(app_client, review):
    """`01-…:421`. The quarantined row keeps its original bytes."""
    quarantine = app_client.get(f"/api/reviews/{review['review_id']}/quarantine", headers=lead_headers()).json()
    assert quarantine["count"] == 1
    row = quarantine["items"][0]
    assert "title" in row["quarantine_reason"]
    assert row["raw_representation"]["doi"] == "10.9999/no-title"


def test_regulatory_pdf_imports_as_an_ordinary_report(app_client, review):
    """`DM-AT-18` POSITIVE control — the half that must SUCCEED."""
    records = app_client.get(f"/api/reviews/{review['review_id']}/records", headers=lead_headers()).json()
    reports = [r for r in records["items"] if r["record_type"] == "report"]
    assert len(reports) == 1
    assert "health technology assessment" in reports[0]["title"].lower()


def test_the_corpus_seal_reproduces_on_recompute(app_client, review):
    """`ER-V011` / `DM-AT-05`."""
    members = app_client.get(
        f"/api/reviews/{review['review_id']}/corpus-versions/{review['corpus']['corpus_version_id']}/members",
        headers=lead_headers(),
    ).json()
    assert members["count"] == 4
    assert members["member_manifest_hash"] == members["recomputed_manifest_hash"]
    assert members["member_manifest_hash"].startswith("sha256:")
    assert [m["member_ordinal"] for m in members["items"]] == [1, 2, 3, 4]


def test_reimporting_identical_bytes_is_a_replay_not_a_second_import(app_client, review):
    """Worker death / client retry creates no duplicate record (`01-…:422`)."""
    again = app_client.post(
        f"/api/reviews/{review['review_id']}/imports?source_kind=canonical_json",
        content=json.dumps(CORPUS),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert again.status_code == 201
    assert again.json()["replayed"] is True
    assert again.json()["import_batch_id"] == review["import"]["import_batch_id"]


# ---------------------------------------------------------------------------
# ER-003 — reversible deduplication
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dedup(app_client, review):  # noqa: ANN201, ANN001
    run = app_client.post(f"/api/reviews/{review['review_id']}/deduplication-runs", json={}, headers=lead_headers())
    assert run.status_code == 201, run.text
    clusters = app_client.get(f"/api/reviews/{review['review_id']}/duplicate-clusters", headers=lead_headers()).json()
    return {"run": run.json(), "clusters": clusters}


def test_the_shared_doi_produces_exactly_one_cluster(dedup):
    assert dedup["run"]["cluster_count"] == 1
    cluster = dedup["clusters"]["items"][0]
    assert cluster["member_count"] == 2
    assert {m["doi"] for m in cluster["members"]} == {
        "10.1016/S0140-6736(21)00676-0",
        "https://doi.org/10.1016/S0140-6736(21)00676-0",
    }, "normalization must fold the URL-prefixed DOI onto the bare one"


def test_an_identical_rerun_is_the_same_run(app_client, review, dedup):
    """`01-…:454` — exact input/config digest gives repeatable output."""
    again = app_client.post(f"/api/reviews/{review['review_id']}/deduplication-runs", json={}, headers=lead_headers())
    assert again.json()["replayed"] is True
    assert again.json()["input_digest"] == dedup["run"]["input_digest"]


def test_merge_split_merge_is_reversible_and_attributable(app_client, review, dedup):
    """`01-…:456`. Three rows, one chain, zero source records mutated."""
    review_id = review["review_id"]
    cluster = dedup["clusters"]["items"][0]
    cluster_id = cluster["cluster_id"]
    survivor = cluster["members"][0]["review_record_id"]

    before = app_client.get(f"/api/reviews/{review_id}/records", headers=lead_headers()).json()["total"]

    merge = app_client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}:resolve",
        json={
            "resolution_type": "merge",
            "rationale": "same DOI, same trial",
            "survivor_record_id": survivor,
        },
        headers=lead_headers(),
    )
    assert merge.status_code == 200, merge.text

    split = app_client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}:resolve",
        json={"resolution_type": "split", "rationale": "second look: different populations"},
        headers=lead_headers(),
    )
    assert split.status_code == 200, split.text
    assert split.json()["supersedes_resolution_id"] == merge.json()["resolution_id"]

    remerge = app_client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}:resolve",
        json={
            "resolution_type": "merge",
            "rationale": "confirmed duplicate after checking the registry entry",
            "survivor_record_id": survivor,
        },
        headers=lead_headers(),
    )
    assert remerge.status_code == 200, remerge.text

    chain = app_client.get(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}/resolutions", headers=lead_headers()
    ).json()
    assert chain["count"] == 3
    assert [r["resolution_type"] for r in chain["items"]] == ["merge", "split", "merge"]
    assert all(r["decided_by_subject_id"] == LEAD for r in chain["items"])

    after = app_client.get(f"/api/reviews/{review_id}/records", headers=lead_headers()).json()["total"]
    assert after == before, "no source record may be deleted or mutated by a merge"


# ---------------------------------------------------------------------------
# ER-004 — blinded dual screening
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def screened(app_client, review, dedup):  # noqa: ANN201, ANN001
    """Two reviewers screen every record; one record is a deliberate conflict."""
    review_id, stage_id = review["review_id"], review["stage_id"]

    assigned = app_client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER_A, REVIEWER_B]},
        headers=lead_headers(),
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["assignments_created"] == 8  # 4 records x 2 reviewers

    queue_a = app_client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}", headers=headers(REVIEWER_A)
    ).json()
    records = [item["review_record_id"] for item in queue_a["items"]]

    # THE QUEUE SERVES EXACTLY THE RECORDS STILL IN THE REVIEW, and that is
    # asserted against the ledger rather than against a constant.
    #
    # It used to be `== 4`, which was the corpus size and ignored deduplication:
    # the queue served records a merge had already removed, the reviewer judged
    # them, and `derive` discarded the verdict as `duplicate_removed`. Now they
    # are filtered out — so the expected length depends on whether an earlier
    # test in this module has left a merge standing, and a constant here would
    # pass or fail on test ORDER. Deriving it from `/flow` states the rule
    # instead, and a queue that went empty would fail the `>= 3` floor below
    # rather than sail through as "nothing to screen".
    counts = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers()).json()["counts"]
    expected = counts["records_total"] - counts["duplicates_removed"]
    assert len(records) == expected, (
        f"the queue served {len(records)} records while the ledger says {expected} are still in the review: {counts}"
    )
    assert expected >= 3, f"this fixture needs at least three screenable records to plan a conflict: {counts}"

    # A and B agree on everything except the LAST record, where they disagree.
    plan_a = ["include"] * (len(records) - 1) + ["include"]
    plan_b = ["include"] * (len(records) - 1) + ["exclude"]
    for record_id, disposition in zip(records, plan_a, strict=True):
        body = {"stage_id": stage_id, "review_record_id": record_id, "disposition": disposition}
        if disposition == "exclude":
            body["exclusion_reason_code"] = "wrong_design"
        r = app_client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(REVIEWER_A))
        assert r.status_code == 201, r.text

    for record_id, disposition in zip(records, plan_b, strict=True):
        body = {"stage_id": stage_id, "review_record_id": record_id, "disposition": disposition}
        if disposition == "exclude":
            body["exclusion_reason_code"] = "wrong_design"
        r = app_client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(REVIEWER_B))
        assert r.status_code == 201, r.text
        # The decision response must say NOTHING about a conflict. With a binary
        # disposition and two reviewers, "a conflict opened" is the other
        # reviewer's disposition.
        assert "conflict" not in r.text, "the decision response leaks the other reviewer's disagreement"

    # The conflict is found where it belongs: on the LEAD's route.
    conflicts = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()
    conflict_record = conflicts["items"][0]["review_record_id"] if conflicts["count"] else None

    return {"records": records, "conflict_record": conflict_record}


def test_agreement_opens_no_conflict_and_disagreement_opens_exactly_one(app_client, review, screened):
    """`01-…:484`."""
    conflicts = app_client.get(f"/api/reviews/{review['review_id']}/conflicts", headers=lead_headers()).json()
    assert conflicts["count"] == 1
    assert conflicts["items"][0]["review_record_id"] == screened["conflict_record"]


def test_reviewer_b_cannot_see_reviewer_a_s_decision(app_client, review, screened):
    """L1 + L2. The core blinding assertion."""
    visible = app_client.get(f"/api/reviews/{review['review_id']}/decisions", headers=headers(REVIEWER_B)).json()
    assert visible["count"] == len(screened["records"]), (
        "B sees exactly their own decisions, one per record the queue served them"
    )
    assert "reviewer_subject_id" not in json.dumps(visible), (
        "a reviewer is never told whose decision they are looking at"
    )


def test_the_queue_never_reveals_the_other_reviewer_s_progress(app_client, review, screened):
    """L3. `1 of 2 done` is a decision leak."""
    queue = app_client.get(
        f"/api/reviews/{review['review_id']}/screening/queue?stage_id={review['stage_id']}",
        headers=headers(REVIEWER_A),
    )
    body = queue.text
    for leak in ("decisions_so_far", "others_pending", "n_of_2", "completion", "reviewers_done"):
        assert leak not in body, f"queue payload leaks {leak}"
    payload = queue.json()
    # Both against the records the queue actually served, not a constant. The
    # denominator counts SCREENABLE assignments — a record a merge removed is not
    # work — and pinning 4 here made the counter unreachable the moment the queue
    # started filtering them out: three served, three decided, "3 of 4" forever.
    served = len(screened["records"])
    assert payload["assigned_to_me"] == served, payload
    assert payload["decided_by_me"] == served, (
        f"the reviewer decided every record they were served and the counter still says otherwise: {payload}"
    )
    assert all("my_decision" in item for item in payload["items"])


def test_conflict_dispositions_are_redacted_until_unblinded(app_client, review, screened):
    """The governed door is the only door — and the room is lead-only.

    Asserted on the LEAD. This used to read the route as reviewer A, which is
    the read that should never have succeeded: a conflict's existence is the
    other reviewer's disposition when the vocabulary is binary, so the route is
    lead-only now and the reviewer's refusal is asserted alongside the lead's
    redaction rather than instead of it.
    """
    refused = app_client.get(f"/api/reviews/{review['review_id']}/conflicts", headers=headers(REVIEWER_A))
    assert refused.status_code == 403, refused.text

    conflicts = app_client.get(f"/api/reviews/{review['review_id']}/conflicts", headers=lead_headers()).json()
    assert conflicts["items"][0]["dispositions"] == "redacted"
    assert conflicts["items"][0]["unblinded"] is False


def test_a_decision_on_an_unassigned_record_is_404_with_the_identifier_absent(app_client, review):
    """L5. `01-…:728` — assert ABSENCE, not merely the status code."""
    stranger = uuid.uuid4()
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/decisions",
        json={
            "stage_id": review["stage_id"],
            "review_record_id": str(stranger),
            "disposition": "include",
        },
        headers=headers("reviewer-nobody"),
    )
    assert response.status_code == 404, response.text
    assert str(stranger) not in response.text, "a 404 that echoes the id confirms what it denies"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_service_principal_cannot_record_a_decision(app_client, review, screened):
    """`12-…:121-124` — machines propose; humans decide."""
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/decisions",
        json={
            "stage_id": review["stage_id"],
            "review_record_id": screened["records"][0],
            "disposition": "include",
        },
        headers={"X-Tenant-Id": TENANT},  # tenant, no subject: a service principal
    )
    assert response.status_code == 403
    assert response.json()["code"] == "human_principal_required"


def test_a_correction_supersedes_rather_than_overwrites(app_client, review, screened):
    """`ER-004.5`.

    ## Two things this test used to get wrong

    **It never asserted its own last call.** The "put it back" POST below was
    fired and its status thrown away, and it had been returning **409** on every
    run: `own_decision_for` selected the chain HEAD, so the only
    `supersedes_decision_id` the service would accept was the ORIGINAL, and this
    call correctly sends the CORRECTION's id. A silent 409 left the record on
    `uncertain` while the comment said the corpus was settled. Asserted now.

    **It corrected a record the two reviewers AGREED on.** With corrections
    invisible to conflict detection that was harmless; now that the current
    decision is really the current one, `include -> uncertain` on an agreed
    record is a genuine disagreement and opens a real conflict — one that
    correcting back does NOT close, because `screening_conflicts` is append-only
    and its only resolution is an attributed adjudication. That is a live open
    question (see
    `tests/api/test_the_queue_shows_the_current_decision.py::test_a_correction_that_restores_agreement_leaves_the_conflict_open`),
    NOT something this acceptance run should be silently deciding.

    So the correction is taken on the record that is **already** the conflict —
    A `include` vs B `exclude`. `uq_conflict_once` plus `ON CONFLICT DO NOTHING`
    means no second row can open there, the review's conflict count is
    unchanged, and the supersession chain is exercised on exactly the same
    number of rows as before.
    """
    review_id, stage_id = review["review_id"], review["stage_id"]
    record_id = screened["conflict_record"]
    assert record_id is not None, "the screened fixture must have opened exactly one conflict"

    conflicts_before = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()["count"]

    before = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_A)
    ).json()["items"][0]

    blocked = app_client.post(
        f"/api/reviews/{review_id}/decisions",
        json={"stage_id": stage_id, "review_record_id": record_id, "disposition": "uncertain"},
        headers=headers(REVIEWER_A),
    )
    assert blocked.status_code == 409, "a second decision must supersede, not silently replace"

    corrected = app_client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "uncertain",
            "rationale": "on reflection the population is unclear",
            "supersedes_decision_id": before["decision_id"],
        },
        headers=headers(REVIEWER_A),
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["supersedes_decision_id"] == before["decision_id"]

    # The correction is what the reviewer now reads back. Without this the whole
    # test passes on a service that files corrections and never shows them.
    current = app_client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={record_id}", headers=headers(REVIEWER_A)
    ).json()["items"]
    assert [i["decision_id"] for i in current] == [corrected.json()["decision_id"]], current
    assert current[0]["disposition"] == "uncertain"

    # Put it back so the rest of the run has a settled corpus — and ASSERT it,
    # which is the whole point. This call supersedes the CORRECTION, so it is
    # the one that was 409-ing silently before `_is_current`.
    restored = app_client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "include",
            "supersedes_decision_id": corrected.json()["decision_id"],
        },
        headers=headers(REVIEWER_A),
    )
    assert restored.status_code == 201, (
        "a reviewer may change their mind twice; a 409 here means the service is still comparing "
        "against the FIRST decision in the chain: " + restored.text
    )
    assert restored.json()["supersedes_decision_id"] == corrected.json()["decision_id"]

    # No SECOND conflict was opened on the way through, so the rest of the run
    # adjudicates exactly what `screened` set up.
    conflicts_after = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()["count"]
    assert conflicts_after == conflicts_before, "the correction opened a conflict the acceptance run did not expect"


def test_an_exclusion_reason_outside_the_locked_vocabulary_is_refused(app_client, review, screened):
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/decisions",
        json={
            "stage_id": review["stage_id"],
            "review_record_id": screened["records"][1],
            "disposition": "exclude",
            "exclusion_reason_code": "i-did-not-like-it",
            "supersedes_decision_id": str(uuid.uuid4()),
        },
        headers=headers(REVIEWER_A),
    )
    assert response.status_code == 422
    assert "vocabulary" in response.text


# ---------------------------------------------------------------------------
# Adjudication, unblinding, separation of duty
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adjudicated(app_client, review, screened):  # noqa: ANN201, ANN001
    review_id = review["review_id"]
    conflict = app_client.get(f"/api/reviews/{review_id}/conflicts", headers=lead_headers()).json()["items"][0]
    conflict_id = conflict["conflict_id"]

    unblind = app_client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating the title/abstract disagreement"},
        headers=lead_headers(),
    )
    assert unblind.status_code == 200, unblind.text

    resolved = app_client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:resolve",
        json={
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_population",
            "rationale": "outpatient cohort; the protocol is hospitalised adults",
        },
        headers=lead_headers(),
    )
    assert resolved.status_code == 200, resolved.text
    return {"conflict_id": conflict_id, "adjudication": resolved.json()}


def test_unblinding_leaves_an_audit_row_and_opens_the_dispositions(app_client, review, adjudicated):
    conflicts = app_client.get(f"/api/reviews/{review['review_id']}/conflicts", headers=lead_headers()).json()
    item = conflicts["items"][0]
    assert item["unblinded"] is True
    assert isinstance(item["dispositions"], list) and len(item["dispositions"]) == 2
    assert item["resolved"] is True


def test_an_adjudicator_who_took_part_in_the_conflict_is_refused(app_client, review, adjudicated):
    """`01-…:659` — separation of duty, enforced by a database trigger."""
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/conflicts/{adjudicated['conflict_id']}:resolve",
        json={
            "disposition": "include",
            "rationale": "I think it should be in",
            "supersedes_adjudication_id": adjudicated["adjudication"]["adjudication_id"],
        },
        headers=headers(REVIEWER_A, role="lead"),
    )
    assert response.status_code == 409, response.text
    assert "cannot adjudicate" in response.text


def test_a_reviewer_cannot_adjudicate_or_unblind(app_client, review, adjudicated):
    for path, body in (
        (f"conflicts/{adjudicated['conflict_id']}:unblind", {"reason": "curious"}),
        (
            f"conflicts/{adjudicated['conflict_id']}:resolve",
            {"disposition": "include", "rationale": "mine"},
        ),
    ):
        response = app_client.post(f"/api/reviews/{review['review_id']}/{path}", json=body, headers=headers(REVIEWER_B))
        assert response.status_code == 403, f"{path}: {response.text}"


# ---------------------------------------------------------------------------
# ER-005 / ER-009 — flow ledger and the seal
# ---------------------------------------------------------------------------


def test_the_flow_conserves_every_record(app_client, review, adjudicated):
    flow = app_client.get(f"/api/reviews/{review['review_id']}/flow", headers=lead_headers()).json()
    counts = flow["counts"]

    assert counts["identified_total"] == 5
    assert counts["quarantined"] == 1
    assert counts["records_total"] == 4
    assert counts["duplicates_removed"] == 1
    assert counts["not_yet_screened"] == 0
    assert counts["identified_total"] == counts["quarantined"] + counts["rejected"] + counts["records_total"]
    assert counts["records_total"] == (
        counts["duplicates_removed"] + counts["records_screened"] + counts["not_yet_screened"]
    )
    assert counts["records_screened"] == counts["included"] + counts["excluded"] + counts["uncertain"]


def test_rebuild_reproduces_the_canonical_counts_hash(app_client, review, adjudicated):
    """`01-…:510`."""
    first = app_client.get(f"/api/reviews/{review['review_id']}/flow", headers=lead_headers()).json()
    rebuilt = app_client.post(f"/api/reviews/{review['review_id']}/flow:rebuild", headers=lead_headers()).json()
    assert rebuilt["canonical_counts_hash"] == first["canonical_counts_hash"]
    assert rebuilt["matches_previous_projection"] is True


@pytest.fixture(scope="module")
def export(app_client, review, adjudicated):  # noqa: ANN201, ANN001
    response = app_client.post(f"/api/reviews/{review['review_id']}/exports", headers=lead_headers())
    assert response.status_code == 201, response.text
    return response.json()


def test_the_export_seals_two_distinct_hashes(export):
    """`16-…:357-364` / `DM-AT-28` — never substitutable."""
    assert export["manifest_hash"].startswith("sha256:")
    assert export["payload_digest"].startswith("sha256:")
    assert export["manifest_hash"] != export["payload_digest"]


def test_the_export_verifies_without_trusting_the_stored_hash(app_client, review, export):
    """`DM-AT-29`."""
    verified = app_client.get(
        f"/api/reviews/{review['review_id']}/exports/{export['export_id']}:verify",
        headers=lead_headers(),
    ).json()
    assert verified["verified"] is True
    assert verified["manifest_matches"] and verified["payload_matches"]


def test_omissions_are_explicit_and_never_empty_when_something_was_left_out(app_client, review, export):
    """`ER-009.3` / `01-…:626-627`."""
    body = app_client.get(
        f"/api/reviews/{review['review_id']}/exports/{export['export_id']}", headers=lead_headers()
    ).json()
    buckets = {o["bucket"] for o in body["omissions"]}
    assert "quarantined" in buckets
    assert "duplicate_removed" in buckets
    assert "excluded" in buckets
    # Everything is accounted for exactly once: 4 records (one of them removed
    # as a duplicate) plus the 1 quarantined import row.
    assert body["included_count"] + body["omissions_count"] == 5


def test_tampering_one_byte_of_the_payload_breaks_the_seal(review, export):
    """The negative control for the seal itself. Done in SQL, deliberately.

    The API cannot mutate an export — `inclusion_exports` is append-only — so the
    only way to falsify the verifier is to reach around it as the OWNER, which is
    also the only realistic tamper scenario worth testing.

    ## The tamper writes `true`, and it used to write `false`

    `manifest.shadow_mode` was a hardcoded `True` in `flow.py`, so overwriting it
    with `false` was a real edit. It now reads `settings.shadow_mode`, which is
    `False` on a deployment that declares nothing — this suite included — so the
    old `jsonb_set(..., 'false')` became a NO-OP that rewrote the value it already
    held. Postgres accepted the UPDATE, the bytes were identical, the digest
    reproduced, and the assertion below failed for the one reason it must never
    fail: nothing was tampered with.

    Writing the opposite constant restores the edit and is exactly as strong a
    tamper. But a boolean has only two values and the seal is what is on trial
    here, so the SAME class of silent no-op could return the moment the default
    moves again. `before`/`after` is the guard: it proves the row actually
    changed before any conclusion is drawn from the digest.
    """
    engine = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        before = conn.execute(
            text("SELECT payload FROM inclusion_exports WHERE export_id = :e"), {"e": export["export_id"]}
        ).scalar_one()
        conn.execute(text("ALTER TABLE inclusion_exports DISABLE TRIGGER trg_inclusion_exports_append_only"))
        try:
            conn.execute(
                text(
                    "UPDATE inclusion_exports SET payload = jsonb_set(payload, "
                    "'{manifest,shadow_mode}', 'true') "
                    "WHERE export_id = :e"
                ),
                {"e": export["export_id"]},
            )
        finally:
            conn.execute(text("ALTER TABLE inclusion_exports ENABLE TRIGGER trg_inclusion_exports_append_only"))

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        row = conn.execute(
            text("SELECT payload, manifest_hash, payload_digest FROM inclusion_exports WHERE export_id = :e"),
            {"e": export["export_id"]},
        ).one()

    assert row[0] != before, (
        "the UPDATE did not change the payload, so this test proves nothing about the seal — "
        f"manifest.shadow_mode was already {before['manifest'].get('shadow_mode')!r} and the tamper "
        "wrote the same value back"
    )

    import hashlib

    recomputed = (
        "sha256:"
        + hashlib.sha256(json.dumps(row[0], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    )
    assert recomputed != row[2], "a tampered payload must not reproduce the stored digest"


# ---------------------------------------------------------------------------
# Negative controls that must FAIL
# ---------------------------------------------------------------------------


def test_a_body_carrying_work_type_at_any_depth_is_rejected(app_client, review):
    """`ER-002.7` / `DM-AT-18` NEGATIVE control — reject, never silently drop."""
    payload = {"records": [{"title": "A regulation", "work_type": "regulatory_instrument"}]}
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/imports?source_kind=canonical_json",
        content=json.dumps(payload),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "forbidden_discriminator"
    assert response.json()["forbidden_paths"] == ["$[0].work_type"]


def test_camel_case_work_type_is_caught_too(app_client, review):
    """A guard matching only the literal string is bypassed by any JS client."""
    payload = {"records": [{"title": "A regulation", "workType": "regulatory_instrument"}]}
    response = app_client.post(
        f"/api/reviews/{review['review_id']}/imports?source_kind=canonical_json",
        content=json.dumps(payload),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "forbidden_discriminator"


def test_a_locked_protocol_cannot_be_edited(review):
    """Database exception, not an application check."""
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with pytest.raises(Exception, match="locked"):
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(
                text("UPDATE review_protocol_versions SET reviewers_required = 1 WHERE protocol_version_id = :p"),
                {"p": review["protocol_version_id"]},
            )


def test_a_screening_decision_cannot_be_mutated_or_deleted(review, screened):
    """The append-only trigger, reached by a caller who can actually SEE the rows.

    This used to bind only `app.current_tenant`, and it worked — because L1's
    third clause opened every decision on an unblinded record to anyone in the
    tenant, so an actor-less connection could still match rows and trip the
    trigger. `0008` narrowed that clause to the audience the unblinding names, and
    the test stopped raising: with no actor bound it now matches ZERO rows, and an
    UPDATE that changes nothing never reaches a row-level trigger.

    That is the correct new behaviour and it made this test vacuous, which is the
    more dangerous half — "DID NOT RAISE" is at least loud, but a matching-nothing
    UPDATE would have gone green the moment the assertion were relaxed. The role
    GUC is bound instead, so the mutation genuinely reaches a row and the trigger
    is the thing being measured rather than the visibility rule.

    `screening_decisions` is one of `0015`'s `FULLY_GUARDED` tables, so
    `evidence_review_app` — the role `DB_URL` connects as, per `conftest.py` —
    has had `UPDATE, DELETE` revoked outright. That REVOKE is now correctly
    enforced (it used to be silently un-revoked by the harness's own blanket
    GRANT; that gap is what `0015`'s conftest wiring closed), so the write below
    is refused by the GRANT before it ever reaches the row-level trigger:
    `psycopg.errors.InsufficientPrivilege: permission denied for table
    screening_decisions`, not the trigger's `append-only` message. That is what
    PRODUCTION actually takes — the app never runs as the table owner. The
    trigger's `append-only` message stays reachable too, for a connection that
    owns the table (the GRANT does not bind it, only the trigger does), so the
    regex accepts either. It must not accept anything else: `match=` is a regex
    search, not "any exception", so an unguarded UPDATE that LANDS still fails
    this test on the same "DID NOT RAISE" it always would have.
    """
    refused = "append-only|permission denied for table screening_decisions"
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})

    def _visible_decision_count() -> int:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(text("SELECT set_config('app.actor_role', 'lead', true)"))
            return conn.execute(
                text("SELECT count(*) FROM screening_decisions WHERE review_id = :r"),
                {"r": review["review_id"]},
            ).scalar_one()

    # The non-vacuity floor. Without it, both blocks below would pass against a
    # policy that hid everything.
    assert _visible_decision_count() > 0, (
        "no screening decision is visible to a lead-bound connection, so the UPDATE and DELETE below "
        "would match nothing and the append-only trigger would never fire. This assertion would then "
        "be measuring the RLS policy, not the trigger."
    )

    with pytest.raises(Exception, match=refused):
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(text("SELECT set_config('app.actor_role', 'lead', true)"))
            conn.execute(
                text("UPDATE screening_decisions SET disposition = 'include' WHERE review_id = :r"),
                {"r": review["review_id"]},
            )
    with pytest.raises(Exception, match=refused):
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(text("SELECT set_config('app.actor_role', 'lead', true)"))
            conn.execute(text("DELETE FROM screening_decisions WHERE review_id = :r"), {"r": review["review_id"]})


def test_export_is_refused_while_a_stage_is_incomplete(app_client):
    """The first negative control in the plan's table."""
    external_key = f"incomplete-{uuid.uuid4()}"
    created = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": external_key, "title": "Incomplete"},
        headers=lead_headers(),
    ).json()
    review_id = created["review_id"]

    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=lead_headers(),
    ).json()
    app_client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "test"},
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Unscreened paper", "year": 2020}]}),
        headers={**lead_headers(), "Content-Type": "application/octet-stream"},
    )
    app_client.post(f"/api/reviews/{review_id}/corpus-versions", headers=lead_headers())

    response = app_client.post(f"/api/reviews/{review_id}/exports", headers=lead_headers())
    assert response.status_code == 422, response.text
    assert response.json()["not_yet_screened"] == 1

    exports = app_client.get(f"/api/reviews/{review_id}/exports", headers=lead_headers()).json()
    assert exports["count"] == 0, "a refused export must not leave an export row"


def test_another_tenant_sees_nothing(app_client, review):
    """`ER-V008` — tenant negatives on every read path."""
    for path in ("", "/records", "/flow", "/conflicts", "/corpus-versions"):
        response = app_client.get(f"/api/reviews/{review['review_id']}{path}", headers=lead_headers(OTHER_TENANT))
        assert response.status_code == 404, f"{path} leaked across tenants: {response.status_code}"
        # `instance` echoes the request path, which the caller supplied — that is
        # RFC 9457 and not a disclosure. What must be absent is any DATA: the
        # title, the project, or a record. Asserting on the whole body would
        # only be asserting that we do not echo the URL.
        body = response.json()
        body.pop("instance", None)
        leaked = json.dumps(body)
        for secret in ("Shadow smoke", "dogfood", "Tocilizumab", review["stage_id"]):
            assert secret not in leaked, f"{path} leaked {secret!r} across tenants"


def test_an_unbound_tenant_sees_nothing(review):
    """Fail-closed. A forgotten `bind_actor` must return zero rows, not all rows."""
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM screening_decisions")).scalar_one()
    assert count == 0


def test_illegal_lifecycle_transitions_are_refused(app_client, review):
    response = app_client.post(
        f"/api/reviews/{review['review_id']}:archive",
        json={"rationale": "skipping close"},
        headers=lead_headers(),
    )
    assert response.status_code == 409
    # The command-level refusal, which is more specific than the old generic
    # "illegal transition": `archive` names the states it applies to, so the
    # caller learns they used the wrong verb rather than that the machine
    # happened to say no.
    assert response.json()["allowed_from"] == ["draft", "closed"]
    assert response.json()["current_state"] == "active"


def test_a_reopen_without_an_attestation_is_refused(app_client):
    external_key = f"reopen-{uuid.uuid4()}"
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": external_key, "title": "Reopen"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
        },
        headers=lead_headers(),
    ).json()
    app_client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=lead_headers(),
    )
    app_client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=lead_headers())

    refused = app_client.post(
        f"/api/reviews/{review_id}:reopen", json={"rationale": "more work"}, headers=lead_headers()
    )
    assert refused.status_code == 422
    assert "attested" in refused.text

    allowed = app_client.post(
        f"/api/reviews/{review_id}:reopen",
        json={"rationale": "more work", "approval_attestation_ref": "fr-attestation-abc123"},
        headers=lead_headers(),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["state"] == "active"


def test_activation_requires_a_locked_protocol(app_client):
    """`DM-AT-02`."""
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": f"unlocked-{uuid.uuid4()}", "title": "Unlocked"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
        },
        headers=lead_headers(),
    ).json()

    response = app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "too early"},
        headers=lead_headers(),
    )
    assert response.status_code == 422
    assert "LOCKED" in response.text


def test_resume_and_reopen_are_distinct_commands(app_client):
    """`:resume` and `:reopen` used to be the same operation with two URLs.

    Both map to `to_state='active'`, and `LEGAL_TRANSITIONS` allows that from
    BOTH `paused` and `closed` — so legality alone could not tell them apart.
    `:resume` on a closed review performed a reopen; `:reopen` on a paused one
    performed a resume that skipped the attestation entirely. A UI with a
    "Resume" button would have reopened a closed review.
    """
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": f"verbs-{uuid.uuid4()}", "title": "Verbs"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
        },
        headers=lead_headers(),
    ).json()
    app_client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=lead_headers(),
    )

    # PAUSED: resume is the right verb, reopen is not.
    app_client.post(f"/api/reviews/{review_id}:pause", json={"rationale": "hold"}, headers=lead_headers())
    wrong = app_client.post(
        f"/api/reviews/{review_id}:reopen",
        json={"rationale": "wrong verb", "approval_attestation_ref": "fr-att-1"},
        headers=lead_headers(),
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["allowed_from"] == ["closed"]

    right = app_client.post(f"/api/reviews/{review_id}:resume", json={"rationale": "back"}, headers=lead_headers())
    assert right.status_code == 200 and right.json()["state"] == "active"

    # CLOSED: reopen is the right verb, resume is not — and resuming a closed
    # review would previously have reopened it, attestation and all.
    app_client.post(f"/api/reviews/{review_id}:close", json={"rationale": "done"}, headers=lead_headers())
    wrong = app_client.post(
        f"/api/reviews/{review_id}:resume", json={"rationale": "wrong verb"}, headers=lead_headers()
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["allowed_from"] == ["paused"]

    state = app_client.get(f"/api/reviews/{review_id}", headers=lead_headers()).json()["state"]
    assert state == "closed", "a refused command must not have moved the review"

    right = app_client.post(
        f"/api/reviews/{review_id}:reopen",
        json={"rationale": "more work", "approval_attestation_ref": "fr-att-2"},
        headers=lead_headers(),
    )
    assert right.status_code == 200 and right.json()["state"] == "active"


def test_archive_applies_to_a_draft_or_a_closed_review(app_client):
    """`0028`: a draft is archivable — the database is append-only, so an
    abandoned draft has no other exit. (Until `0028` this test pinned the
    opposite: a draft archive was a 409 naming only `closed`.)"""
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": f"arch-{uuid.uuid4()}", "title": "Archive"},
        headers=lead_headers(),
    ).json()["review_id"]
    archived = app_client.post(
        f"/api/reviews/{review_id}:archive", json={"rationale": "abandoned draft"}, headers=lead_headers()
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["state"] == "archived"


def test_a_review_can_be_corrected_before_activation_and_not_after(app_client):
    """`§8.2` — patch only pre-activation, with an expected version.

    Without this the first typo was permanent: `reviews` is written once, there
    is no DELETE, and the only workaround was to abandon the review and create a
    second one under a different `external_key` — leaving the typo in every
    listing forever.
    """
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": f"typo-{uuid.uuid4()}", "title": "Diabtees"},
        headers=lead_headers(),
    ).json()["review_id"]

    fixed = app_client.patch(f"/api/reviews/{review_id}", json={"title": "Diabetes"}, headers=lead_headers())
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["title"] == "Diabetes"
    assert fixed.headers["etag"], "a lifecycle write must carry an ETag"

    # Stale expected_revision -> 412, not a silent overwrite.
    stale = app_client.patch(
        f"/api/reviews/{review_id}",
        json={"title": "Something else", "expected_revision": 99},
        headers=lead_headers(),
    )
    assert stale.status_code == 412, stale.text

    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
        },
        headers=lead_headers(),
    ).json()
    app_client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=lead_headers(),
    )
    app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=lead_headers(),
    )

    # After activation the title labels evidence already in use.
    refused = app_client.patch(f"/api/reviews/{review_id}", json={"title": "Too late"}, headers=lead_headers())
    assert refused.status_code == 409, refused.text
    assert refused.json()["allowed_from"] == ["draft", "configured"]
    assert app_client.get(f"/api/reviews/{review_id}", headers=lead_headers()).json()["title"] == "Diabetes"


def test_every_lifecycle_write_carries_an_etag(app_client):
    """`fr`'s reviews client documents this header as present on lifecycle
    writes. It was present on exactly two of them, so a client had to GET after
    every transition or silently drop to last-write-wins.
    """
    review_id = app_client.post(
        "/api/reviews",
        json={"project_id": "dogfood", "external_key": f"etag-{uuid.uuid4()}", "title": "ETags"},
        headers=lead_headers(),
    ).json()["review_id"]
    protocol = app_client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
        },
        headers=lead_headers(),
    ).json()
    app_client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=lead_headers(),
    )

    activated = app_client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=lead_headers(),
    )
    assert activated.headers["etag"] == f'"{activated.json()["revision"]}"'

    for verb, body in (
        ("pause", {"rationale": "hold"}),
        ("resume", {"rationale": "back"}),
        ("close", {"rationale": "done"}),
    ):
        response = app_client.post(f"/api/reviews/{review_id}:{verb}", json=body, headers=lead_headers())
        assert response.status_code == 200, response.text
        assert response.headers["etag"] == f'"{response.json()["revision"]}"', verb


def test_a_record_and_its_full_version_history_are_readable(app_client, review):
    """The route `list_records` already told readers about.

    Its docstring said superseded versions "remain queryable through the record
    detail route" — which did not exist. A lead adjudicating a conflict had only
    a title, and the pre-merge state of a deduplicated record was readable
    nowhere.
    """
    records = app_client.get(f"/api/reviews/{review['review_id']}/records", headers=lead_headers()).json()
    record_id = records["items"][0]["review_record_id"]

    detail = app_client.get(f"/api/reviews/{review['review_id']}/records/{record_id}", headers=lead_headers())
    assert detail.status_code == 200, detail.text
    body = detail.json()
    # `== 1`, not `>= 1`. This record was never merged with survivorship, so it
    # has exactly one version and the route must say so. The old `>= 1` was
    # satisfied by the defect it was meant to describe: NOTHING could write a
    # second version, so "full version history" was always a single row and the
    # assertion could never have noticed. The multi-version case is
    # `tests/integration/test_survivorship_is_applied.py`.
    assert body["version_count"] == 1, body
    assert [v["version_number"] for v in body["versions"]] == [1]
    assert body["current_version_number"] == 1
    assert body["versions"][0]["supersedes_version_id"] is None
    # The fields a conflict view needs and `GET /conflicts` does not carry.
    assert "abstract" in body["versions"][0]
    assert "doi" in body["versions"][0]
    assert "field_provenance" in body["versions"][0]

    stranger = app_client.get(f"/api/reviews/{review['review_id']}/records/{uuid.uuid4()}", headers=lead_headers())
    assert stranger.status_code == 404
