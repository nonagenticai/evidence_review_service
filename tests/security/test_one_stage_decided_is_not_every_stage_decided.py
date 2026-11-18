"""Deciding at one stage unlocked every other reviewer's verdict at every stage.

`visible_decisions` documents the rule L1 cannot express: **you may not read
another reviewer's decision on a record you still owe a decision on.** It exists
because a lead may read across reviewers under the RLS policy, and nothing stops
a lead from also being an assigned reviewer — so a screening lead could read
their partner's verdict before forming their own.

`records_the_caller_still_owes` computed that debt as `assigned - decided` keyed
by `review_record_id` alone, while both `screening_assignments` and
`screening_decisions` are keyed by `(stage_id, round_number, review_record_id)`.
One decision anywhere in the review therefore discharged the debt on that record
everywhere in it.

Measured on `21685a6`, two stages, the lead also assigned as a reviewer at both:

    OTHER decides at title/abstract and at full text
    lead has decided nothing            -> lead sees 0 of OTHER's decisions
    lead decides at title/abstract ONLY -> lead sees 2 of OTHER's decisions

The second of those is OTHER's FULL-TEXT verdict, on a stage the lead has not
ruled on and is still assigned to. Exactly the disclosure the module refuses at
stage one, reached by walking one stage further — and `#42`, which made a
two-stage review able to finish at all, is what put that stage within reach.

Rounds are in the key for the same reason: a re-screen is a fresh judgement, and
having decided round 1 is not having decided round 2.

## Why this file asserts on `stage_id` rather than on a count

My first probe of this printed `LEAK: False` while its own numbers showed the
leak, because it compared a `stage_id` that the decision payload does not carry —
the comparison could never be true. A count alone is not much better: with one
record and two stages, "sees 1" is the right answer both before and after if the
one it sees is the wrong one. So the assertions below name the stage each visible
decision belongs to, which needs `stage_id` on the view.
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

TENANT = tenant("tenant-stage-blinding")
LEAD = "stage-blinding-lead"
OTHER = "stage-blinding-other"
#: A second lead who screens nothing in any of these reviews.
#:
#: `/conflicts` withholds a conflict from a caller who still owes a decision at
#: its position — row, counters and all — so the LEAD, who is the actor these
#: fixtures build around precisely BECAUSE they have not yet reported at full
#: text, can no longer be asked for the `conflict_id`. This subject can: lead
#: authority comes from the realm role as well as from having created the review
#: (`reviews.require_review_lead`), and holding no assignment is what makes the
#: withholding not apply — the same caller property
#: `test_a_lead_who_is_not_screening_is_unaffected` asserts on the read side.
WITNESS = "stage-blinding-witness"
#: Two reviewers who play no other part in this file's scenarios. Used ONLY to
#: settle a record `included` at title/abstract so `create_assignments` will
#: mint a full-text assignment at all (`ERS #42`) — see
#: `lead_is_also_a_reviewer`'s docstring for why LEAD or OTHER cannot be used
#: for this instead.
SETTLER_A = "stage-blinding-settler-a"
SETTLER_B = "stage-blinding-settler-b"


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
def lead_is_also_a_reviewer(client):  # noqa: ANN001, ANN201
    """Two stages, one record, and the lead assigned as a reviewer at both.

    The lead is the review's creator, which is how `require_review_lead` grants
    lead authority — so this is not a contrived actor, it is the ordinary way a
    small team runs a review.

    ## Assignment order, and why it changed (`ERS #65`, finding 2)

    `create_assignments` now refuses to assign a reviewer at stage N while they
    still OWE a decision at stage N-1 — an assignment held with nothing decided
    against it — which is exactly the state this fixture used to put LEAD and
    OTHER in before requesting their full-text assignment: named at
    title/abstract, deliberately left undecided, and handed a full-text queue
    anyway. That is finding 2 itself, so the fixture cannot build its subject
    that way any more.

    It still needs the SAME end state, because that dual debt — assigned and
    undecided at BOTH stages at once — is what every test below reads. The fix
    is ORDER, not a smaller guarantee: request the full-text assignment while
    LEAD and OTHER hold NO title/abstract assignment at all. Owing is
    `assigned - decided` everywhere in this service
    (`records_the_caller_still_owes`); an unassigned reviewer owes nothing by
    that same definition, so the guard has nothing to refuse. SETTLER_A and
    SETTLER_B carry the title/abstract work that makes the record eligible for
    full text in the first place; LEAD and OTHER are added to title/abstract —
    deliberately left undecided — only AFTER full text is already granted,
    which does not touch it: assignments are append-only.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "stage-blinding", "external_key": f"sb-{uuid.uuid4()}", "title": "Stage blinding"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}, {"key": "ft", "name": "Full text"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
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
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stages = {
        s["stage_key"]: s["stage_id"]
        for s in client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"]
    }
    # SETTLE THE RECORD `included` AT TITLE/ABSTRACT BEFORE FULL TEXT CAN BE
    # ASSIGNED (`ERS #42`: `create_assignments` now scopes stage order N > 1 to
    # records that settled `included` at stage N-1). The two VOTES are
    # SETTLER_A/B's, never LEAD's or OTHER's — either of those deciding here
    # would discharge a debt of theirs and make them mutually visible before
    # any test below has run, which is exactly the blank slate every test in
    # this file assumes.
    #
    # LEAD and OTHER are NOT named in this assignment (unlike before — see
    # "Assignment order" above): naming them here, undecided, is precisely the
    # state `create_assignments` now refuses to hand a full-text queue to.
    settled_assignment = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ta"], "reviewer_subject_ids": [SETTLER_A, SETTLER_B]},
        headers=headers(),
    )
    assert settled_assignment.status_code in (200, 201), settled_assignment.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stages["ta"]},
        headers=headers(SETTLER_A, "reviewer"),
    ).json()["items"][0]["review_record_id"]
    for settler in (SETTLER_A, SETTLER_B):
        settled = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stages["ta"],
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": "include",
            },
            headers=headers(settler, "reviewer"),
        )
        assert settled.status_code == 201, settled.text

    # FULL TEXT, WHILE LEAD AND OTHER OWE NOTHING AT TITLE/ABSTRACT — they hold
    # no assignment there yet, so the guard above has nothing to refuse.
    assigned_ft = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ft"], "reviewer_subject_ids": [LEAD, OTHER]},
        headers=headers(),
    )
    assert assigned_ft.status_code in (200, 201), assigned_ft.text

    # ONLY NOW: title/abstract for LEAD and OTHER, deliberately left undecided
    # forever by this fixture. Appending an assignment does not touch the
    # full-text one already granted — assignments are append-only — so this is
    # what puts LEAD and OTHER in the dual-debt state (owing BOTH stages at
    # once) every test below needs, reached without ever asking
    # `create_assignments` to hand a stage-N queue to someone who still owed
    # stage N-1.
    assigned_ta = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stages["ta"], "reviewer_subject_ids": [LEAD, OTHER]},
        headers=headers(),
    )
    assert assigned_ta.status_code in (200, 201), assigned_ta.text

    return {"review_id": review_id, "stages": stages, "record_id": record_id}


def _decide(client, fixture, who: str, role: str, stage_key: str, disposition: str, *, round_number: int = 1):  # noqa: ANN001, ANN201
    return client.post(
        f"/api/reviews/{fixture['review_id']}/decisions",
        json={
            "stage_id": fixture["stages"][stage_key],
            "round_number": round_number,
            "review_record_id": fixture["record_id"],
            "disposition": disposition,
            "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
            "rationale": "screened",
        },
        headers=headers(who, role),
    )


def _others_visible_to_the_lead(client, fixture) -> list[dict]:  # noqa: ANN001
    got = client.get(f"/api/reviews/{fixture['review_id']}/decisions", headers=headers(LEAD, "lead"))
    assert got.status_code == 200, got.text
    return [d for d in got.json()["items"] if d.get("reviewer_subject_id") not in (None, LEAD)]


# --------------------------------------------------------------------------


def test_deciding_one_stage_does_not_disclose_the_other_stage(client, lead_is_also_a_reviewer) -> None:
    """THE LEAK. The lead reads OTHER's full-text verdict before casting theirs."""
    fixture = lead_is_also_a_reviewer
    assert _decide(client, fixture, OTHER, "reviewer", "ta", "exclude").status_code == 201
    assert _decide(client, fixture, OTHER, "reviewer", "ft", "include").status_code == 201

    assert _others_visible_to_the_lead(client, fixture) == [], (
        "the lead owes both stages and can already see the other reviewer's work"
    )

    assert _decide(client, fixture, LEAD, "lead", "ta", "include").status_code == 201

    visible = _others_visible_to_the_lead(client, fixture)
    stages = {d["stage_id"] for d in visible}
    assert fixture["stages"]["ft"] not in stages, (
        "the lead decided title/abstract and can now read the other reviewer's FULL-TEXT decision, "
        f"which they have not ruled on and are still assigned to: {visible}"
    )


def test_the_stage_they_have_decided_is_still_visible(client, lead_is_also_a_reviewer) -> None:
    """NEGATIVE CONTROL. The rule is "until you vote", not "never".

    Once the lead has cast their own title/abstract verdict there is nothing left
    to influence at that stage, and they need to see the disagreement to
    adjudicate it. Without this, hiding EVERYTHING would pass the test above
    while making adjudication impossible.
    """
    fixture = lead_is_also_a_reviewer
    _decide(client, fixture, OTHER, "reviewer", "ta", "exclude")
    _decide(client, fixture, OTHER, "reviewer", "ft", "include")
    _decide(client, fixture, LEAD, "lead", "ta", "include")

    visible = _others_visible_to_the_lead(client, fixture)
    stages = {d["stage_id"] for d in visible}
    assert fixture["stages"]["ta"] in stages, (
        "the lead has cast their own title/abstract verdict and still cannot see the disagreement "
        f"they have to adjudicate: {visible}"
    )


def test_a_later_round_owes_its_own_decision(client, lead_is_also_a_reviewer) -> None:
    """A re-screen is a fresh judgement, so round 1 does not discharge round 2.

    Same defect one field over: keying the debt by `(stage, record)` and not by
    `(stage, round, record)` would pass every assertion above and still disclose
    the partner's re-screen verdict before the lead casts theirs.
    """
    fixture = lead_is_also_a_reviewer
    _decide(client, fixture, OTHER, "reviewer", "ta", "exclude")
    _decide(client, fixture, LEAD, "lead", "ta", "include")
    _decide(client, fixture, OTHER, "reviewer", "ft", "include")
    _decide(client, fixture, LEAD, "lead", "ft", "include")

    assert client.post(
        f"/api/reviews/{fixture['review_id']}/assignments",
        json={"stage_id": fixture["stages"]["ta"], "reviewer_subject_ids": [LEAD, OTHER], "round_number": 2},
        headers=headers(),
    ).status_code in (200, 201)
    assert _decide(client, fixture, OTHER, "reviewer", "ta", "exclude", round_number=2).status_code == 201

    visible = _others_visible_to_the_lead(client, fixture)
    rounds = {(d["stage_id"], d["round_number"]) for d in visible}
    assert (fixture["stages"]["ta"], 2) not in rounds, (
        "the lead owes a round-2 verdict and can already read the other reviewer's: " + json.dumps(visible)
    )
    assert (fixture["stages"]["ta"], 1) in rounds, (
        "round 1 is settled for both of them and must still be readable: " + json.dumps(visible)
    )


def test_a_lead_who_is_not_screening_is_unaffected(client) -> None:
    """NEGATIVE CONTROL. The rule is a caller property, not a role property.

    A lead with no assignments owes nothing, so nothing is hidden from them —
    which is the whole reason adjudication works. A fix that keyed off the role
    would break this and pass everything above.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "stage-blinding", "external_key": f"sb2-{uuid.uuid4()}", "title": "Bystander lead"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
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
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["rv-one", "rv-two"]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers("rv-one", "reviewer"),
    ).json()["items"][0]["review_record_id"]
    for who, disposition in (("rv-one", "include"), ("rv-two", "exclude")):
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": disposition,
                "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
                "rationale": "screened",
            },
            headers=headers(who, "reviewer"),
        )

    got = client.get(f"/api/reviews/{review_id}/decisions", headers=headers(LEAD, "lead"))
    assert got.status_code == 200, got.text
    assert len(got.json()["items"]) == 2, (
        "a lead who is screening nothing owes nothing and must see both reviewers, or no disagreement "
        "in this service can ever be adjudicated: " + got.text
    )


# --------------------------------------------------------------------------
# The write side of the same rule, and the shape it renders as.
# --------------------------------------------------------------------------


def _conflicted_at_full_text(client, fixture) -> str:  # noqa: ANN001
    """Two OTHER reviewers disagree at full text; the lead has decided only ta.

    Three reviewers on the stage, so the disagreement is between people who are
    not the lead — the lead's problem here is not that they took part, it is that
    they have not yet reported and so may not see.
    """
    review_id = fixture["review_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        # All three named: the endpoint refuses a call that names fewer than the
        # stage's `reviewers_required`, and the two already assigned are
        # append-only no-ops. Adding one reviewer is spelled as re-naming the set.
        json={"stage_id": fixture["stages"]["ft"], "reviewer_subject_ids": [LEAD, OTHER, "r-three"]},
        headers=headers(),
    )
    assert assigned.status_code in (200, 201), assigned.text
    for got in (
        _decide(client, fixture, LEAD, "lead", "ta", "include"),
        _decide(client, fixture, OTHER, "reviewer", "ft", "include"),
        _decide(client, fixture, "r-three", "reviewer", "ft", "exclude"),
    ):
        assert got.status_code == 201, got.text

    # READ BY A LEAD WHO OWES NOTHING, and not by the LEAD this fixture is about.
    #
    # `/conflicts` now drops a conflict from `items` AND from every counter while
    # the caller still owes a decision at its position, so the lead's own panel
    # is exactly where this id cannot be found — that withholding is the state
    # these tests exercise. The three alternatives, and why this one:
    #
    #   * Have the LEAD record their full-text decision first. Rejected outright:
    #     it discharges the debt, and the debt IS the fixture.
    #   * Read `screening_conflicts` from the database. It would work, and it
    #     would make every one of these tests pass over a route that never
    #     served the id — the write-side cases below then assert `409` against a
    #     `conflict_id` no API caller in their scenario could hold. A security
    #     test that reaches around the API to build its subject is testing a
    #     smaller system than the one that ships.
    #   * A second endpoint. There is none that names a conflict without the
    #     same gate; `:unblind` and `:resolve` both take the id, they do not
    #     give it.
    #
    # So: the ordinary adjudication path. `WITNESS` holds lead authority and no
    # assignment, which is the caller `/conflicts` was always meant for and the
    # one `test_a_lead_who_is_not_screening_is_unaffected` already pins. Nothing
    # is reached around, and the id arrives from the route that publishes it.
    conflicts = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers(WITNESS, "lead")).json()["items"]
    at_full_text = [c for c in conflicts if c["stage_id"] == fixture["stages"]["ft"]]
    assert at_full_text, f"the fixture opened no full-text conflict: {conflicts}"

    # THE FIXTURE'S OWN PREMISE, asserted rather than assumed. Every test below
    # is about what the LEAD may or may not do with a conflict they owe, and all
    # of them would still pass if the withholding silently stopped working —
    # they read the id from WITNESS either way.
    lead_panel = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()
    assert at_full_text[0]["conflict_id"] not in {c["conflict_id"] for c in lead_panel["items"]}, (
        "the lead owes their full-text verdict and the conflict is still in their panel: " + json.dumps(lead_panel)
    )
    assert lead_panel["owed_hidden"] >= 1, (
        "the conflict was withheld from the lead without saying so, which makes 'no disagreements' and "
        "'none you may be told about' the same response: " + json.dumps(lead_panel)
    )
    return at_full_text[0]["conflict_id"]


def test_an_unblinded_conflict_the_caller_owes_reads_redacted_not_empty(client, lead_is_also_a_reviewer) -> None:
    """An empty list is not a redaction.

    `visible_decisions` withholds rows at a position the caller still owes,
    whatever the unblinding says. Unblinded AND owed therefore rendered as `[]` —
    the exact confusion the `REDACTED` literal exists to prevent, and a client
    reading it as "there is nothing here" would conclude the conflict was empty.

    This state pre-dates the per-position debt; keying the debt by record alone
    only changed WHICH cases reach it.

    ## And an absent row is not a redaction either

    `/conflicts` now drops a conflict the caller owes from `items` and from every
    counter, because the count moving `0 -> 1` was itself the partner's verdict.
    This row survives that because the unblinding NAMES the LEAD: an `:unblind`
    is a recorded, audited authorisation carrying an actor and a reason, and
    hiding the row afterwards would make it a control with no observable effect —
    the lead opens the door and cannot tell "still blinded to the contents" from
    "the unblinding failed". So the debt withholds the CONTENTS and the
    unblinding settles whether the caller may be told the conflict is there, and
    the assertion below is unchanged: `redacted`, never `[]` and never absent.

    ## The unblinding actor, and why it changed (`ERS #65`, finding 1)

    This used to be `headers()` — LEAD themselves, naming LEAD in `disclose_to`.
    `unblind_conflict` now refuses that exact call: it is the caller unblinding
    to themselves a conflict they still owe, which is the self-service half of
    finding 1 and would 409 here. This test is not about who may unblind —
    `test_someone_elses_unblinding_does_not_open_the_row` and
    `test_the_ruling_is_redacted_at_a_position_the_caller_owes` already use a
    bystander lead for exactly that reason — so it moves onto the same actor
    rather than being the one place in the file still asserting the leak's
    entry point works.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)

    unblinded = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers("bystander-lead", "lead"),
    )
    assert unblinded.status_code in (200, 201), unblinded.text

    row = next(
        c
        for c in client.get(f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers()).json()["items"]
        if c["conflict_id"] == conflict_id
    )
    assert row["dispositions"] == "redacted", (
        "the lead owes their own full-text verdict, so the dispositions are withheld — and saying so "
        f"with an empty list makes 'you may not see this' look like 'there is nothing here': {row}"
    )


def test_someone_elses_unblinding_does_not_open_the_row(client, lead_is_also_a_reviewer) -> None:
    """THE CARVE-OUT IS THE AUDIENCE, NOT THE EXISTENCE OF AN EVENT.

    The test above is the only one that reads a conflict the caller owes, and it
    would pass just as well against a `/conflicts` that readmitted the row
    whenever ANY unblinding existed for it. That version is the leak again, one
    party over: `disclose_to` defaults to the unblinding lead alone, so a second
    lead unblinding a conflict for their own adjudication would disclose its
    existence — and therefore the partner's disposition, since a conflict opens
    only on disagreement — to a lead who is still screening that record and was
    named by nobody. `ER-M008` added the declared audience precisely so that
    widening blinding is something somebody chose.

    So the unblinding here names the bystander ONLY, and the LEAD must still be
    told nothing.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)

    unblinded = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": ["bystander-lead"]},
        headers=headers("bystander-lead", "lead"),
    )
    assert unblinded.status_code in (200, 201), unblinded.text
    assert LEAD not in unblinded.json()["disclosed_to_subject_ids"], (
        "the fixture put the lead in the audience, so this measures the carve-out and not its key: " + unblinded.text
    )

    panel = client.get(f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers()).json()
    assert conflict_id not in {c["conflict_id"] for c in panel["items"]}, (
        "another lead unblinded this conflict to themselves and it appeared in the panel of a lead who "
        "still owes the verdict — who therefore now knows their partner disagreed: " + json.dumps(panel)
    )
    assert panel["total"] == 0 and panel["total_open"] == 0 and panel["owed_hidden"] == 1, (
        "the row is gone and a counter still moved on the partner's verdict: " + json.dumps(panel)
    )

    # THE OTHER HALF, so this cannot pass by hiding the conflict from everyone.
    for_the_bystander = client.get(
        f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers("bystander-lead", "lead")
    ).json()
    assert conflict_id in {c["conflict_id"] for c in for_the_bystander["items"]}, (
        "the lead who recorded the unblinding cannot see what they unblinded: " + json.dumps(for_the_bystander)
    )


def test_a_lead_who_owes_the_stage_cannot_adjudicate_it(client, lead_is_also_a_reviewer) -> None:
    """THE WRITE SIDE. `:resolve` answered 200 on a conflict it would not show.

    The read rule was never applied to the write, so a lead could rule blind on a
    disagreement the service was actively withholding from them — and that ruling
    is what the ledger settles the record on.

    The separation-of-duty trigger does not cover it: that refuses a lead who
    VOTED in the conflict, and this one has not voted at all, which is precisely
    what makes the ruling uninformed rather than partial.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)
    client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers(),
    )

    refused = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:resolve",
        json={"disposition": "include", "rationale": "ruling"},
        headers=headers(),
    )
    assert refused.status_code == 409, (
        f"a lead ruled on a disagreement the service refused to show them (HTTP {refused.status_code}): " + refused.text
    )
    assert "decision" in refused.text.lower(), "the refusal does not say what to do about it: " + refused.text


def test_recording_the_decision_does_not_open_the_door_either(client, lead_is_also_a_reviewer) -> None:
    """BOTH DOORS ARE CLOSED for a screening lead, and that is the design.

    My refusal first advised "record your decision first". Measured, that advice
    is wrong: the separation-of-duty trigger then refuses the same caller as
    "an adjudicator who recorded a decision in this conflict". A lead assigned to
    a record cannot rule on a disagreement about it, blind OR informed.

    This is worth a test rather than a comment because it is the consequence of
    the gate above and it is invisible from the gate's own test. The two real
    exits are the next two cases.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)
    client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers(),
    )
    assert _decide(client, fixture, LEAD, "lead", "ft", "include").status_code == 201

    still_refused = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:resolve",
        json={"disposition": "include", "rationale": "ruling"},
        headers=headers(),
    )
    assert still_refused.status_code == 409, still_refused.text
    assert "separation of duty" in still_refused.text.lower() or "recorded a decision" in still_refused.text.lower(), (
        "the second refusal is not the separation-of-duty one, so this test is measuring something else: "
        + still_refused.text
    )


def test_consensus_resolves_it_without_any_lead(client, lead_is_also_a_reviewer) -> None:
    """EXIT ONE, and the one that needs nobody. The reviewers come to agree.

    Without an exit the gate above would be a deadlock for any review whose only
    lead is also screening — `create_assignments` gives every named reviewer every
    record, so a lead named as a reviewer is assigned to all of them.
    """
    fixture = lead_is_also_a_reviewer
    _conflicted_at_full_text(client, fixture)

    third = _decide(client, fixture, "r-three", "reviewer", "ft", "exclude")
    assert third.status_code == 409, "expected the already-decided conflict; the fixture changed: " + third.text
    assert "current_decision_id" in third.json(), (
        "this is a 409, but not the already-decided one, so the id read from it below would be "
        "somebody else's or absent: " + third.text
    )

    # r-three supersedes their exclude with an include, so the two agree.
    #
    # IDENTIFIED BY THE ID THE REFUSAL HANDED BACK, not by "the full-text row in
    # my list". `GET /decisions` does not serve `reviewer_subject_id` to a
    # non-lead — deliberately, it is the attribution blinding withholds — so
    # there is nothing in the payload to pick r-three's row out by, and the old
    # `[d for d in own if d["stage_id"] == ft]` was picking the FIRST full-text
    # row in decided-at order. That is OTHER's `include`, not r-three's
    # `exclude`, and the supersede then failed 409 "the decision being superseded
    # is not your current decision".
    #
    # It read correctly only while L1 was narrowing the list to r-three's own
    # rows underneath it, which is an RLS property this test never asserted and
    # does not hold on every connection: `DATABASE_URL` here names the OWNER,
    # which is `rolsuper`/`rolbypassrls`, so the policy does not run and all
    # three subjects' decisions come back. The test was measuring the database
    # role it happened to connect as. `current_decision_id` comes from the 409
    # above, is r-three's own by construction, and needs no such assumption.
    current_decision_id = third.json()["current_decision_id"]
    own = client.get(
        f"/api/reviews/{fixture['review_id']}/decisions",
        headers=headers("r-three", "reviewer"),
    ).json()["items"]
    mine = [d for d in own if d["decision_id"] == current_decision_id]
    assert mine, f"r-three cannot see their own full-text decision: {own}"
    assert mine[0]["stage_id"] == fixture["stages"]["ft"], mine

    corrected = client.post(
        f"/api/reviews/{fixture['review_id']}/decisions",
        json={
            "stage_id": fixture["stages"]["ft"],
            "round_number": 1,
            "review_record_id": fixture["record_id"],
            "disposition": "include",
            "rationale": "on reflection",
            "supersedes_decision_id": mine[0]["decision_id"],
        },
        headers=headers("r-three", "reviewer"),
    )
    assert corrected.status_code == 201, corrected.text

    # Asserted on the EXPORT gate, not on `/conflicts`. `_unresolved_conflicts`
    # is where the consensus arm lives; the panel's own count learns about it in
    # a later change, and asserting there here would be measuring that change
    # rather than this exit.
    # READ BY WITNESS, not by LEAD, and for the reason WITNESS exists at all.
    # LEAD is assigned at full text and has decided nothing there while OTHER
    # and r-three both have, so `flow.require_no_owed_decisions` now refuses
    # LEAD the ledger — the read-side twin of the `/conflicts` withholding this
    # constant was introduced for. WITNESS screens nothing and reads it.
    counts = client.get(f"/api/reviews/{fixture['review_id']}/flow", headers=headers(WITNESS)).json()["counts"]
    assert counts["not_yet_screened"] == 0, (
        "the reviewers agree and the ledger still calls the record unscreened: " + json.dumps(counts)
    )
    # SEALED BY WITNESS, for the same reason the ledger above is READ by WITNESS.
    # `flow.create_export` holds the identical owed-guard now — it derives the
    # ledger through `_compute_flow` and used to do so with `require_review_lead`
    # as its only gate, which is the leak
    # `tests/security/test_the_ledger_is_blinded_from_a_reader_who_still_owes.py`
    # closes. LEAD owes a full-text decision that OTHER and r-three have both
    # made, so LEAD is blinded here whatever the conflict says.
    exported = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers(WITNESS))
    assert exported.status_code == 201, (
        "the reviewers agree and the export still refuses, so a screening lead has no exit at all: " + exported.text
    )

    # AND THE REFUSAL LEAD GETS IS THE BLINDING ONE, NOT THE CONFLICT ONE.
    #
    # Without this the line above could pass on a review whose disagreement was
    # never resolved at all — WITNESS would simply be reading a different gate.
    # `403` says "you owe a decision"; a `422` would say "the conflict is still
    # open", which is the thing this test exists to prove consensus closed.
    lead_sealing = client.post(f"/api/reviews/{fixture['review_id']}/exports", headers=headers())
    assert lead_sealing.status_code == 403, (
        "a screening lead who owes a decision must be refused by the blinding guard, and a 422 here "
        "would mean the consensus never resolved the conflict: " + lead_sealing.text
    )
    assert "conflicts are unresolved" not in lead_sealing.text, (
        "the conflict the reviewers resolved by agreeing is still open: " + lead_sealing.text
    )


def test_a_lead_who_is_not_screening_the_record_can_adjudicate_it(client) -> None:
    """EXIT TWO, and the control that stops this being "refuse a lead always".

    The gate is a caller property: a lead with no assignment on the record owes
    nothing, sees everything, and rules. That is the ordinary adjudication path
    and it must be untouched.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "stage-blinding", "external_key": f"sb3-{uuid.uuid4()}", "title": "Bystander rules"},
        headers=headers(),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
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
        content=json.dumps({"records": [{"title": "A study", "year": 2021, "doi": f"10.1/{uuid.uuid4()}"}]}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": ["rv-one", "rv-two"]},
        headers=headers(),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers("rv-one", "reviewer"),
    ).json()["items"][0]["review_record_id"]
    for who, disposition in (("rv-one", "include"), ("rv-two", "exclude")):
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "round_number": 1,
                "review_record_id": record_id,
                "disposition": disposition,
                "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
                "rationale": "screened",
            },
            headers=headers(who, "reviewer"),
        )

    conflict_id = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers()).json()["items"][0]["conflict_id"]
    client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [LEAD]},
        headers=headers(),
    )
    resolved = client.post(
        f"/api/reviews/{review_id}/conflicts/{conflict_id}:resolve",
        json={"disposition": "include", "rationale": "the design qualifies"},
        headers=headers(),
    )
    assert resolved.status_code in (200, 201), (
        "a lead who is screening nothing cannot adjudicate, so no disagreement in this service can ever "
        "be resolved by a lead: " + resolved.text
    )


def test_the_ruling_is_redacted_at_a_position_the_caller_owes(client, lead_is_also_a_reviewer) -> None:
    """The window beside the shut door.

    `_adjudication_summary` carries `disposition`, `exclusion_reason_code`,
    `rationale` and `adjudicator_subject_id` — the settled outcome in full — and
    it was rendered whenever an adjudication existed, two lines below the
    redaction that exists to stop exactly this. Measured before the fix, for a
    caller who owes their own full-text verdict:

        "dispositions": "redacted",
        "adjudication": {"disposition": "exclude", "rationale": "...", ...}

    Here the adjudicating lead is a DIFFERENT subject who is screening nothing,
    so the ruling exists and is legitimate; the only question is whether the
    caller who still owes a verdict may be told its outcome.

    ## Why the unblinding now names the LEAD too

    It used to name the bystander alone, and the original measurement above was
    taken with the LEAD outside the audience. `/conflicts` no longer serves a
    conflict AT ALL to a caller who owes its position and was not named in an
    unblinding — the row and every counter are withheld — so that setup now
    exercises the row's absence rather than this field, and `_conflicted_at_full
    _text` already asserts the absence for all six of its callers.

    Naming the LEAD puts the case back where this test wants it, and makes it
    the HARDER of the two: the caller has an audited authorisation to read who
    said what in this conflict, and the ruling is withheld anyway. The gate is
    `owes_this_conflict` alone and deliberately not `unblinded` — a lead's
    ruling is not a reviewer attribution, so unblinding does not govern it — and
    with the LEAD outside the audience that distinction could not be seen.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)

    bystander = "bystander-lead"
    unblinded = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": [bystander, LEAD]},
        headers=headers(bystander, "lead"),
    )
    assert unblinded.status_code in (200, 201), unblinded.text
    ruled = client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:resolve",
        json={"disposition": "exclude", "exclusion_reason_code": "wrong_design", "rationale": "B was right"},
        headers=headers(bystander, "lead"),
    )
    assert ruled.status_code in (200, 201), ruled.text

    row = next(
        c
        for c in client.get(f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers()).json()["items"]
        if c["conflict_id"] == conflict_id
    )
    assert row["dispositions"] == "redacted", f"the fixture is not in the owed state: {row}"
    assert row["adjudication"] == "redacted", (
        "the dispositions were withheld and the ruling that settled them was served in full — "
        f"disposition, reason code and rationale: {json.dumps(row['adjudication'])}"
    )


def test_the_ruling_is_readable_by_a_lead_who_owes_nothing(client, lead_is_also_a_reviewer) -> None:
    """NEGATIVE CONTROL. `None` and `redacted` must stay distinguishable.

    A lead who is screening nothing needs the ruling for the audit trail. And
    `None` already means "no lead has ruled" — collapsing the two would tell an
    owing reviewer that a disagreement is unsettled when it is settled against
    them, which is a different lie rather than a fix.
    """
    fixture = lead_is_also_a_reviewer
    conflict_id = _conflicted_at_full_text(client, fixture)

    before = next(
        c
        for c in client.get(
            f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers("bystander-lead", "lead")
        ).json()["items"]
        if c["conflict_id"] == conflict_id
    )
    assert before["adjudication"] is None, f"nobody has ruled yet, so this must be None: {before}"

    client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating", "disclose_to": ["bystander-lead"]},
        headers=headers("bystander-lead", "lead"),
    )
    client.post(
        f"/api/reviews/{fixture['review_id']}/conflicts/{conflict_id}:resolve",
        json={"disposition": "exclude", "exclusion_reason_code": "wrong_design", "rationale": "B was right"},
        headers=headers("bystander-lead", "lead"),
    )

    after = next(
        c
        for c in client.get(
            f"/api/reviews/{fixture['review_id']}/conflicts", headers=headers("bystander-lead", "lead")
        ).json()["items"]
        if c["conflict_id"] == conflict_id
    )
    assert isinstance(after["adjudication"], dict), (
        "a lead who owes nothing cannot read the ruling, so the audit trail is gone: " + json.dumps(after)
    )
    assert after["adjudication"]["rationale"] == "B was right", after["adjudication"]
