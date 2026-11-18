"""A screening round names one corpus, and `POST /assignments` may not pretend otherwise.

## The defect

`create_export`'s G3 refusal tells a lead:

    "... were screened against a version that corpus version X does not contain,
     or their reviewers named different versions. Export before re-sealing, or
     re-run the stage against the new corpus; ..."

The second remedy cannot be carried out in the round it is read in.
`uq_assignment_once` is `(review_id, stage_id, round_number, review_record_id,
reviewer_subject_id)` — `corpus_version_id` is deliberately NOT in it, so one
reviewer cannot hold two live assignments at one position — and
`create_assignments` inserts `.on_conflict_do_nothing(constraint=
"uq_assignment_once")`. An existing assignment can therefore never be re-pointed
at another corpus. The route said `201` anyway, and echoed the corpus id it had
just failed to store.

Measured on `a75da1a`, driving the real API against the local database
(`scratchpad/repro.py`): a 3-record corpus v1, one reviewer, decisions taken,
then a survivorship merge and a re-seal to v2.

    POST /exports                                        -> 422  (G3, as above)
    POST /assignments {round_number: 1, corpus: v2}       -> 201
        body.corpus_version_id                           =  v2   <- the lie
        body.assignments_created                          =  0
        screening_assignments for round 1                =  v1 x3
    GET  /screening/queue                                -> pre-merge titles
    POST /decisions (superseding, round 1)               -> 201, stamped v1
    POST /exports                                        -> 422  (identical)

So the lead follows the instruction, is told it worked, and lands on the same
refusal. Nothing wrong is ever sealed — the whole chain fails closed — but the
export is stranded behind an instruction that cannot be carried out.

## The mixed case is worse than the headline, and decided the fix

The same call with ONE MORE reviewer named partially succeeds, because the extra
reviewer's rows do not conflict:

    POST /assignments {round_number: 1, reviewers: [a, b], corpus: v2} -> 201
        body.corpus_version_id                           =  v2
        body.assignments_created                          =  3
        screening_assignments for round 1                =  v1 x3  AND  v2 x3

One round, two corpora. Reviewer `a` is screening the pre-merge text and
reviewer `b` the post-merge text, at the same position, blinded from each other
— a disagreement manufactured by the tooling and invisible in the response,
which reports a single `corpus_version_id` as though the round were homogeneous.
No single value of that field can describe that round honestly, which is why
reporting the stored corpus is not on its own a sufficient fix: the state must
not be reachable.

## What is asserted here

* A round already holding assignments from corpus A refuses to be re-pointed at
  corpus B — `409 state_conflict`, naming both ids and the round that would
  accept the new corpus. Assignments are append-only at the database, so this is
  a refusal and not a repair.
* The `201` body's `corpus_version_id` is read out of the round, not echoed back
  from the request.
* The refusal does NOT fire on the legitimate cases: a retry with the same
  corpus, a reviewer added to an existing round, a new round, or another stage.
* `create_export`'s G3 remedy is one a lead can carry out — asserted by carrying
  it out and reaching a sealed export, not by reading the sentence.

Measured with this file added and `src/` unchanged (the red run):

    test_a_round_cannot_be_re_pointed_at_a_new_corpus              FAILED
    test_a_round_cannot_end_up_holding_two_corpora                 FAILED
    test_the_new_round_the_refusal_prescribes_is_accepted          FAILED
    test_the_export_refusal_prescribes_a_remedy_a_lead_can_carry   FAILED
    (the four negative controls PASSED — they must, both before and after)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT = tenant("tenant-round-corpus")
LEAD = "round-corpus-lead"
REVIEWER = "round-corpus-reviewer"
SECOND_REVIEWER = "round-corpus-reviewer-2"

#: A shared DOI so the dedup engine clusters the pair, and two titles so a
#: survivorship choice has something to be about. Full-length DOIs: a short one
#: (`10.1/abc`) does not normalise and the pair would not deduplicate.
SHARED_DOI = "10.1056/NEJMoa2021436"
SEALED_TITLE = "Tocilizumab in severe COVID-19"
MERGED_TITLE = "Tocilizumab in severe COVID-19 (merged)"


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


def corpora_of_round(review_id: str, round_number: int) -> dict[str, int]:
    """`corpus_version_id -> row count` for one round, straight out of the table.

    Read as the OWNER with `app.current_tenant` bound. `screening_assignments` is
    `FORCE ROW LEVEL SECURITY`, so an owner read that binds nothing matches no
    rows, returns `{}`, and every assertion below would then be comparing two
    empty dicts — a false green that looks exactly like a passing test. The same
    trap made `0009`'s first backfill a silent no-op.
    """
    url = os.environ.get("MIGRATION_DATABASE_URL") or DB_URL
    engine = sa.create_engine(url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": TENANT})
            rows = conn.execute(
                sa.text(
                    "SELECT corpus_version_id, count(*) FROM screening_assignments "
                    "WHERE review_id = :rid AND round_number = :rn GROUP BY 1"
                ),
                {"rid": review_id, "rn": round_number},
            ).all()
    finally:
        engine.dispose()
    return {str(row[0]): row[1] for row in rows}


def _activated_review(client, stage_definitions: list[dict[str, str]]):  # noqa: ANN001, ANN202
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "round-corpus", "external_key": f"rc-{uuid.uuid4()}", "title": "Round corpus"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": stage_definitions,
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(),
    )
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(),
    )
    assert activated.status_code in (200, 201), activated.text
    return review_id


def _import(client, review_id: str, records: list[dict]) -> None:  # noqa: ANN001
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": records}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text


def _assign(client, review_id: str, stage_id: str, **body):  # noqa: ANN001, ANN003, ANN202
    return client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, **body},
        headers=headers(),
    )


@pytest.fixture
def stranded(client):  # noqa: ANN001, ANN201
    """The measured state: round 1 screened against v1, corpus re-sealed to v2, export refused.

    The ORDER is the fixture. Seal, assign, decide, THEN merge and re-seal — the
    other order leaves the corpus and the decisions agreeing and nothing is
    stranded.

    The re-seal changes the MANIFEST without changing the membership: the merge
    mints a v2 of the survivor, so v2 holds the same three record ids pointing at
    a different version of one of them. That is what makes the re-pointing call
    a FULL conflict — `assignments_created: 0` — which is the headline shape.
    """
    review_id = _activated_review(client, [{"key": "ta", "name": "TA"}])
    doi = f"{SHARED_DOI}.{uuid.uuid4().hex[:8]}"
    _import(
        client,
        review_id,
        [
            {"title": SEALED_TITLE, "year": 2021, "doi": doi},
            {"title": MERGED_TITLE, "year": 2021, "doi": doi},
            {"title": "An unrelated trial", "year": 2019, "doi": f"10.1056/NEJMoa1911.{uuid.uuid4().hex[:8]}"},
        ],
    )

    v1 = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]

    assigned = _assign(
        client, review_id, stage_id, round_number=1, reviewer_subject_ids=[REVIEWER], corpus_version_id=v1
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["assignments_created"] == 3, assigned.text

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    assert len(queue) == 3, queue
    for item in queue:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    run = client.post(f"/api/reviews/{review_id}/deduplication-runs", headers=headers()).json()
    clusters = client.get(
        f"/api/reviews/{review_id}/duplicate-clusters",
        params={"dedup_run_id": run["dedup_run_id"]},
        headers=headers(),
    ).json()["items"]
    assert len(clusters) == 1, f"fixture expected exactly one duplicate cluster, got {clusters}"
    cluster = clusters[0]
    survivor = next(m for m in cluster["members"] if m["title"] == SEALED_TITLE)
    loser = next(m for m in cluster["members"] if m["title"] == MERGED_TITLE)
    resolved = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
        json={
            "resolution_type": "merge",
            "survivor_record_id": survivor["review_record_id"],
            "rationale": "same trial reported twice",
            "survivorship": {"title": loser["review_record_version_id"]},
        },
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), resolved.text

    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text
    v2 = resealed.json()["corpus_version_id"]
    assert v2 != v1, "fixture drift: the re-seal produced no new corpus version"

    assert corpora_of_round(review_id, 1) == {v1: 3}, "fixture drift: round 1 does not hold v1"
    return {"review_id": review_id, "stage_id": stage_id, "v1": v1, "v2": v2}


@pytest.fixture
def two_stages(client):  # noqa: ANN001, ANN201
    """Two stages and two corpus versions, with only stage 1 assigned.

    No merge here: a second corpus version needs only a changed MANIFEST, and one
    more imported record is the cheapest way to get one. This fixture exists for
    the negative control that the binding is per `(stage, round)` and not per
    review.
    """
    review_id = _activated_review(client, [{"key": "ta", "name": "TA"}, {"key": "ft", "name": "Full text"}])
    _import(
        client,
        review_id,
        [{"title": "Stage one paper", "year": 2020, "doi": f"10.1056/NEJMoa2001.{uuid.uuid4().hex[:8]}"}],
    )
    v1 = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).json()["corpus_version_id"]

    stages = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]
    assert len(stages) == 2, stages
    first, second = stages[0]["stage_id"], stages[1]["stage_id"]

    assigned = _assign(client, review_id, first, round_number=1, reviewer_subject_ids=[REVIEWER], corpus_version_id=v1)
    assert assigned.status_code == 201, assigned.text

    # REVIEWER DECIDES ROUND 1 OF `first` BEFORE THIS FIXTURE EVER ASKS FOR A
    # STAGE-2 ASSIGNMENT (`ERS #65`, finding 2). `create_assignments` now
    # refuses to assign a reviewer at stage N while they still owe a decision
    # at stage N-1, and this fixture used to leave REVIEWER's round-1 `first`
    # assignment undecided forever — which is fine for THIS file's own subject
    # (corpus binding), but it is exactly the debt finding 2 now checks, and it
    # would silently zero out every `second`-stage assignment the one test
    # below makes. Deciding it here is also how a real reviewer would behave:
    # finish the earlier round before being handed a later stage.
    round_one_queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": first, "round_number": 1},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    for item in round_one_queue:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": first,
                "round_number": 1,
                "review_record_id": item["review_record_id"],
                "disposition": "include",
            },
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    _import(
        client,
        review_id,
        [{"title": "Stage two paper", "year": 2022, "doi": f"10.1056/NEJMoa2202.{uuid.uuid4().hex[:8]}"}],
    )
    resealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    assert resealed.status_code in (200, 201), resealed.text
    v2 = resealed.json()["corpus_version_id"]
    assert v2 != v1

    # SETTLE BOTH v2 MEMBERS `included` AT STAGE 1 BEFORE STAGE 2 IS ASSIGNED
    # (`ERS #42`).
    #
    # `create_assignments` now scopes stage order N > 1 to records that
    # settled `included` at stage N-1, so `second` needs a settled verdict on
    # both v2 members first, not just the one round 1 already screened. Round
    # 2 of `first` — never read by `corpora_of_round(review_id, 1)`, which this
    # fixture's own tests query at round 1 — carries that settling work so it
    # cannot be mistaken for the round-1 binding under test.
    settling = _assign(client, review_id, first, round_number=2, reviewer_subject_ids=[REVIEWER], corpus_version_id=v2)
    assert settling.status_code == 201, settling.text
    settling_queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": first, "round_number": 2, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    assert len(settling_queue) == 2, settling_queue
    for item in settling_queue:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": first,
                "round_number": 2,
                "review_record_id": item["review_record_id"],
                "disposition": "include",
            },
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    return {"review_id": review_id, "first_stage_id": first, "second_stage_id": second, "v1": v1, "v2": v2}


# ==========================================================================
# The gates.
# ==========================================================================


def test_a_round_cannot_be_re_pointed_at_a_new_corpus(client, stranded) -> None:
    """The headline. `201 corpus_version_id: v2` while the table holds v1 must not survive.

    Two independent assertions, because they fail for different reasons and a
    reader has to be able to tell them apart:

    * the call is REFUSED (it was `201`);
    * whatever the call returns, the round still holds v1 and only v1 — which is
      true before the fix too, and is what makes the `201` a lie rather than a
      partial success.
    """
    review_id, v1, v2 = stranded["review_id"], stranded["v1"], stranded["v2"]

    repointed = _assign(
        client, review_id, stranded["stage_id"], round_number=1, reviewer_subject_ids=[REVIEWER], corpus_version_id=v2
    )

    assert corpora_of_round(review_id, 1) == {v1: 3}, (
        "the database moved. `screening_assignments` is append-only by trigger and "
        "`uq_assignment_once` excludes `corpus_version_id`, so this must be impossible"
    )
    assert repointed.status_code == 409, (
        f"re-pointing round 1 from {v1} to {v2} answered {repointed.status_code} "
        f"with body {repointed.text}. The round still holds {v1}."
    )
    body = repointed.json()
    assert body["code"] == "state_conflict", body
    assert body["bound_corpus_version_ids"] == [v1], body
    assert body["requested_corpus_version_id"] == v2, body
    assert body["next_round_number"] == 2, body
    # The remedy has to be carriable from the body alone, without the prose.
    assert body["round_number"] == 1, body


def test_a_round_cannot_end_up_holding_two_corpora(client, stranded) -> None:
    """The mixed case: adding a reviewer while re-pointing partially succeeded.

    Measured before the fix: `201`, `assignments_created: 3`, and round 1 left
    holding `{v1: 3, v2: 3}` — two reviewers at one blinded position reading
    different text. This is the state that no `corpus_version_id` in the response
    could have described honestly.
    """
    review_id, v1, v2 = stranded["review_id"], stranded["v1"], stranded["v2"]

    split = _assign(
        client,
        review_id,
        stranded["stage_id"],
        round_number=1,
        reviewer_subject_ids=[REVIEWER, SECOND_REVIEWER],
        corpus_version_id=v2,
    )

    held = corpora_of_round(review_id, 1)
    assert held == {v1: 3}, (
        f"round 1 holds {held}: a single round is screening more than one corpus, so two blinded "
        f"reviewers at the same position are reading different text (call returned {split.status_code})"
    )
    assert split.status_code == 409, split.text


def test_the_export_refusal_prescribes_a_remedy_a_lead_can_carry(client, stranded) -> None:
    """G3's remedy, carried out rather than read.

    The old sentence said "re-run the stage against the new corpus". Carried out
    literally — `POST /assignments` on the SAME round with the new corpus — it
    returned `201` and changed nothing, and the export refused again with a
    byte-identical message. Measured before the fix: 422 -> 201 -> 422.

    The assertion is the seal at the end, not the wording: a message that
    prescribes a new round and a service that cannot deliver one would pass a
    string check and strand the lead exactly as before.
    """
    review_id, stage_id, v2 = stranded["review_id"], stranded["stage_id"], stranded["v2"]

    refused = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "new round" in detail.lower(), (
        f"the refusal does not prescribe a new round, which is the only remedy that works: {detail!r}"
    )

    # Now do what it says.
    round_two = _assign(
        client, review_id, stage_id, round_number=2, reviewer_subject_ids=[REVIEWER], corpus_version_id=v2
    )
    assert round_two.status_code == 201, round_two.text
    assert round_two.json()["assignments_created"] == 3, round_two.text
    assert corpora_of_round(review_id, 2) == {v2: 3}

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "round_number": 2, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()["items"]
    assert queue, "round 2 has an empty queue; the remedy is not carriable after all"
    for item in queue:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 2,
                "review_record_id": item["review_record_id"],
                "disposition": "include",
            },
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    sealed = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert sealed.status_code == 201, f"the remedy the refusal prescribes does not reach a sealed export: {sealed.text}"


# ==========================================================================
# Negative controls. A guard that refuses everything passes the gates above.
# ==========================================================================


def test_a_retry_with_the_same_corpus_is_still_a_no_op(client, stranded) -> None:
    """Retry-safety is the reason `ON CONFLICT DO NOTHING` is there at all.

    `create_assignments` is documented as safe to retry — a gateway timeout on a
    corpus of thousands leaves the caller unable to tell a timeout from a failure
    — and `test_assignment_throughput.py` pins that. A guard that turned the
    retry into a `409` would break the only recovery path that route has.
    """
    review_id, v1 = stranded["review_id"], stranded["v1"]

    again = _assign(
        client, review_id, stranded["stage_id"], round_number=1, reviewer_subject_ids=[REVIEWER], corpus_version_id=v1
    )

    assert again.status_code == 201, again.text
    body = again.json()
    assert body["assignments_created"] == 0, body
    assert body["corpus_version_id"] == v1, body
    assert corpora_of_round(review_id, 1) == {v1: 3}


def test_a_no_op_says_how_many_assignments_were_already_there(client, stranded) -> None:
    """`assignments_created: 0` on its own does not distinguish two very different runs.

    "Every row was already there" and "the insert never ran" produce the same
    body. That ambiguity is what let the re-pointing call above read as a
    success: a lead who saw `0` had nothing else to look at. `assignments_existing`
    is what the round held BEFORE this call, so `0 created / 3 existing` reads as
    "already done" and `0 created / 0 existing` reads as "nothing happened".

    Kept apart from the retry control above deliberately: that control has to
    pass both before and after the fix, and this field does not exist before it.
    """
    review_id, v1 = stranded["review_id"], stranded["v1"]

    first = _assign(
        client, review_id, stranded["stage_id"], round_number=2, reviewer_subject_ids=[REVIEWER], corpus_version_id=v1
    )
    assert first.status_code == 201, first.text
    assert first.json()["assignments_existing"] == 0, first.text
    assert first.json()["assignments_created"] == 3, first.text

    again = _assign(
        client, review_id, stranded["stage_id"], round_number=2, reviewer_subject_ids=[REVIEWER], corpus_version_id=v1
    )
    assert again.json()["assignments_existing"] == 3, again.text
    assert again.json()["assignments_created"] == 0, again.text


def test_a_reviewer_added_to_an_existing_round_is_still_accepted(client, stranded) -> None:
    """Widening a round is a legitimate, common act and must not be refused.

    This is the case the guard is closest to breaking: it is a partial conflict
    against an existing round, which is exactly the shape of the mixed case the
    gate above refuses. The corpus is what separates them, and nothing else.
    """
    review_id, v1 = stranded["review_id"], stranded["v1"]

    widened = _assign(
        client,
        review_id,
        stranded["stage_id"],
        round_number=1,
        reviewer_subject_ids=[REVIEWER, SECOND_REVIEWER],
        corpus_version_id=v1,
    )

    assert widened.status_code == 201, widened.text
    assert widened.json()["assignments_created"] == 3, widened.text
    assert widened.json()["corpus_version_id"] == v1
    assert corpora_of_round(review_id, 1) == {v1: 6}


def test_the_new_round_the_refusal_prescribes_is_accepted(client, stranded) -> None:
    """The remedy the `409` names, taken from the `409` body itself.

    Before the fix there is no `409` to read `next_round_number` off, so this
    fails at that KeyError — structural evidence that the refusal does not exist,
    which is weaker than the gates above and recorded as such.
    """
    review_id, v2 = stranded["review_id"], stranded["v2"]

    refused = _assign(
        client, review_id, stranded["stage_id"], round_number=1, reviewer_subject_ids=[REVIEWER], corpus_version_id=v2
    )
    next_round = refused.json()["next_round_number"]

    accepted = _assign(
        client,
        review_id,
        stranded["stage_id"],
        round_number=next_round,
        reviewer_subject_ids=[REVIEWER],
        corpus_version_id=v2,
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["assignments_created"] == 3, accepted.text
    assert accepted.json()["corpus_version_id"] == v2
    assert corpora_of_round(review_id, next_round) == {v2: 3}


def test_a_different_stage_may_name_a_different_corpus(client, two_stages) -> None:
    """The binding is per `(stage, round)`, not per review.

    A two-stage review that re-seals between title/abstract and full text is
    ordinary: the full-text stage screens the corpus as it stands when that stage
    starts. A guard scoped to the review would refuse it, and would look
    identical to a correct one on every other test in this file.
    """
    review_id = two_stages["review_id"]

    second = _assign(
        client,
        review_id,
        two_stages["second_stage_id"],
        round_number=1,
        reviewer_subject_ids=[REVIEWER],
        corpus_version_id=two_stages["v2"],
    )

    assert second.status_code == 201, second.text
    assert second.json()["corpus_version_id"] == two_stages["v2"]
    held = corpora_of_round(review_id, 1)
    assert held == {two_stages["v1"]: 1, two_stages["v2"]: 2}, (
        f"round 1 across BOTH stages should hold one corpus each; got {held}"
    )
