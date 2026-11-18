"""`GET /conflicts` moved a counter as the other reviewers worked — `ER-004.2`.

`visible_decisions` and the two field-level gates in `list_conflicts` withhold
`dispositions` and `adjudication` at a position the caller still owes a verdict
on. That was necessary and it was not sufficient, in two ways that no amount of
further redaction can reach.

**The row is the disclosure.** It carries `review_record_id`, `title`,
`stage_id`, `round_number`, `opened_at` and `resolved`, and a conflict exists
only when the current decisions disagree. So the row says *this record is
disputed*, and at `reviewers_required = 2` a lead assigned to that record reads
the other reviewer's disposition straight off it: the disagreement is with them,
so the other vote is the negation of the one they are about to cast.

**The count is the disclosure a second time, and needs no row at all.** `total`,
`total_open` and `open` moved `0 -> 1` the instant the second reviewer disagreed.
A lead-who-screens polling this route watches their partner's verdict arrive as
an integer — the same aggregate leak `list_conflicts`' own docstring refuses for
a plain reviewer ("a reviewer could sit on this endpoint and watch their partner
work"), reached by a caller holding the role the endpoint is gated on. Redacting
every remaining field cannot close it, because the number is not in a field.

So the conflict is dropped from `items` AND from every counter while the caller
owes a decision at its position, and `owed_hidden` reports the cardinality of
what was withheld.

## What each test here is for

`test_the_counter_does_not_move_when_the_partner_disagrees` is the leak itself,
and it is the one that fails against the pre-fix code. The rest exist because a
filter that hides too much would pass it just as well:

* `test_a_lead_who_owes_nothing_sees_every_conflict` is the negative control, and
  it is what proves the conflicts in these fixtures are REAL. Without it, a
  `list_conflicts` that returned `{"items": [], "total_open": 0}` unconditionally
  — or a fixture that opened no conflict at all — would satisfy every assertion
  about the owing lead.
* `test_the_filter_is_per_position_not_a_blanket` holds one disputed record the
  lead owes and one they have decided, in ONE response. A blanket "hide
  everything from a lead who is screening anything" is the obvious wrong fix and
  is invisible from a single-record fixture.
* `test_discharging_the_debt_reinstates_the_conflict` is the other direction:
  withheld is not deleted, and the lead gets the disagreement the moment they
  have reported.

## On `owed_hidden`, and the residual it accepts

`owed_hidden` moves `0 -> 1` in these fixtures, and that is asserted rather than
glossed. It is a cardinality and never an identity — no record, no stage, no
title, no timestamp — but a caller who owes exactly ONE position and reads
`owed_hidden: 1` has located the disputed record, because there is only one
candidate. That residual shrinks as the caller's outstanding queue grows, it is
strictly less than the field-level rule disclosed (which named the record
whatever the caller owed), and the alternative is a panel whose counters cannot
be reconciled against a `POST /exports` that refuses on conflicts the lead is not
being shown. See `list_conflicts` for the full argument.
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

TENANT = tenant("tenant-owed-conflict-panel")

#: The lead who is ALSO an assigned reviewer. `create_assignments` gives every
#: named reviewer every record, so naming them here assigns them to all of them.
SCREENING_LEAD = "owed-panel-screening-lead"
#: A lead of the same review who is screening nothing. The negative control, and
#: the oracle: what they see is what the review actually holds.
BYSTANDER_LEAD = "owed-panel-bystander-lead"
FIRST = "owed-panel-r1"
SECOND = "owed-panel-r2"


def headers(subject: str = SCREENING_LEAD, role: str = "lead") -> dict[str, str]:
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


def _review(client, records: int):  # noqa: ANN001, ANN202
    """An activated review, `records` sealed records, three assigned reviewers.

    The screening lead is one of the three. `reviewers_required` is 2, so FIRST
    and SECOND alone are enough to open a conflict — the lead's own verdict is
    outstanding while the disagreement already exists, which is exactly the state
    the leak lives in.
    """
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "owed-panel", "external_key": f"op-{uuid.uuid4()}", "title": "Owed panel"},
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
    assert (
        client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol}:lock", headers=headers()).status_code == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol, "rationale": "go"},
            headers=headers(),
        ).status_code
        == 200
    )
    marker = uuid.uuid4().hex[:8]
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Study {marker}-{i}", "doi": f"10.1000/{marker}-{i}", "year": 2021}
                    for i in range(records)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code == 201, imported.text
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code == 201
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "round_number": 1, "reviewer_subject_ids": [SCREENING_LEAD, FIRST, SECOND]},
        headers=headers(),
    )
    assert assigned.status_code == 201, assigned.text

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(SCREENING_LEAD, "lead"),
    ).json()
    assert len(queue["items"]) == records, queue
    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "record_ids": [i["review_record_id"] for i in queue["items"]],
    }


def _decide(client, fx, who: str, role: str, record_id: str, disposition: str):  # noqa: ANN001, ANN202
    got = client.post(
        f"/api/reviews/{fx['review_id']}/decisions",
        json={
            "stage_id": fx["stage_id"],
            "round_number": 1,
            "review_record_id": record_id,
            "disposition": disposition,
            "exclusion_reason_code": "wrong_design" if disposition == "exclude" else None,
            "rationale": "screened",
        },
        headers=headers(who, role),
    )
    assert got.status_code == 201, got.text
    return got


def _dispute(client, fx, record_id: str) -> None:  # noqa: ANN001
    """FIRST and SECOND disagree. The screening lead has still reported nothing."""
    _decide(client, fx, FIRST, "reviewer", record_id, "include")
    _decide(client, fx, SECOND, "reviewer", record_id, "exclude")


def _panel(client, fx, who: str) -> dict:  # noqa: ANN001
    got = client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers(who, "lead"))
    assert got.status_code == 200, got.text
    return got.json()


def test_a_lead_who_owes_nothing_sees_every_conflict(client) -> None:
    """THE ORACLE, and the non-vacuity floor for every other test in this file.

    Everything else here asserts that a caller sees LESS, and every one of those
    assertions is satisfied by a fixture that opened no conflict at all, or by a
    `list_conflicts` that returns an empty page to everybody. This is the test
    that says the disagreements are real and readable — by a lead of the same
    review, in the same tenant, at the same moment, differing only in whether
    they owe a verdict.
    """
    fx = _review(client, records=2)
    for record_id in fx["record_ids"]:
        _dispute(client, fx, record_id)

    panel = _panel(client, fx, BYSTANDER_LEAD)
    assert panel["total"] == 2, panel
    assert panel["total_open"] == 2, panel
    assert panel["count"] == 2, panel
    assert panel["owed_hidden"] == 0, (
        "a lead who is screening nothing owes nothing, so nothing may be withheld from them — "
        f"and `owed_hidden` must be 0 rather than absent for the ordinary caller: {panel}"
    )
    assert {i["review_record_id"] for i in panel["items"]} == set(fx["record_ids"]), panel


def test_the_counter_does_not_move_when_the_partner_disagrees(client) -> None:
    """THE LEAK. `total_open` was a live readout of the other reviewers' verdicts.

    Read twice, either side of the second reviewer's decision, by a lead who is
    assigned to the record and has decided nothing. Before the fix the pair reads
    `0` then `1`, and that single integer is the disposition: the disagreement is
    with the reader, so the vote they have not yet cast is the negation of it.

    The bystander read at the end is what makes the `0` a measurement rather than
    an empty database.
    """
    fx = _review(client, records=1)
    record_id = fx["record_ids"][0]

    _decide(client, fx, FIRST, "reviewer", record_id, "include")
    before = _panel(client, fx, SCREENING_LEAD)
    assert before["total_open"] == 0, f"no conflict exists yet, so this is a broken fixture: {before}"
    assert before["owed_hidden"] == 0, before

    _decide(client, fx, SECOND, "reviewer", record_id, "exclude")
    after = _panel(client, fx, SCREENING_LEAD)

    assert after["total_open"] == before["total_open"], (
        f"`total_open` moved {before['total_open']} -> {after['total_open']} because the other two "
        "reviewers disagreed, in a response served to a lead who is assigned to that record and has "
        "not decided it. At reviewers_required=2 that integer IS the other reviewer's disposition."
    )
    assert after["total"] == 0 and after["count"] == 0 and after["open"] == 0, (
        f"every counter on this route has to move together, and these did not: {after}"
    )
    assert after["items"] == [], f"the conflict row is served to a caller who owes the position: {after}"
    assert record_id not in json.dumps(after), (
        "the withheld record is named somewhere in the body — dropping it from `items` is not enough "
        f"if it survives in another field: {json.dumps(after)}"
    )
    assert after["owed_hidden"] == 1, (
        "the withholding is silent: the lead cannot tell 'no disagreements' from 'disagreements you "
        f"may not be told about', and cannot reconcile this panel against the export gate: {after}"
    )
    # The conflict is REAL. Without this, every assertion above is satisfied by a
    # decision that opened nothing.
    assert _panel(client, fx, BYSTANDER_LEAD)["total_open"] == 1, "the fixture opened no conflict"


def test_the_filter_is_per_position_not_a_blanket(client) -> None:
    """Two disputed records, ONE response: one owed and hidden, one decided and served.

    A "hide the whole panel from any lead who is screening" fix passes the leak
    test above and is wrong — `create_assignments` assigns every named reviewer
    every record, so a screening lead owes the whole corpus on day one and would
    never see a disagreement again. The debt is per `(stage, round, record)`, and
    this is the smallest fixture in which that is observable.
    """
    fx = _review(client, records=2)
    owed, decided = fx["record_ids"]
    for record_id in (owed, decided):
        _dispute(client, fx, record_id)
    _decide(client, fx, SCREENING_LEAD, "lead", decided, "include")

    panel = _panel(client, fx, SCREENING_LEAD)
    assert [i["review_record_id"] for i in panel["items"]] == [decided], (
        f"the panel is not filtered per position: {panel}"
    )
    assert panel["count"] == 1 and panel["total"] == 1 and panel["total_open"] == 1, panel
    assert panel["owed_hidden"] == 1, panel
    assert owed not in json.dumps(panel), f"the record the lead still owes is named in the body: {json.dumps(panel)}"
    # And the review really holds two.
    assert _panel(client, fx, BYSTANDER_LEAD)["total"] == 2


def test_discharging_the_debt_reinstates_the_conflict(client) -> None:
    """WITHHELD IS NOT DELETED. The lead gets it the moment they have reported.

    This is the half that stops the fix from being a data-loss bug: the rule is
    "not until you have voted", never "not for you". It is also what keeps the
    adjudication path reachable — `:resolve` refuses a lead who owes the position
    with `409`, so the row becoming visible and the door opening happen together.
    """
    fx = _review(client, records=1)
    record_id = fx["record_ids"][0]
    _dispute(client, fx, record_id)

    hidden = _panel(client, fx, SCREENING_LEAD)
    assert hidden["items"] == [] and hidden["owed_hidden"] == 1, hidden

    _decide(client, fx, SCREENING_LEAD, "lead", record_id, "include")

    shown = _panel(client, fx, SCREENING_LEAD)
    assert shown["owed_hidden"] == 0, f"the debt is discharged and something is still withheld: {shown}"
    assert [i["review_record_id"] for i in shown["items"]] == [record_id], shown
    assert shown["total"] == 1 and shown["total_open"] == 1 and shown["count"] == 1, shown
