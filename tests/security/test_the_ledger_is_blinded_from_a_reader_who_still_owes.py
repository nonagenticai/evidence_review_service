"""The owed-guard covers the LEDGER, not only `GET /decisions` — `ER-004.2`.

`tests/security/test_blinding_layers.py::
test_a_lead_who_still_owes_a_decision_cannot_read_their_partners` pins the rule
on one route. It had no counterpart here, and that gap is the whole defect: the
rule was written into `blinded_reads.visible_decisions`, which `/decisions`
calls and `/flow` does not.

Measured on `ef028ab`, `ENABLE_AUTH=false`, `reviewers_required=1`, one record,
`C` creating the review and assigning `[C, B]`, `B` excluding, `C` having
decided nothing:

    GET /reviews/{id}/decisions?review_record_id={r0}  as C
        -> {"items": [], "count": 0, "total": 0}                    # correct
    GET /reviews/{id}/flow                             as C
        -> {"records_total": 1, "excluded": 1,
            "exclusion_reasons": {"wrong_design": 1},
            "counts_by_stage": [{"settled_exclude": 1, ...}]}       # LEAK

`C` learns `B`'s disposition AND `B`'s exclusion reason on the exact record `C`
is about to judge, with no unblinding recorded, no audience and no audit row.

`routers/flow.py` names this leak in its own docstring and asserts "lead only"
is the mitigation. It is not: `require_review_lead` grants on
`reviews.created_by_subject_id` as well as on the realm role, and nothing stops
the creator of a review from also screening it. Every ledger route is reachable
by that grant, so all of them are gated here.

## ⚠️ "ALL THREE" WAS FOUR, AND THE FOURTH SEALED THE LEAK

This file shipped gating `GET /flow`, `flow:rebuild` and `flow:audit`, and its
own test named `test_every_ledger_route_is_closed_not_only_the_read` covered the
first two of those. `POST /exports` also derives the ledger — `create_export`
calls `_compute_flow` DIRECTLY — and was gated by `require_review_lead` alone,
which is the creator grant that made `GET /flow` reachable in the first place.
Measured on `5442ad5`, in this file's own fixture:

    GET  /api/reviews/{id}/flow           as C -> 403   (this file closed it)
    POST /api/reviews/{id}/exports        as C -> 201
    GET  /api/reviews/{id}/exports/{eid}  as C -> 200
        payload.manifest.counts = {"records_total": 1, "excluded": 1,
                                   "exclusion_reasons": {"wrong_design": 1}}

Byte for byte the disclosure the `403` above refuses, at denominator 1 — and
WORSE, because `inclusion_exports` is append-only and hash-sealed, so the counts
stay readable through `GET /exports` forever, including after `C` has decided
and the owed-guard would no longer refuse anything.

The read routes are gated for the same reason and not only the write: a seal
taken legitimately by a lead who owed nothing is the same disclosure to a caller
who owes one, and it exists BEFORE that caller ever calls anything. So the
enumeration test below is a property of the DERIVATION, not of a route list
somebody remembered to update — see
`test_the_derivation_has_no_caller_this_file_has_not_enumerated`.

## Why a REFUSAL and not a redacted ledger

Because a conserved aggregate cannot be partly suppressed. `derive`'s buckets
sum to `records_total`, so blanking `excluded` hands the same fact back by
subtraction, and at the denominator this fixture uses — one record — the total
alone names the disposition. The other option, deriving from only the rows the
caller may see, produces a ledger whose numbers no history supports, and
`create_export` seals that same derivation into an append-only artifact.

So the assertions below are written as the DISCLOSURE property first — a body
that reveals nothing fails nothing, whichever way a future fix chooses to
achieve it — and the fail-closed status code is pinned separately, as the
mechanism actually shipped.
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

pytestmark = [
    pytest.mark.blinding,
    pytest.mark.security,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

TENANT = tenant("tenant-ledger-owed")


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


def assert_ledger_discloses_nothing(response, message: str) -> None:
    """The ledger told this caller NOTHING about anybody else's work.

    Deliberately not `status_code == 403`. The property is the disclosure, and a
    body is the only place a disclosure can happen — so a 200 is admitted here
    and then held to reporting no other reviewer's disposition, which is what
    makes this gate survive a different-but-correct fix. The status code is
    pinned separately by
    :func:`test_the_refusal_is_fail_closed_and_names_the_remedy`.
    """
    if response.status_code == 403:
        body = response.json()
        assert "excluded" not in json.dumps(body), f"{message} — the refusal body carries counts: {body!r}"
        return
    assert response.status_code == 200, f"{message} — unexpected status {response.status_code}: {response.text}"
    body = response.json()
    counts = body.get("counts", {})
    assert counts.get("excluded", 0) == 0, (
        f"{message} — the ledger reports {counts.get('excluded')!r} excluded, which is the other "
        f"reviewer's disposition on the record this caller still owes: {counts!r}"
    )
    assert not counts.get("exclusion_reasons"), (
        f"{message} — the ledger reports the other reviewer's exclusion REASON: {counts!r}"
    )
    for stage in body.get("counts_by_stage", []):
        assert stage.get("settled_exclude", 0) == 0, f"{message} — per-stage: {stage!r}"
        assert stage.get("settled_include", 0) == 0, f"{message} — per-stage: {stage!r}"


def assert_export_discloses_nothing(response, message: str) -> None:
    """The sealed inclusion set told this caller NOTHING about anybody's work.

    Same shape as :func:`assert_ledger_discloses_nothing` and for the same
    reason — the property is the DISCLOSURE, so a fix that redacts rather than
    refuses passes here — but the export has three surfaces the ledger body does
    not, and every one of them is the same fact spelled differently:

    * `payload.manifest.counts`, which is `GET /flow`'s `counts` verbatim;
    * `omissions`, which names each record's terminal bucket AND its exclusion
      reason, so it is worse than the counts rather than a redaction of them;
    * `included_count` / `omissions_count` on the SUMMARY, served whether or not
      `with_payload` was asked for — at one record, `omissions_count: 1` is the
      partner's verdict as a scalar.

    A list body is unwrapped to its items, because the EXISTENCE of a seal is
    itself progress (`routers/exports.py`), so `count` is checked too.

    ⛔ NOT usable on `:verify`. That body is hashes and booleans — it carries no
    counts, so every assertion here would pass on it while it confirmed the seal
    exists and reproduces, which at one record is the same one bit. `:verify` is
    asserted as a status code, deliberately and separately.
    """
    if response.status_code == 403:
        body = response.json()
        blob = json.dumps(body)
        assert "excluded" not in blob and "wrong_design" not in blob, (
            f"{message} — the refusal body carries the counts it refuses: {body!r}"
        )
        return
    assert response.status_code in (200, 201), (
        f"{message} — unexpected status {response.status_code}: {response.text[:400]}"
    )
    body = response.json()
    is_list = "items" in body
    summaries = body["items"] if is_list else [body]
    if is_list:
        assert body.get("count", 0) == len(summaries), f"{message} — `count` disagrees with `items`: {body!r}"
    else:
        assert "omissions_count" in body, (
            f"{message} — this body carries none of the fields this assertion checks, so it would "
            f"pass whatever the route disclosed: {body!r}"
        )
    for summary in summaries:
        assert summary.get("omissions_count", 0) == 0, (
            f"{message} — `omissions_count` is {summary.get('omissions_count')!r}, which at this "
            f"denominator is the other reviewer's verdict as a scalar: {summary!r}"
        )
        assert summary.get("included_count", 0) == 0, f"{message} — `included_count`: {summary!r}"
        assert not summary.get("omissions"), (
            f"{message} — the omissions array names each record's bucket and exclusion reason: "
            f"{summary.get('omissions')!r}"
        )
        counts = summary.get("payload", {}).get("manifest", {}).get("counts", {})
        assert counts.get("excluded", 0) == 0, f"{message} — sealed counts: {counts!r}"
        assert not counts.get("exclusion_reasons"), f"{message} — sealed exclusion reasons: {counts!r}"


def _review_where_the_creator_also_screens(client, slug: str, *, reviewers_required: int = 1) -> dict:
    """`C` creates the review, assigns `[C, B]`, `B` decides, `C` decides nothing.

    `C` sends `X-Actor-Role: reviewer` throughout, deliberately: the point is
    that no realm `lead` role is needed to reach a lead-only route, because
    `require_review_lead` grants on `created_by_subject_id`.
    """
    creator, other = f"{slug}-c", f"{slug}-b"
    review_id = client.post(
        "/api/reviews",
        json={"project_id": slug, "external_key": f"{slug}-{uuid.uuid4()}", "title": "The creator screens too"},
        headers=headers(creator),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": reviewers_required,
        },
        headers=headers(creator),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(creator),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(creator),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Contested paper", "year": 2021}]}),
        headers={**headers(creator), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(creator))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(creator)).json()["items"][0]["stage_id"]
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [other, creator]},
        headers=headers(creator),
    )
    assert assigned.status_code in (200, 201), assigned.text
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(other),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(other),
    )
    assert decided.status_code == 201, decided.text
    return {
        "review_id": review_id,
        "stage_id": stage_id,
        "record_id": record_id,
        "creator": creator,
        "other": other,
    }


def test_a_reader_who_still_owes_a_decision_cannot_read_the_flow_ledger(client):
    """THE GATE, and the control beside it.

    `GET /decisions` already refuses this caller. The two routes derive from the
    same table and must agree about what this caller may learn from it — so the
    control is asserted in the same test, and a fix that closes the ledger by
    breaking `/decisions` fails here rather than passing.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed")

    control = client.get(
        f"/api/reviews/{fx['review_id']}/decisions",
        params={"review_record_id": fx["record_id"]},
        headers=headers(fx["creator"]),
    )
    assert control.status_code == 200, control.text
    assert control.json()["total"] == 0, (
        f"the CONTROL moved: `/decisions` disclosed the partner's row to a caller who owes one, so "
        f"this test is no longer measuring the gap between the two routes: {control.json()!r}"
    )

    ledger = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers(fx["creator"]))
    assert_ledger_discloses_nothing(
        ledger,
        "a reviewer who has decided NOTHING read the settled disposition of the record they owe, "
        "off the flow ledger, because they happened to create the review",
    )


def test_every_ledger_route_is_closed_not_only_the_read(client):
    """`flow:rebuild`, `flow:audit` AND `POST /exports` are the same leak.

    ⚠️ This test used to carry this name while covering the first two only. The
    third is the one that mattered: `create_export` calls `_compute_flow`
    directly, so it never inherited `compute_flow`'s gate, and it SEALS what it
    derives into an append-only row. A test whose name is broader than its
    coverage is how that survived a fix aimed at exactly this property.

    `flow:rebuild` is worse than the read: it PERSISTS the projection it returns,
    so gating only `GET /flow` would leave a write path that hands back the
    identical body. `POST /exports` is worse again: its 201 body carries
    `included_count` and `omissions_count`, which at this fixture's denominator
    of one ARE the partner's verdict, and the row it writes cannot be deleted.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed-verbs")

    rebuilt = client.post(f"/api/reviews/{fx['review_id']}/flow:rebuild", headers=headers(fx["creator"]))
    assert_ledger_discloses_nothing(rebuilt, "`flow:rebuild` handed the ledger to a caller who owes a decision")

    audited = client.post(f"/api/reviews/{fx['review_id']}/flow:audit", headers=headers(fx["creator"]))
    assert audited.status_code == 403, (
        f"`flow:audit` derives the live ledger and holds it against every seal, so it discloses "
        f"the same counts: {audited.status_code} {audited.text[:400]}"
    )

    sealed = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers(fx["creator"]))
    assert_export_discloses_nothing(
        sealed,
        "`POST /exports` derived and SEALED the ledger for a caller who owes a decision on the one "
        "record in it — the same counts `GET /flow` refuses, in an append-only row",
    )


def test_a_seal_taken_before_the_reader_owed_is_not_a_second_door(client):
    """The pre-existing seal, which gating only the WRITE leaves wide open.

    A lead who owes nothing may seal, and should — that is the product. The row
    they leave behind carries `manifest.counts` verbatim plus an `omissions`
    array naming every record's terminal bucket and exclusion reason, and
    `GET /exports/{id}` defaults to `with_payload=True`. So a caller who still
    owes a decision reads the whole ledger off a row they did not create and
    could not have been refused at creation time.

    That is why the READ routes are gated too, and why gating them is not
    retroactive punishment of old seals: the guard is a property of the CALLER,
    not of the row. The moment this caller records the verdict they owe, every
    route here opens — pinned by
    :func:`test_once_the_reader_has_decided_the_sealed_export_returns`.

    `list_exports` is included because existence is disclosure in its own right
    (`routers/exports.py` says so), and `included_count` / `omissions_count` are
    on the summary whether or not the payload is asked for. `:verify` is
    included because it names the export it verified and confirms it verifies —
    which, on a review with one record, is the same one bit.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed-seal")
    bystander = "ledger-owed-seal-lead"

    sealed = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers(bystander, "lead"))
    assert sealed.status_code == 201, (
        f"the CONTROL moved: a lead who owes nothing could not seal, so this test is not measuring "
        f"a pre-existing seal at all: {sealed.status_code} {sealed.text[:400]}"
    )
    export_id = sealed.json()["export_id"]
    assert sealed.json()["omissions_count"] == 1, (
        f"the seal under test does not carry the partner's verdict, so reading it would disclose "
        f"nothing and the assertions below would pass vacuously: {sealed.json()!r}"
    )

    for path, what in (
        (f"/api/reviews/{fx['review_id']}/exports", "`GET /exports`"),
        (f"/api/reviews/{fx['review_id']}/exports/{export_id}", "`GET /exports/{id}`"),
        (f"/api/reviews/{fx['review_id']}/exports/{export_id}?with_payload=false", "`GET /exports/{id}` summary"),
    ):
        assert_export_discloses_nothing(
            client.get(path, headers=headers(fx["creator"])),
            f"{what} served a seal taken by somebody else to a caller who still owes a decision on "
            "the record it reports",
        )

    # `:verify` as a STATUS, for the reason `assert_export_discloses_nothing`
    # refuses to be pointed at it: its body is hashes and booleans, so the
    # disclosure property is unassertable there. What it discloses is that the
    # seal EXISTS and reproduces — and an export can only be taken once every
    # record is screened, so on this review that is the partner's verdict.
    verified = client.get(f"/api/reviews/{fx['review_id']}/exports/{export_id}:verify", headers=headers(fx["creator"]))
    assert verified.status_code == 403, (
        f"`GET /exports/{{id}}:verify` confirmed to a caller who still owes a decision that a seal "
        f"exists and reproduces, which is only possible once their partner has finished: "
        f"{verified.status_code} {verified.text[:400]}"
    )


def test_once_the_reader_has_decided_the_sealed_export_returns(client):
    """The other half for the export routes: the guard lifts by WORKING.

    Without this a hard-coded 403 on `/exports` satisfies every assertion above,
    and the fix would be "nobody reads the inclusion set" rather than "the
    reader who owes a verdict does not read it first".
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed-seal-released")
    bystander = "ledger-owed-seal-released-lead"

    sealed = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers(bystander, "lead"))
    assert sealed.status_code == 201, sealed.text
    export_id = sealed.json()["export_id"]

    decided = client.post(
        f"/api/reviews/{fx['review_id']}/decisions",
        json={
            "stage_id": fx["stage_id"],
            "review_record_id": fx["record_id"],
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(fx["creator"]),
    )
    assert decided.status_code == 201, decided.text

    listed = client.get(f"/api/reviews/{fx['review_id']}/exports", headers=headers(fx["creator"]))
    assert listed.status_code == 200, f"the creator cannot read the review's own inclusion set: {listed.text[:400]}"
    assert listed.json()["count"] == 1, listed.json()

    detail = client.get(f"/api/reviews/{fx['review_id']}/exports/{export_id}", headers=headers(fx["creator"]))
    assert detail.status_code == 200, detail.text
    assert detail.json()["payload"]["manifest"]["counts"]["records_total"] == 1, detail.json()["payload"]["manifest"]

    verified = client.get(f"/api/reviews/{fx['review_id']}/exports/{export_id}:verify", headers=headers(fx["creator"]))
    assert verified.status_code == 200, verified.text

    resealed = client.post(f"/api/reviews/{fx['review_id']}/exports", headers=headers(fx["creator"]))
    assert resealed.status_code == 201, resealed.text
    assert resealed.json()["replayed"] is True, (
        f"the same inclusion set was sealed a second time rather than replayed: {resealed.json()!r}"
    )


def test_the_derivation_has_no_caller_this_file_has_not_enumerated(client):
    """The routes above are ENUMERATED FROM THE CODE, not from memory.

    `_compute_flow` is the derivation every count in this service comes out of.
    A new caller of it is a new door onto the same room, and the way this leak
    survived was a test whose NAME claimed every route while its body knew about
    two. So the caller set is asserted rather than described: adding a third
    caller fails here, and the failure says to gate it and to add it above.

    Parsed, not grepped — a regex over the source matches this docstring's own
    mention of `_compute_flow` and reports the test that is doing the right
    thing. `flow_audit.audit_flow` is deliberately NOT in this set: it derives
    through `_collect` instead, which is why its gate is written in
    `routers/flow.py` and why it is exercised separately above.
    """
    import ast

    tree = ast.parse((SRC / "application" / "services" / "flow.py").read_text())
    callers: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_compute_flow":
                callers.add(function.name)

    assert callers == {"compute_flow", "create_export"}, (
        f"the set of functions deriving the flow through `_compute_flow` is {sorted(callers)}, not "
        "['compute_flow', 'create_export']. Every one of them is a route onto another reviewer's "
        "dispositions: gate the new one with `require_no_owed_decisions` and add its route to "
        "`test_every_ledger_route_is_closed_not_only_the_read`."
    )


def test_the_refusal_is_fail_closed_and_names_the_remedy(client):
    """The mechanism actually shipped, pinned separately from the property.

    A refusal a caller cannot act on is a bug report addressed to the wrong
    person: the remedy here is entirely in the caller's hands — record the
    verdict they owe — so the body has to say so.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed-body")

    ledger = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers(fx["creator"]))
    assert ledger.status_code == 403, f"the ledger was not refused: {ledger.status_code} {ledger.text[:400]}"
    body = ledger.json()
    assert body.get("type", "").endswith("/forbidden"), body
    assert "own" in body.get("detail", "") or "owe" in body.get("detail", ""), (
        f"the refusal does not tell the caller that recording their own decision lifts it: {body!r}"
    )


def test_once_the_reader_has_decided_the_ledger_returns(client):
    """The other half, and it is what stops the fix being "nobody reads the ledger".

    Without this, a hard-coded 403 satisfies every gate above. The rule is about
    what the caller still OWES, so the moment their own verdict is recorded the
    blinding reason is gone and the ledger — which they need, to run the review
    they created — comes back.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-owed-released")

    before = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers(fx["creator"]))
    assert_ledger_discloses_nothing(before, "the caller still owed a decision and read the ledger anyway")

    decided = client.post(
        f"/api/reviews/{fx['review_id']}/decisions",
        json={
            "stage_id": fx["stage_id"],
            "review_record_id": fx["record_id"],
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(fx["creator"]),
    )
    assert decided.status_code == 201, decided.text

    after = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers(fx["creator"]))
    assert after.status_code == 200, f"the creator cannot run the review they created: {after.text[:400]}"
    assert after.json()["counts"]["records_total"] == 1, after.json()["counts"]

    rebuilt = client.post(f"/api/reviews/{fx['review_id']}/flow:rebuild", headers=headers(fx["creator"]))
    assert rebuilt.status_code == 200, rebuilt.text
    audited = client.post(f"/api/reviews/{fx['review_id']}/flow:audit", headers=headers(fx["creator"]))
    assert audited.status_code == 200, audited.text


def test_a_lead_who_is_not_screening_keeps_the_ledger(client):
    """Scoping. The rule is a CALLER property, not a role property.

    A lead holding no assignment owes nothing and reads the ledger exactly as
    they always could — which is the whole point of `require_review_lead`
    granting to a creator in the first place, and what a role-shaped fix
    ("reviewers never read the ledger") would have taken away.
    """
    fx = _review_where_the_creator_also_screens(client, "ledger-unowed")
    bystander = "ledger-unowed-lead"

    ledger = client.get(f"/api/reviews/{fx['review_id']}/flow", headers=headers(bystander, "lead"))
    assert ledger.status_code == 200, f"a lead with no assignment lost the ledger: {ledger.text[:400]}"
    counts = ledger.json()["counts"]
    assert counts["records_total"] == 1, counts
    assert counts["excluded"] == 1, (
        f"a lead who owes nothing must still see the review's real numbers, or the fix has "
        f"broken the ledger rather than blinded it: {counts!r}"
    )
