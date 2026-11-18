"""A re-screen must settle the record. It was being discarded.

The fourth finding from the adversarial verification of `#42`, and the worst of
them: it seals a wrong inclusion silently rather than refusing loudly.

`#42` settled a record at `max(reached)` over `StagePosition`, which sorts
`(stage_order, stage_key, round_number)`. `round_number` sits last, justified in
`flow_derivation` as *"stage 2 round 1 is later work than stage 1 round 4"*.
Measured, that is false:

    stage ta r1  include, include
    stage ft r1  include, include        -> included: 1
    stage ta r2  EXCLUDE, EXCLUDE        -> included: 1        (unchanged)
    POST /exports -> 201, included_count 1

Both reviewers unanimously excluded the record in the newest work the review
contains, and the ledger discarded it: no conflict, no `not_yet_screened`, no
omission entry, no warning. The record was sealed into an append-only artifact as
included, with an empty `omissions` array, and `:verify` answers verified.

## What the ledger actually knows

Not which stage came later — a stage order cannot answer that once rounds exist,
because a round is a re-screen and a re-screen happens whenever the lead calls
for it. What it knows is WHEN each decision was taken: `screening_decisions` is
append-only and carries `decided_at`.

So the settling position is the position whose most recent current decision is
the latest, tie-broken by `StagePosition` for determinism. `decided_at` is a
persisted, immutable column, never a clock read at derivation time, so the
derivation stays a pure function of stored history and `canonical_counts_hash`
stays reproducible.

## The control that matters

`ta r1 -> ta r2 -> ft r1` is a legitimate, ordinary flow: a stage is re-screened,
then the review moves on. A rule of "a later round at an earlier stage wins"
would settle that at `ta r2` and break it. Only "the newest work" gets both this
and the defect right, which is why
:func:`test_a_rescreen_before_the_next_stage_does_not_win` is here.
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

TENANT = tenant("tenant-newest-work")
LEAD = "newest-work-lead"
#: Settle title/abstract eligibility for full text WITHOUT touching r1/r2's own
#: debt there — see `two_stage_review`'s "SETTLE `included`" comment.
SETTLER_1 = "newest-work-settler-1"
SETTLER_2 = "newest-work-settler-2"


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


@pytest.fixture
def two_stage_review(client):  # noqa: ANN001, ANN201
    """One record, two stages, two reviewers, one sealed corpus."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "newest-work", "external_key": f"nw-{uuid.uuid4()}", "title": "Newest work"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}, {"key": "ft", "name": "Full text"}],
            "exclusion_reason_vocabulary": ["wrong_design", "wrong_population"],
            "reviewers_required": 2,
        },
        headers=headers(),
    ).json()["protocol_version_id"]
    client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers())
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol, "rationale": "go"},
        headers=headers(),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "The disputed study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code in (200, 201)

    stages = {
        s["stage_key"]: s["stage_id"]
        for s in client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]
    }

    # SETTLE `included` AT TITLE/ABSTRACT BEFORE FULL TEXT CAN BE ASSIGNED
    # (`ERS #42`: `create_assignments` now scopes stage order N > 1 to records
    # that settled `included` at stage N-1). SETTLER_1/2 carry that vote —
    # never r1's or r2's — for a SECOND reason beyond the one this fixture
    # always had: `ERS #65` finding 2 now also refuses to hand a stage-N
    # assignment to a reviewer who still owes a decision at stage N-1, and r1
    # and r2 must stay owing an UNDECIDED title/abstract position (every test
    # below decides it FRESH, as its first, defining act — some `include`, some
    # `exclude`, some at round 1, some at round 2). So r1/r2 cannot be named in
    # ANY title/abstract assignment before full text is granted below, or the
    # guard would refuse them there. See `lead_is_also_a_reviewer` in
    # `tests/security/test_one_stage_decided_is_not_every_stage_decided.py`
    # for the identical fix, written up at more length.
    settled_assignment = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ta"], "reviewer_subject_ids": [SETTLER_1, SETTLER_2]},
        headers=headers(),
    )
    assert settled_assignment.status_code in (200, 201), settled_assignment.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stages["ta"]},
        headers=headers(SETTLER_1, "reviewer"),
    ).json()["items"][0]["review_record_id"]
    for who in (SETTLER_1, SETTLER_2):
        settled = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stages["ta"],
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": "include",
            },
            headers=headers(who, "reviewer"),
        )
        assert settled.status_code == 201, settled.text

    # FULL TEXT, WHILE r1/r2 OWE NOTHING AT TITLE/ABSTRACT — neither holds an
    # assignment there yet.
    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ft"], "reviewer_subject_ids": ["r1", "r2"]},
        headers=headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text

    # ONLY NOW: title/abstract for r1/r2, deliberately left undecided — every
    # test below casts the first real vote there itself. Appending this
    # assignment does not touch the full-text one already granted.
    assigned_ta = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ta"], "reviewer_subject_ids": ["r1", "r2"]},
        headers=headers(),
    )
    assert assigned_ta.status_code in (200, 201), assigned_ta.text

    return {"review_id": review_id, "stages": stages, "record_id": record_id}


def _screen(client, fixture, stage_key: str, disposition: str, *, round_number: int = 1, reason=None) -> None:  # noqa: ANN001
    """Both reviewers, unanimously, at one stage and round."""
    if round_number != 1:
        assigned = client.post(
            f"/api/reviews/{fixture['review_id']}/assignments",
            json={
                "stage_id": fixture["stages"][stage_key],
                "reviewer_subject_ids": ["r1", "r2"],
                "round_number": round_number,
            },
            headers=headers(),
        )
        assert assigned.status_code in (200, 201), assigned.text
    for who in ("r1", "r2"):
        got = client.post(
            f"/api/reviews/{fixture['review_id']}/decisions",
            json={
                "stage_id": fixture["stages"][stage_key],
                "round_number": round_number,
                "review_record_id": fixture["record_id"],
                "disposition": disposition,
                "exclusion_reason_code": reason,
                "rationale": "screened",
            },
            headers=headers(who, "reviewer"),
        )
        assert got.status_code == 201, got.text


def _counts(client, review_id: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{review_id}/flow", headers=headers())
    assert got.status_code == 200, got.text
    return got.json()["counts"]


# --------------------------------------------------------------------------


def test_a_rescreen_after_a_later_stage_settles_the_record(client, two_stage_review) -> None:
    """THE DEFECT. A unanimous exclusion, discarded, and the record sealed."""
    fixture = two_stage_review
    _screen(client, fixture, "ta", "include")
    _screen(client, fixture, "ft", "include")
    assert _counts(client, fixture["review_id"])["included"] == 1

    _screen(client, fixture, "ta", "exclude", round_number=2, reason="wrong_design")

    counts = _counts(client, fixture["review_id"])
    assert counts["excluded"] == 1 and counts["included"] == 0, (
        "both reviewers excluded the record in the newest work this review contains and the ledger "
        f"discarded it: {counts}"
    )
    assert counts["exclusion_reasons"] == {"wrong_design": 1}, counts

    exported = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers())
    assert exported.status_code == 201, exported.text
    assert exported.json()["included_count"] == 0, (
        "the export sealed a record that the review's newest work excluded: " + exported.text
    )


def test_a_rescreen_before_the_next_stage_does_not_win(client, two_stage_review) -> None:
    """THE CONTROL, and the one that rules out the simpler wrong fix.

    `ta r1 -> ta r2 -> ft r1` is an ordinary flow: title/abstract is re-screened,
    then the review moves on to full text. A rule of "a later round at an earlier
    stage wins" would settle this at `ta r2` and be wrong. Only "the newest work
    settles it" gets both this and the defect above right.
    """
    fixture = two_stage_review
    _screen(client, fixture, "ta", "include")
    _screen(client, fixture, "ta", "exclude", round_number=2, reason="wrong_design")
    assert _counts(client, fixture["review_id"])["excluded"] == 1

    _screen(client, fixture, "ft", "include")

    counts = _counts(client, fixture["review_id"])
    assert counts["included"] == 1 and counts["excluded"] == 0, (
        "the full-text stage is the newest work in this review and must settle the record: " + json.dumps(counts)
    )


def test_the_ordinary_two_stage_flow_is_unchanged(client, two_stage_review) -> None:
    """NEGATIVE CONTROL. Every live review is this shape; it must not move.

    One round per stage, so there is nothing for a round rule to do and the
    answer must be exactly what `#42` produced.
    """
    fixture = two_stage_review
    _screen(client, fixture, "ta", "include")
    _screen(client, fixture, "ft", "exclude", reason="wrong_population")

    counts = _counts(client, fixture["review_id"])
    assert counts["excluded"] == 1 and counts["included"] == 0, counts
    assert counts["exclusion_reasons"] == {"wrong_population": 1}, counts
    assert counts["not_yet_screened"] == 0, counts


def test_a_rescreen_in_progress_reopens_the_record(client, two_stage_review) -> None:
    """A re-screen that has started and not finished is not a settled record.

    One reviewer of the two has reported at `ta r2`. The newest work is that
    round, and it is incomplete, so the record is `not_yet_screened` rather than
    still carrying the inclusion the superseded round gave it. This is the
    behaviour that makes the fix a statement about the review's state rather than
    a way of picking whichever answer is convenient.
    """
    fixture = two_stage_review
    _screen(client, fixture, "ta", "include")
    _screen(client, fixture, "ft", "include")
    assert _counts(client, fixture["review_id"])["included"] == 1

    assert client.post(
        f"/api/reviews/{fixture['review_id']}/assignments",
        json={"stage_id": fixture["stages"]["ta"], "reviewer_subject_ids": ["r1", "r2"], "round_number": 2},
        headers=headers(),
    ).status_code in (200, 201)
    assert (
        client.post(
            f"/api/reviews/{fixture['review_id']}/decisions",
            json={
                "stage_id": fixture["stages"]["ta"],
                "round_number": 2,
                "review_record_id": fixture["record_id"],
                "disposition": "exclude",
                "exclusion_reason_code": "wrong_design",
                "rationale": "half a round",
            },
            headers=headers("r1", "reviewer"),
        ).status_code
        == 201
    )

    counts = _counts(client, fixture["review_id"])
    assert counts["not_yet_screened"] == 1 and counts["included"] == 0, (
        "a re-screen is under way and the record still counts as included: " + json.dumps(counts)
    )

    exported = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers())
    assert exported.status_code == 422, (
        f"the export sealed while a re-screen was open (HTTP {exported.status_code}): {exported.text}"
    )


def test_the_collector_times_every_position_it_reports(client, two_stage_review) -> None:
    """THE PIN for the untimed fallback.

    `settling_position` uses time only when every position a record reached
    carries one, and falls back to stage order otherwise — which is the OLD,
    defective ordering. That fallback is correct for a `FlowInputs` built by
    hand, and it must never be what the live collector produces, because then the
    defect would be back with a passing suite over it.

    Asserted structurally against `_collect`'s own output rather than through a
    count, because a count can agree by luck: with one record and two positions
    the two orderings coincide half the time.

    ## Decisions, and deliberately not adjudications

    An adjudication contributes a position but no time, because it backfills an
    older stage rather than advancing the record — see `flow._collect`. Through
    this collector that is invisible: a conflict needs `reviewers_required`
    disagreeing decisions at its own position to exist at all, so every
    adjudicated position has already been timed by those decisions. The fixture
    below adjudicates one anyway, so the assertion is made over a review that
    actually holds an adjudication rather than over a shape that cannot have one.

    Checked by mutation rather than assumed: leaving any round-2 position untimed
    fails this test and two of the behaviour tests above.
    """
    import uuid as _uuid

    from actor import Actor, ActorRole
    from application.services.flow import _collect
    from database import actor_scope

    fixture = two_stage_review
    _screen(client, fixture, "ta", "include")
    _screen(client, fixture, "ft", "include")
    _screen(client, fixture, "ta", "exclude", round_number=2, reason="wrong_design")

    # A disagreement at ft round 2, so the review holds a conflict as well as
    # plain agreement — the shape the coverage claim has to hold over.
    client.post(
        f"/api/reviews/{fixture['review_id']}/assignments",
        json={"stage_id": fixture["stages"]["ft"], "reviewer_subject_ids": ["r1", "r2"], "round_number": 2},
        headers=headers(),
    )
    for who, disposition in (("r1", "include"), ("r2", "exclude")):
        client.post(
            f"/api/reviews/{fixture['review_id']}/decisions",
            json={
                "stage_id": fixture["stages"]["ft"],
                "round_number": 2,
                "review_record_id": fixture["record_id"],
                "disposition": disposition,
                "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
                "rationale": "disagree",
            },
            headers=headers(who, "reviewer"),
        )

    actor = Actor(tenant_id=TENANT, subject_id=LEAD, role=ActorRole.LEAD)
    with actor_scope(actor) as session:
        inputs = _collect(session, _uuid.UUID(fixture["review_id"]))

    reached = {key for key in inputs.decisions} | {key for key in inputs.adjudications}
    assert reached, "the fixture produced no screening work at all"
    untimed = sorted(str(position) for position, _ in reached - set(inputs.worked_at))
    assert not untimed, (
        "the collector reported positions it did not time, so `settling_position` falls back to "
        f"stage order and the discarded-round defect is live again for these: {untimed}"
    )


def test_a_retracted_earlier_verdict_reopens_the_record(client, two_stage_review) -> None:
    """The supersession shape of the same rule, and it is a DIFFERENT one.

    The cases above are about new ROUNDS. This is about a correction: a reviewer
    supersedes their own title/abstract verdict after full text has already been
    decided. That correction is the newest work on the record, and it creates a
    live disagreement at title/abstract.

    Before settling by time the record stayed `included`, because full text sits
    higher in stage order — so the ledger reported an inclusion while one of the
    two reviewers had retracted their vote for it. The export refused either way,
    on the conflict rather than on the counts, so the ledger was wrong quietly
    while the gate happened to be right for another reason.

    Worth its own test because a future change could restore stage-order settling
    for rounds and leave this shape broken, or the reverse.
    """
    fixture = two_stage_review
    ids = {}
    for who in ("r1", "r2"):
        got = client.post(
            f"/api/reviews/{fixture['review_id']}/decisions",
            json={
                "stage_id": fixture["stages"]["ta"],
                "round_number": 1,
                "review_record_id": fixture["record_id"],
                "disposition": "include",
                "rationale": "screened",
            },
            headers=headers(who, "reviewer"),
        )
        assert got.status_code == 201, got.text
        ids[who] = got.json()["decision_id"]
    _screen(client, fixture, "ft", "include")
    assert _counts(client, fixture["review_id"])["included"] == 1

    retracted = client.post(
        f"/api/reviews/{fixture['review_id']}/decisions",
        json={
            "stage_id": fixture["stages"]["ta"],
            "round_number": 1,
            "review_record_id": fixture["record_id"],
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
            "rationale": "on reflection it never qualified",
            "supersedes_decision_id": ids["r1"],
        },
        headers=headers("r1", "reviewer"),
    )
    assert retracted.status_code == 201, retracted.text

    counts = _counts(client, fixture["review_id"])
    assert counts["included"] == 0 and counts["not_yet_screened"] == 1, (
        "one of the two reviewers retracted their title/abstract vote and the ledger still reports the "
        "record included: " + json.dumps(counts)
    )
    refused = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers())
    assert refused.status_code == 422, refused.text
