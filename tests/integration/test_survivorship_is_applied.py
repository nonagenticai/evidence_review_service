"""A merge that chooses the survivor's fields must change the survivor.

On `2ffdd5f`, `resolve_cluster` wrote one `duplicate_resolutions` row and N
`duplicate_survivorship_fields` rows and nothing else. The survivor's record was
byte-identical after the merge, and nothing in `src/` ever selected
`duplicate_survivorship_fields` — the lead's choice was write-only data with no
read path anywhere in the service.

Two consequences a demo hits directly:

* choosing "take the title from record B" produces no visible change on any
  endpoint, so the feature appears to do nothing;
* `GET /records/{id}` advertises "one record and its FULL version history" and
  its own docstring says it exists so "the pre-merge state of a deduplicated
  record" is readable — while `version_count` is always exactly 1, because
  `imports._record_version` writes `version_number=1` as a literal and is the
  only writer.

The existing acceptance test baked that in: `test_e2e_acceptance.py:1069` asserted
`version_count >= 1`, which is satisfied by the broken behaviour.

## The security half

`field_name` was unvalidated and the table had no CHECK. Inert while nothing read
the rows; the moment the value is applied it becomes a caller-chosen write target
on `review_record_versions`, whose RLS is scoped to the TENANT and not to the
review. `test_a_field_outside_the_closed_vocabulary_is_refused` is the gate for
that, and it is the one that matters most in this file.
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

TENANT = tenant("tenant-survivorship")
LEAD = "survivorship-lead"

#: One shared DOI, two different titles. The engine clusters on the normalized
#: DOI, and the differing titles are what a survivorship choice can be ABOUT.
SHARED_DOI = "10.1000/survivorship-fixture"
SURVIVOR_TITLE = "The survivor's own title"
LOSER_TITLE = "The loser's better title"


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
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


def _review_with_a_duplicate_pair(client) -> dict:  # noqa: ANN001
    """A fresh review holding exactly one two-member duplicate cluster.

    Not module-scoped: several tests here resolve the cluster, and a resolution
    is append-only, so sharing one fixture would make the tests order-dependent
    in a way that hides which of them actually drove the write.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "survivorship", "external_key": f"surv-{uuid.uuid4()}", "title": "Survivorship"},
        headers=headers(),
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
        headers=headers(),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    doi = f"{SHARED_DOI}.{uuid.uuid4()}"
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": SURVIVOR_TITLE, "year": 2021, "doi": doi},
                    {"title": LOSER_TITLE, "year": 2021, "doi": doi},
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())

    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers())
    assert run.status_code in (200, 201), run.text
    dedup_run_id = run.json()["dedup_run_id"]

    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": dedup_run_id},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == 1, f"fixture expected exactly one cluster, got {len(clusters)}: {clusters}"
    cluster = clusters[0]

    members = cluster["members"]
    assert len(members) == 2, members
    survivor = next(m for m in members if m["title"] == SURVIVOR_TITLE)
    loser = next(m for m in members if m["title"] == LOSER_TITLE)

    # The reviewer picks a value out of `canonical_presentation`, so the field
    # they can choose must BE there. It offered only the normalized forms, which
    # `SURVIVABLE_FIELDS` refuses — three of its four fields disagreed with the
    # vocabulary that would now enforce them.
    titles = cluster["canonical_presentation"]["title"]
    assert {entry["value"] for entry in titles} == {SURVIVOR_TITLE, LOSER_TITLE}, titles
    chosen = next(entry for entry in titles if entry["value"] == LOSER_TITLE)
    assert chosen["sources"] == [loser["review_record_version_id"]], chosen

    return {
        "review_id": review_id,
        "cluster_id": cluster["cluster_id"],
        "survivor": survivor,
        "loser": loser,
    }


def _record(client, review_id: str, record_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/records/{record_id}", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()


# --------------------------------------------------------------------------


def test_a_merge_with_survivorship_writes_a_new_version_on_the_survivor(client) -> None:
    """The headline. `version_count` was permanently 1 before this."""
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    before = _record(client, review_id, survivor["review_record_id"])
    assert before["version_count"] == 1
    original_hash = before["versions"][0]["content_hash"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "same DOI; the loser's title is the published one",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text
    assert resolved.json()["applied_version_id"], (
        "the merge reported no applied version. Before this change the field did not exist and the "
        "survivor was byte-identical afterwards."
    )

    after = _record(client, review_id, survivor["review_record_id"])
    assert after["version_count"] == 2, after
    assert after["current_version_number"] == 2
    v1, v2 = after["versions"]
    assert v1["version_number"] == 1 and v2["version_number"] == 2
    assert v2["supersedes_version_id"] == v1["review_record_version_id"], (
        "the new version does not name the one it supersedes; the chain is not linear"
    )
    assert v2["title"] == LOSER_TITLE, "the chosen field did not reach the new version"
    assert v1["title"] == SURVIVOR_TITLE, "the superseded version was mutated rather than superseded"
    assert v2["content_hash"] != original_hash, (
        "the content changed but the hash did not — the hash is not covering the surviving field"
    )

    provenance = v2["field_provenance"]["derived_from"]
    assert provenance["kind"] == "duplicate_merge_survivorship"
    assert provenance["resolution_id"] == resolved.json()["resolution_id"]
    assert provenance["fields"]["title"] == loser["review_record_version_id"], (
        "the new version does not record WHICH original the surviving title came from — "
        "`01-…:453` requires every survivor field to resolve to an original record/version"
    )


def test_survivorship_recomputes_the_normalized_fields(client) -> None:
    """`normalized_*` are derived, never copied.

    A source that supplies `title` also supplies `normalized_title`, so copying
    would agree here by luck. It stops agreeing the moment someone survives one
    field and not its normalized sibling, and the dedup baseline then stops being
    a deterministic function of stored bytes.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from domain.policies.normalization import normalize_text

    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "take the title",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )

    after = _record(client, review_id, survivor["review_record_id"])
    v2 = after["versions"][1]
    assert v2["normalized_title"] == normalize_text(LOSER_TITLE), (
        f"normalized_title is {v2['normalized_title']!r}; derived from the new title it would be "
        f"{normalize_text(LOSER_TITLE)!r}"
    )


def test_a_field_outside_the_closed_vocabulary_is_refused(client) -> None:
    """THE security gate. On `2ffdd5f` this returned 200 and wrote the row.

    `review_id` is the specific danger: applied, it would mint a version of this
    review's record carrying another review's id, and RLS on
    `review_record_versions` is tenant-scoped, so `WITH CHECK` would not refuse
    it.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    refused = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "hostile",
            "survivorship": {"review_id": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["code"] == "unprocessable_state", body
    assert "title" in body.get("allowed", []), (
        "the refusal must name the vocabulary it is enforcing; an unnamed refusal is not actionable"
    )
    assert body.get("rejected") == ["review_id"]

    unchanged = _record(client, review_id, survivor["review_record_id"])
    assert unchanged["version_count"] == 1, "a refused survivorship still wrote a version"


def test_survivorship_on_a_resolution_with_no_survivor_is_refused(client) -> None:
    """Accepted silently before: the rows were written and could never mean anything."""
    fixture = _review_with_a_duplicate_pair(client)
    review_id, loser = fixture["review_id"], fixture["loser"]

    refused = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            # `no_match` is the "these are not duplicates" resolution — it has no
            # survivor by construction, which is exactly the case survivorship
            # cannot apply to.
            "resolution_type": "no_match",
            "rationale": "different studies",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert refused.status_code == 422, refused.text
    assert "survivorship" in refused.json()["detail"]


def test_a_choice_that_changes_nothing_writes_no_version(client) -> None:
    """Re-affirming the survivor's own value is a no-op, and says so.

    Without this a lead who re-confirms the same value repeatedly mints versions
    forever, each moving the next corpus manifest for no semantic change.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor = fixture["review_id"], fixture["survivor"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "keep our own title",
            "survivorship": {"title": survivor["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text
    assert resolved.json()["applied_version_id"] is None, (
        "a survivorship choice identical to the survivor's own value minted a version"
    )
    assert _record(client, review_id, survivor["review_record_id"])["version_count"] == 1


def test_a_merge_without_survivorship_writes_no_new_version(client) -> None:
    """Scoping. The ordinary merge path is unchanged; only a CHOICE writes."""
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor = fixture["review_id"], fixture["survivor"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "plain merge",
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text
    assert resolved.json()["applied_version_id"] is None
    assert _record(client, review_id, survivor["review_record_id"])["version_count"] == 1


def test_the_resolution_chain_reports_the_survivorship_it_recorded(client) -> None:
    """`duplicate_survivorship_fields` had NO read path anywhere in `src/`."""
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "take the title",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )

    chain = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}/resolutions",
        headers=headers(),
    )
    assert chain.status_code == 200, chain.text
    rows = chain.json()["items"] if isinstance(chain.json(), dict) else chain.json()
    merge = [r for r in rows if r["resolution_type"] == "merge"]
    assert merge, rows
    survivorship = merge[-1]["survivorship"]
    assert survivorship, "the chain reports no survivorship for a merge that carried it"
    assert survivorship[0]["field_name"] == "title"
    assert survivorship[0]["value"] == LOSER_TITLE
    assert survivorship[0]["source_record_version_id"] == loser["review_record_version_id"]


def test_the_sealed_corpus_still_compares_the_version_it_sealed(client) -> None:
    """A new version must NOT shift a dedup run against an already-sealed corpus.

    `_candidates` joins `corpus_version_members.review_record_version_id`, and
    `list_clusters` joins the frozen member version. That is correct — the
    proposal a human resolved must not change underneath the resolution chain —
    and it is exactly the kind of correctness a later "fix the version lookup"
    change would break by making it a tail read. Asserted so it cannot be.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]
    cluster_id = fixture["cluster_id"]

    client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster_id}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "take the title",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )

    corpora = client.get(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["items"]
    sealed = corpora[-1]
    members = client.get(
        f"/api/reviews/{review_id}/corpus-versions/{sealed['corpus_version_id']}/members",
        headers=headers(),
    ).json()
    sealed_version_ids = {m["review_record_version_id"] for m in members["items"]}
    assert survivor["review_record_version_id"] in sealed_version_ids, (
        "the sealed corpus now names a version that did not exist when it was sealed"
    )
    assert members["member_manifest_hash"] == members["recomputed_manifest_hash"], (
        "the sealed corpus manifest no longer recomputes to itself"
    )


def test_survivorship_does_not_move_a_sealed_export_off_its_corpus(client) -> None:
    """F1, end to end through the real merge path.

    Found adversarially after `925cacf` and `fa03b75` were BOTH merged; neither
    shows it alone, because before survivorship no record ever had a v2 and the
    tail and the corpus member were always the same row.

    Measured on the merged pair:

        export `included` version ids : {v2}
        the corpus its manifest cites : {v1, v1}

    and `GET …:verify` answered **200 / verified**, because verification
    re-hashes the STORED payload and has no membership check. An artifact that
    contradicts its own envelope and certifies itself is the precise failure this
    service exists to refuse, so it is asserted here through the API rather than
    only at the seam.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "same DOI; take the published title",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text
    applied = resolved.json()["applied_version_id"]
    assert applied, "fixture drift: no new version was minted, so nothing can diverge"

    # RE-SEAL BEFORE SCREENING, and that ordering is the point.
    #
    # `screening_queue` serves the TAIL version, while the corpus sealed before
    # the merge holds v1. So a reviewer screening now decides on v2 while the
    # cited corpus contains v1, and `create_export` refuses — correctly, since
    # sealing it would credit a decision to content the corpus does not describe.
    # Re-sealing first makes the queue, the decision and the corpus agree.
    #
    # That the queue and the corpus can disagree AT ALL is a defect one level
    # deeper than this test: an assignment is minted from a corpus version, so
    # the queue should serve the version that corpus sealed rather than the
    # review's tail. Recorded in the handoff; changing what a reviewer is shown
    # mid-stage is not a change to make as a side effect of a test fix.
    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["surv-reviewer"]},
        headers=headers(),
    )
    reviewer = {"X-Tenant-Id": TENANT, "X-Subject-Id": "surv-reviewer", "X-Actor-Role": "reviewer"}
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=reviewer,
    ).json()["items"]
    assert queue, "fixture drift: nothing to screen"
    for item in queue:
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=reviewer,
        )

    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert created.status_code == 201, created.text
    export = client.get(f"/api/reviews/{review_id}/exports/{created.json()['export_id']}", headers=headers()).json()

    cited_corpus = export["payload"]["manifest"]["corpus_version_id"]
    members = client.get(f"/api/reviews/{review_id}/corpus-versions/{cited_corpus}/members", headers=headers()).json()
    member_version_ids = {m["review_record_version_id"] for m in members["items"]}
    included_version_ids = {row["review_record_version_id"] for row in export["payload"]["included"]}

    assert included_version_ids, "the export included nothing, so this proves nothing"
    assert included_version_ids <= member_version_ids, (
        "the export seals versions the corpus it cites does not contain: "
        f"included={sorted(included_version_ids)} members={sorted(member_version_ids)}. "
        "The manifest names this corpus and its member_manifest_hash, so the artifact contradicts "
        "its own envelope — and `:verify` cannot see it."
    )
    # NOT `applied not in included_version_ids` — that was this test's original
    # assertion and it is wrong now. It was written for the sequence where the
    # corpus was sealed BEFORE the merge, where including v2 meant citing a
    # corpus that did not contain it. With the re-seal above the corpus DOES
    # contain v2, so sealing it is the correct outcome; what must hold is the
    # subset relation asserted above, in both directions.
    assert applied in member_version_ids, (
        "the re-sealed corpus does not contain the surviving version, so the merge and the seal "
        "disagree about what the review currently holds"
    )

    verified = client.get(
        f"/api/reviews/{review_id}/exports/{created.json()['export_id']}:verify", headers=headers()
    ).json()
    assert verified["outcome"] == "verified", verified


def test_re_sealing_after_a_merge_lets_the_export_carry_the_new_version(client) -> None:
    """The other direction, so the fix is a SCOPE and not a refusal to ever move.

    Re-seal the corpus after the merge and the new version becomes a member, so
    an export taken then carries it legitimately. Without this, replacing
    `corpus_member_versions` with "always seal v1" would satisfy the test above.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "take the published title",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    applied = resolved.json()["applied_version_id"]
    assert applied

    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text
    new_corpus = resealed.json()["corpus_version_id"]
    members = client.get(f"/api/reviews/{review_id}/corpus-versions/{new_corpus}/members", headers=headers()).json()
    assert applied in {m["review_record_version_id"] for m in members["items"]}, (
        "re-sealing after a merge did not pick up the surviving version, so the fix would leave a "
        "review permanently unable to export its own current content"
    )


def test_survivorship_cannot_take_a_value_from_outside_the_cluster(client) -> None:
    """F7. Checking `review_id` alone was not enough.

    `SURVIVABLE_FIELDS` is documented as "a choice between the values a reviewer
    was shown", and what they were shown is `canonical_presentation`, which only
    ever offers members of THIS cluster. Before this, a lead could name any
    version in the review — measured: `200`, with `applied_version_id` set — and
    the same path rewrites `doi` and `pmid`, whose normalized forms
    `apply_survivorship` then recomputes. That moves a record's dedup baseline
    using content that was never part of the duplicate proposal anybody resolved.
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor = fixture["review_id"], fixture["survivor"]

    # A third record, in the same review, in no cluster with these two.
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {"records": [{"title": "An unrelated study", "year": 2019, "doi": f"10.9999/{uuid.uuid4()}"}]}
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    records = client.get(f"/api/reviews/{review_id}/records", headers=headers()).json()["items"]
    outsider = next(r for r in records if r["title"] == "An unrelated study")

    refused = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "borrowing a title from elsewhere",
            "survivorship": {"title": outsider["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert refused.status_code == 422, refused.text
    assert "not a member of this cluster" in refused.json()["detail"], refused.json()

    unchanged = _record(client, review_id, survivor["review_record_id"])
    assert unchanged["version_count"] == 1, "a refused survivorship still minted a version"


def test_apply_survivorship_refuses_a_bad_field_on_its_own(client) -> None:
    """F8. The vocabulary check inside `apply_survivorship` was dead copy.

    `resolve_cluster` validates first, so replacing the check in
    `apply_survivorship` with `unknown = []` left all 749 tests green — which
    makes "both, or it is a convention" (migration `0007`) untrue of the Python
    half. Driven at the seam, because no HTTP path can reach it past the router's
    own check, which is exactly why it needed its own test.

    ## What the mutant actually does, recorded rather than glossed

    Neutering the check turns this test red, but with
    `UnsupportedTypeError: /review_id: UUID has no RFC 8785 representation` from
    `_content_hash`, not with the refusal being asserted. So for `review_id`
    specifically the unguarded path CRASHES rather than silently minting a
    poisoned version — the exploitability is lower than it looks, and saying so
    is more useful than implying otherwise.

    It does not hold in general. A field outside the vocabulary whose value is a
    plain string canonicalizes perfectly well, so the crash is a property of
    `review_id`'s type and not a second line of defence. The check is what makes
    the refusal a refusal rather than a 500.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from actor import Actor, ActorRole
    from api.problem_details import UnprocessableStateError
    from application.services.imports import apply_survivorship
    from database import actor_scope
    from models.records import ReviewRecordVersion

    fixture = _review_with_a_duplicate_pair(client)
    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        source = session.get(ReviewRecordVersion, uuid.UUID(fixture["loser"]["review_record_version_id"]))
        with pytest.raises(UnprocessableStateError) as caught:
            apply_survivorship(
                session,
                actor,
                uuid.UUID(fixture["review_id"]),
                survivor_record_id=uuid.UUID(fixture["survivor"]["review_record_id"]),
                sources={"review_id": source},
                resolution_id=uuid.uuid4(),
                cluster_id=uuid.uuid4(),
                subject=LEAD,
            )
    assert "review_id" in str(caught.value)
    assert "title" in caught.value.extra.get("allowed", []), caught.value.extra


def test_re_sealing_after_screening_refuses_the_export_rather_than_swapping_the_content(client) -> None:
    """G3. My own #21 fix displaced this defect instead of removing it.

    `#21` made the export seal the versions its cited corpus contains, and told a
    lead who tripped the refusal to "re-seal the corpus and re-run the stage".
    Following that advice produced the very artifact #21 exists to prevent —
    measured end to end before this change:

        screen v1 -> merge with survivorship -> export #1 cites corpus A,
                     seals "AS SCREENED title"                      201
        lead re-seals                        -> export #2 cites corpus B,
                     seals "the OTHER title" (nobody screened it)   201
                     GET :verify                                    verified

    The anchor is `screening_decisions.review_record_version_id`: the version each
    decision was actually taken against. (`screening_assignments.corpus_version_id`
    exists as of `0009`, but it is the assignment's corpus, not the decision's
    version, so it is not the anchor for this refusal. This docstring used to say
    the column did not exist at all; that stopped being true in `0009`.)
    """
    fixture = _review_with_a_duplicate_pair(client)
    review_id, survivor, loser = fixture["review_id"], fixture["survivor"], fixture["loser"]

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["reseal-reviewer"]},
        headers=headers(),
    )
    reviewer = {"X-Tenant-Id": TENANT, "X-Subject-Id": "reseal-reviewer", "X-Actor-Role": "reviewer"}
    for item in client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=reviewer,
    ).json()["items"]:
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=reviewer,
        )

    applied = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{fixture['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "same DOI",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert applied.json()["applied_version_id"], "fixture drift: no new version, so nothing can diverge"

    first = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert first.status_code == 201, first.text
    payload = client.get(f"/api/reviews/{review_id}/exports/{first.json()['export_id']}", headers=headers()).json()[
        "payload"
    ]
    titles_before = {row["title"] for row in payload["included"]}
    assert SURVIVOR_TITLE in titles_before, f"export #1 already carries the wrong content: {titles_before}"

    # The remedy #21's refusal message prescribes.
    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text

    second = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert second.status_code == 422, (
        "the export was taken against a corpus sealed AFTER screening, so it seals content nobody "
        f"read — and `:verify` cannot see it. Got {second.status_code}: {second.text[:300]}"
    )
    body = second.json()
    assert "nobody read" in body["detail"], body
    assert body.get("record_ids"), "the refusal does not name the records it refused over"
