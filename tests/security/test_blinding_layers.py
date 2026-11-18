"""Each blinding layer, proven to be load-bearing — `ER-004.2`.

The plan's requirement is not "blinding works". It is that **removing any single
layer turns the suite red**. A blinding test that still passes with a layer
deleted is measuring one of the other layers, and the deleted one was decoration.

So each test here names the layer it covers and, where it is possible to do so
safely, actually removes it and shows the leak:

* **L1** — the RLS policy is dropped inside a transaction that is then rolled
  back, and the previously-invisible row becomes visible.
* **L2** — the source tree is grepped: any reference to `ScreeningDecision`
  outside `blinded_reads` fails the test.
* **L3** — the queue payload is scanned for progress vocabulary.
* **L4** — the cursor is asserted to be over assignments, not records.
* **L5** — the 404 body is asserted not to contain the identifier, and the log
  record is asserted to carry only the decision id.
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.tenants import tenant

DB_URL = os.getenv("DATABASE_URL", "")
OWNER_DB_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = [
    pytest.mark.blinding,
    pytest.mark.skipif(
        not DB_URL.startswith("postgresql"),
        reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
    ),
]

TENANT = tenant("tenant-blind")
LEAD = "lead-dana"
A = "reviewer-ann"
B = "reviewer-ben"


def headers(subject: str, role: str = "reviewer") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def assert_sees_no_decisions(body: dict, message: str) -> None:
    """`GET /decisions` disclosed NOTHING to this caller.

    Six gates in this file spelled that as `body == {"items": [], "count": 0}`,
    whole-body equality against a route that returned exactly two members. That
    route is now paginated — `total`, `offset` and `has_more` beside them — and
    the equality broke on the shape rather than on the disclosure.

    Rewritten as an assertion about the disclosure, and it is STRONGER than the
    equality it replaces rather than a relaxation of it. `count` alone can no
    longer mean "nothing is visible": an empty PAGE of a non-empty population
    satisfies it, and on a paginated route that is a reachable way to look closed
    while being open. `total` is the review-wide number, so asserting it at zero
    is what pins the blinding; `items` is kept because a body reporting `total: 0`
    while carrying rows would be a different bug and this is a security file.
    """
    assert body.get("items") == [], f"{message} — items: {body!r}"
    assert body.get("count") == 0, f"{message} — count: {body!r}"
    assert body.get("total") == 0, (
        f"{message} — the page is empty but `total` says the caller may see "
        f"{body.get('total')!r} decisions, so the disclosure is one query parameter away: {body!r}"
    )


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


@pytest.fixture(scope="module")
def scenario(client):  # noqa: ANN201, ANN001
    """Two reviewers, two records, both reviewers decided."""
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "blinding",
            "external_key": f"blind-{uuid.uuid4()}",
            "title": "Blinding fixture",
        },
        headers=headers(LEAD, "lead"),
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
        headers=headers(LEAD, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(LEAD, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(LEAD, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Paper one", "year": 2021}, {"title": "Paper two", "year": 2022}]}),
        headers={**headers(LEAD, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(LEAD, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(LEAD, "lead")).json()["items"][0][
        "stage_id"
    ]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [A, B]},
        headers=headers(LEAD, "lead"),
    )

    queue = client.get(f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}", headers=headers(A)).json()
    records = [i["review_record_id"] for i in queue["items"]]

    for record_id, disposition in zip(records, ["include", "exclude"], strict=True):
        body = {"stage_id": stage_id, "review_record_id": record_id, "disposition": disposition}
        if disposition == "exclude":
            body["exclusion_reason_code"] = "wrong_design"
        client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(A))
    for record_id in records:
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": record_id, "disposition": "include"},
            headers=headers(B),
        )

    return {"review_id": review_id, "stage_id": stage_id, "records": records}


# ---------------------------------------------------------------------------
# L1 — the RLS policy
# ---------------------------------------------------------------------------


def test_l1_is_load_bearing_removing_the_policy_leaks_the_other_reviewer(scenario):
    """Drop the policy, observe the leak, roll back.

    The whole transaction is rolled back, so the policy is never actually absent
    outside this test. What it proves is the one thing a passing blinding test
    cannot otherwise show: that the invisibility comes from THIS policy and not
    from an accident of the query.
    """
    review_id = scenario["review_id"]
    app_engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    owner_engine = create_engine(OWNER_DB_URL, future=True, connect_args={"prepare_threshold": 0})

    def visible_to_ann(conn) -> int:  # noqa: ANN001
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
        conn.execute(text("SELECT set_config('app.actor_subject_id', :s, true)"), {"s": A})
        conn.execute(text("SELECT set_config('app.actor_role', 'reviewer', true)"))
        return conn.execute(
            text("SELECT count(*) FROM screening_decisions WHERE review_id = :r"), {"r": review_id}
        ).scalar_one()

    with app_engine.connect() as conn:
        with conn.begin():
            blinded = visible_to_ann(conn)
    assert blinded == 2, "Ann must see exactly her own two decisions"

    connection = owner_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text("DROP POLICY screening_decisions_blinded ON screening_decisions"))
        connection.execute(text("ALTER TABLE screening_decisions NO FORCE ROW LEVEL SECURITY"))
        connection.execute(text("ALTER TABLE screening_decisions DISABLE ROW LEVEL SECURITY"))
        leaked = connection.execute(
            text("SELECT count(*) FROM screening_decisions WHERE review_id = :r"), {"r": review_id}
        ).scalar_one()
    finally:
        transaction.rollback()
        connection.close()

    assert leaked == 4, "with L1 removed both reviewers' decisions are visible — the layer is real"

    with app_engine.connect() as conn:
        with conn.begin():
            assert visible_to_ann(conn) == 2, "the policy must be intact after the rollback"


def test_l1_write_check_refuses_a_forged_reviewer(scenario):
    """The policy's `WITH CHECK` half.

    A reviewer cannot insert a decision attributed to somebody else even by
    reaching around the service — the application never offers the field, and
    the database refuses it independently.
    """
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with pytest.raises(Exception, match="row-level security|violates"):
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
            conn.execute(text("SELECT set_config('app.actor_subject_id', :s, true)"), {"s": A})
            conn.execute(text("SELECT set_config('app.actor_role', 'reviewer', true)"))
            row = conn.execute(
                text(
                    "SELECT stage_id, round_number, review_record_id, review_record_version_id, "
                    "assignment_id FROM screening_decisions WHERE review_id = :r LIMIT 1"
                ),
                {"r": scenario["review_id"]},
            ).one()
            conn.execute(
                text(
                    "INSERT INTO screening_decisions (decision_id, review_id, tenant_id, stage_id, "
                    "round_number, review_record_id, review_record_version_id, assignment_id, "
                    "reviewer_subject_id, disposition) VALUES "
                    "(gen_random_uuid(), :r, :t, :st, :rn, :rec, :ver, :asg, :forged, 'include')"
                ),
                {
                    "r": scenario["review_id"],
                    "t": TENANT,
                    "st": row[0],
                    "rn": row[1],
                    "rec": row[2],
                    "ver": row[3],
                    "asg": row[4],
                    "forged": B,  # Ann, claiming to be Ben
                },
            )


def _bind_actor(conn, subject: str, role: str = "reviewer") -> None:  # noqa: ANN001
    """Bind the three GUCs `screening_decisions_blinded` reads, for one statement.

    Written once because the tests below bind five times between them, and the
    three-line incantation repeated inline is the kind of noise that gets
    copy-pasted with one line stale — a test bound to the wrong SUBJECT still
    passes, it just stops measuring the clause it names.
    """
    conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT})
    conn.execute(text("SELECT set_config('app.actor_subject_id', :s, true)"), {"s": subject})
    conn.execute(text("SELECT set_config('app.actor_role', :r, true)"), {"r": role})


def test_the_write_check_refuses_a_decision_written_in_another_reviewers_name(client):
    """`WITH CHECK`'s `reviewer_subject_id` pin — which nothing covered.

    `test_l1_write_check_refuses_a_forged_reviewer` above is the test that looks
    like this one, and it does not cover the clause. Measured: replace
    `AND reviewer_subject_id = current_setting(ACTOR_GUC, true)` in the `WITH
    CHECK` with `AND TRUE` and the whole file stays green. The reason is its
    `match="row-level security|violates"`: the forged row it writes lands on a
    `(review, stage, round, record, reviewer)` slot the fixture has ALREADY
    filled, so with the pin gone the write is still refused — by
    `uq_decision_one_current_per_reviewer`, whose message contains "violates".
    The regex accepts a unique-index violation as proof of a policy, and a
    write-side separation-of-duty failure reads as a pass.

    So this test writes into an EMPTY slot: `_review_with_a_conflict_and_a_bystander`
    (defined further down, with the audience tests that share it) leaves its third
    reviewer assigned to the contested record and still pending, so nothing but the
    policy has an opinion about the row.

    The two halves are the same INSERT, byte for byte, with the same parameters
    against the same empty slot. The only thing that differs is who is bound to
    `ACTOR_GUC` while it runs. That is what makes the refusal attributable: no
    constraint, foreign key or trigger can tell the two attempts apart, so if the
    first is refused and the second succeeds, the policy is the thing that
    refused it.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "writecheck-attribution")
    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})

    # The bystander's own assignment and the record's version. Both tables are
    # tenant-scoped only, so the app role reads them with the tenant bound.
    with engine.begin() as conn:
        _bind_actor(conn, fx["bystander"])
        row = conn.execute(
            text(
                "SELECT a.assignment_id, a.stage_id, a.round_number, a.review_record_id, "
                "v.review_record_version_id FROM screening_assignments a "
                "JOIN review_record_versions v ON v.review_record_id = a.review_record_id "
                "WHERE a.review_id = :r AND a.reviewer_subject_id = :s LIMIT 1"
            ),
            {"r": fx["review_id"], "s": fx["bystander"]},
        ).one()

    insert = text(
        "INSERT INTO screening_decisions (decision_id, review_id, tenant_id, stage_id, "
        "round_number, review_record_id, review_record_version_id, assignment_id, "
        "reviewer_subject_id, disposition) VALUES "
        "(gen_random_uuid(), :r, :t, :st, :rn, :rec, :ver, :asg, :subj, 'include')"
    )
    #: `reviewer_subject_id` is the BYSTANDER in both attempts — this is a vote
    #: planted in somebody else's name, not one claimed for oneself.
    params = {
        "r": fx["review_id"],
        "t": TENANT,
        "st": row[1],
        "rn": row[2],
        "rec": row[3],
        "ver": row[4],
        "asg": row[0],
        "subj": fx["bystander"],
    }

    # THE GATE. Reviewer `first`, who has decided this record, writes a second
    # decision on it attributed to the reviewer who has not.
    #
    # Caught rather than `pytest.raises`, because the failure this test exists to
    # report is the write being ACCEPTED, and `pytest.raises` renders that as
    # "DID NOT RAISE Exception" — three words that name neither the forgery nor
    # the clause. A security test's red has to say what got through.
    refusal: Exception | None = None
    try:
        with engine.begin() as conn:
            _bind_actor(conn, fx["first"])
            conn.execute(insert, params)
    except Exception as exc:  # noqa: BLE001 - narrowed on SQLSTATE immediately below
        refusal = exc

    assert refusal is not None, (
        f"{fx['first']} wrote a screening decision attributed to {fx['bystander']} and the database "
        "ACCEPTED it. WITH CHECK is not pinning reviewer_subject_id to the bound actor, so attribution "
        "inside a blinded review is forgeable by anyone holding the runtime role"
    )
    # Asserted on SQLSTATE, not on a substring of the message. `42501` with this
    # message is `WITH CHECK`; a unique violation is `23505` and a trigger's
    # `restrict_violation` is `2F004`, and either of those would mean the write
    # was refused for a reason that has nothing to do with attribution.
    sqlstate = getattr(refusal.orig, "sqlstate", None)
    assert sqlstate == "42501", (
        f"the forged write was refused with SQLSTATE {sqlstate!r}, not the policy's 42501 — "
        f"something other than WITH CHECK stopped it, so this proves nothing: {refusal}"
    )
    assert "violates row-level security policy" in str(refusal), str(refusal)

    # THE POSITIVE CONTROL. The identical row, written by the reviewer it names.
    # Without this the test passes just as well against a database that refuses
    # every insert into this table.
    with engine.begin() as conn:
        _bind_actor(conn, fx["bystander"])
        conn.execute(insert, params)

    with engine.begin() as conn:
        _bind_actor(conn, fx["bystander"])
        mine = conn.execute(
            text("SELECT count(*) FROM screening_decisions WHERE review_id = :r AND reviewer_subject_id = :s"),
            {"r": fx["review_id"], "s": fx["bystander"]},
        ).scalar_one()
    engine.dispose()

    assert mine == 1, (
        f"the rightful subject's decision did not land ({mine}), so the slot was never empty and the "
        "refusal above cannot be attributed to the policy"
    )


# ---------------------------------------------------------------------------
# L2 — the single read path
# ---------------------------------------------------------------------------


def test_l2_single_read_path(scenario):
    """`ScreeningDecision` may be referenced only by `blinded_reads`.

    This is the layer that keeps L1 honest as the service grows: a new handler
    that queries the table directly is still covered by the policy, but nobody
    reviewed the SHAPE of what it returns, and `reviewer_subject_id` reaches a
    response body the first time somebody serializes the ORM row.
    """
    allowed = {SRC / "application" / "services" / "blinded_reads.py", SRC / "models" / "screening.py"}
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path in allowed:
            continue
        # Parsed, not grepped. A regex over the source matches the comment that
        # says "ScreeningDecision is deliberately NOT imported here" and reports
        # the module that is doing the right thing — a false positive that would
        # push the next maintainer to delete the comment rather than fix a leak.
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            referenced = (
                (isinstance(node, ast.Name) and node.id == "ScreeningDecision")
                or (isinstance(node, ast.Attribute) and node.attr == "ScreeningDecision")
                or (isinstance(node, ast.ImportFrom) and any(alias.name == "ScreeningDecision" for alias in node.names))
            )
            if referenced:
                offenders.append(str(path.relative_to(SRC)))
                break
    assert offenders == [], (
        "these modules reference ScreeningDecision directly; every read of a decision must go "
        f"through application.services.blinded_reads: {offenders}"
    )


# ---------------------------------------------------------------------------
# L3 — the queue shape
# ---------------------------------------------------------------------------


PROGRESS_LEAKS = (
    "decisions_so_far",
    "others_pending",
    "n_of_2",
    "completion",
    "reviewers_done",
    "other_reviewer",
    "awaiting",
    "conflict",
)


def test_l3_the_queue_carries_no_progress_vocabulary(client, scenario):
    response = client.get(
        f"/api/reviews/{scenario['review_id']}/screening/queue?stage_id={scenario['stage_id']}",
        headers=headers(B),
    )
    body = response.text
    for leak in PROGRESS_LEAKS:
        assert leak not in body, f"the queue payload contains {leak!r}"


def test_l3_the_decision_response_carries_no_conflict_signal(client, scenario):
    """Recording a decision must not reveal that the other reviewer disagreed."""
    review_id, stage_id = scenario["review_id"], scenario["stage_id"]
    current = client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={scenario['records'][1]}",
        headers=headers(B),
    ).json()["items"][0]

    response = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": scenario["records"][1],
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
            "supersedes_decision_id": current["decision_id"],
        },
        headers=headers(B),
    )
    assert response.status_code == 201, response.text
    for leak in PROGRESS_LEAKS:
        assert leak not in response.text, f"the decision response contains {leak!r}"


# ---------------------------------------------------------------------------
# L4 — the cursor
# ---------------------------------------------------------------------------


def test_l4_the_cursor_is_over_assignments_not_records(client, scenario):
    """A record-keyed cursor exposes gaps that reveal the other reviewer's work.

    The assertion is on the cursor's SHAPE: it must be `<timestamp>|<uuid>` where
    the uuid is one of the caller's own assignment ids, not a record id.
    """
    review_id, stage_id = scenario["review_id"], scenario["stage_id"]
    page = client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}&limit=1", headers=headers(A)
    ).json()
    assert page["next_cursor"], "a two-record queue with limit=1 must page"
    # The cursor is opaque and URL-safe; decoded, it must key on an ASSIGNMENT.
    raw = page["next_cursor"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    _, cursor_id = decoded.split("|", 1)
    assert cursor_id not in scenario["records"], "the cursor must not be keyed on a record id"
    assert cursor_id == page["items"][0]["assignment_id"]
    assert "+" not in raw and " " not in raw, "the cursor must survive a query string unescaped"

    second = client.get(
        f"/api/reviews/{review_id}/screening/queue?stage_id={stage_id}&limit=1&cursor={page['next_cursor']}",
        headers=headers(A),
    ).json()
    assert second["count"] == 1
    assert second["items"][0]["review_record_id"] != page["items"][0]["review_record_id"]


# ---------------------------------------------------------------------------
# L5 — the error shape and the logs
# ---------------------------------------------------------------------------


def test_l5_an_unassigned_record_is_404_and_the_identifier_is_absent(client, scenario):
    """`01-…:728` — assert ABSENCE, not merely the status code.

    The record genuinely exists in this review; the caller is simply not
    assigned to it. A 403 would confirm both facts.
    """
    review_id = scenario["review_id"]
    real_record = scenario["records"][0]
    response = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={"stage_id": scenario["stage_id"], "review_record_id": real_record, "disposition": "include"},
        headers=headers("reviewer-outsider"),
    )
    assert response.status_code == 404
    assert real_record not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")


def test_l5_the_log_record_carries_only_the_decision_id(client, scenario, caplog):
    """`01-…:706` — no blinded decision in logs, events or metrics."""
    review_id, stage_id = scenario["review_id"], scenario["stage_id"]
    current = client.get(
        f"/api/reviews/{review_id}/decisions?review_record_id={scenario['records'][0]}",
        headers=headers(A),
    ).json()["items"][0]

    with caplog.at_level(logging.INFO):
        client.post(
            f"/api/reviews/{review_id}/decisions",
            json={
                "stage_id": stage_id,
                "review_record_id": scenario["records"][0],
                "disposition": "uncertain",
                "supersedes_decision_id": current["decision_id"],
            },
            headers=headers(A),
        )

    lines = [r for r in caplog.records if "screening decision recorded" in r.getMessage()]
    assert lines, "the decision was not logged at all"
    for record in lines:
        rendered = json.dumps({k: str(v) for k, v in record.__dict__.items()})
        assert "uncertain" not in rendered, "the log carries the disposition"
        assert A not in rendered, "the log carries the reviewer subject"
        assert scenario["records"][0] not in rendered, "the log carries the record id"


def test_l5_conflict_detail_is_redacted_until_the_governed_door_opens(client, scenario):
    """Redaction is now the LEAD's gate, because nobody else reads this route.

    This used to be asserted on reviewer A, which measured the weaker half of a
    route that should never have answered A at all (see the test below). Holding
    lead authority is still not the same as having opened the governed door, so
    the assertion moves to the lead and keeps its meaning: `who said what` waits
    for a recorded unblinding, and `redacted` is a literal string rather than an
    empty list so "you may not see this" stays distinguishable from "there is
    nothing here".
    """
    conflicts = client.get(f"/api/reviews/{scenario['review_id']}/conflicts", headers=headers(LEAD, "lead")).json()
    assert conflicts["items"], "the fixture must produce a conflict or this asserts nothing"
    for item in conflicts["items"]:
        assert item["dispositions"] == "redacted"
        assert item["unblinded"] is False


def test_the_conflicts_read_is_lead_only_because_a_conflict_is_a_disposition(client, scenario):
    """`ER-004.2` — the second door onto the fact `record_decision` refuses to say.

    `record_decision` strips `conflict_opened` from its own response and the
    comment there gives the reason: with a binary vocabulary, "a conflict opened"
    IS the other reviewer's disposition. `GET /conflicts` then served that same
    fact to any reviewer in the tenant, one URL later — measured as A polling
    `count: 0`, B deciding, A polling `count: 1` with the record's title
    attached. The count is also monotone in the other reviewers' completed work,
    which is the aggregate leak that makes `/flow` lead-only.

    Three negatives, because they fail for three different reasons:

    * reviewer A, who took part — the "they already knew they disagreed"
      justification, which is false at the moment of the second decision;
    * reviewer B, the same;
    * a subject who has screened NOTHING and was never assigned — for whom the
      old justification was not merely early but categorically inapplicable, and
      who was nonetheless served the conflict and the record title.
    """
    path = f"/api/reviews/{scenario['review_id']}/conflicts"
    for subject in (A, B, "conflicts-bystander"):
        response = client.get(path, headers=headers(subject))
        assert response.status_code == 403, f"{subject} -> {response.status_code}: {response.text}"
        body = response.text
        # Not merely refused — refused with nothing in the body to infer from.
        # A count, a conflict id or a record title would each re-leak what the
        # 403 exists to withhold.
        for leak in ("conflict_id", "review_record_id", "dispositions", '"count"', '"open"'):
            assert leak not in body, f"the refusal to {subject} still carries {leak}"

    # The gate is a gate, not a wall: adjudication still has to be possible.
    allowed = client.get(path, headers=headers(LEAD, "lead"))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["count"] >= 1, (
        "the leak this test guards has to exist for the lead, or the 403s prove nothing"
    )


def test_the_flow_ledger_is_lead_only(client, scenario):
    """Aggregate counts are a decision leak in a two-reviewer review.

    `records_screened` moving from 1 to 2 tells reviewer B that reviewer A
    finished a record. The ledger therefore requires the lead role, and the
    negative is asserted here rather than assumed from the route decorator.
    """
    for method, path in (
        ("GET", f"/api/reviews/{scenario['review_id']}/flow"),
        ("POST", f"/api/reviews/{scenario['review_id']}/flow:rebuild"),
    ):
        response = client.request(method, path, headers=headers(A))
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
        assert "records_screened" not in response.text
    # And the lead can still read it, so the gate is a gate and not a wall.
    assert client.get(f"/api/reviews/{scenario['review_id']}/flow", headers=headers(LEAD, "lead")).status_code == 200


def test_the_creator_governs_their_own_review_and_only_theirs(client):
    """A person who creates a review can run it, without a realm role.

    The realm role alone was not enough to use the product: someone who opened
    the studio, created a review and locked its protocol could not then assign
    reviewers to it — `POST /assignments` returned 403 — because nothing had
    granted them `lead` in Keycloak. Their own fully-configured review was
    unusable, at the first thing anybody does.

    The grant is per-review and derived from `created_by_subject_id`, so this
    also asserts the narrow half: a stranger, with the same reviewer role, is
    still refused on the review they did not create.
    """
    creator = "creator-no-realm-role"
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "authority",
            "external_key": f"creator-{uuid.uuid4()}",
            "title": "Creator governs",
        },
        headers=headers(creator),  # role=reviewer, NOT lead
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
        content=json.dumps({"records": [{"title": "A paper", "year": 2021}]}),
        headers={**headers(creator), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(creator))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(creator)).json()["items"][0]["stage_id"]

    # The creator can assign, with no realm lead role.
    assigned = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [creator]},
        headers=headers(creator),
    )
    assert assigned.status_code == 201, assigned.text

    # …and read the ledger for their own review.
    assert client.get(f"/api/reviews/{review_id}/flow", headers=headers(creator)).status_code == 200

    # A STRANGER with the same role is still refused — the grant is per-review.
    stranger = client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [creator]},
        headers=headers("stranger-same-role"),
    )
    assert stranger.status_code == 403, stranger.text
    assert client.get(f"/api/reviews/{review_id}/flow", headers=headers("stranger-same-role")).status_code == 403


def test_creating_a_review_does_not_defeat_separation_of_duty(client, scenario):
    """The creator is still refused as adjudicator of a conflict they screened.

    Separation of duty is a database trigger that compares SUBJECTS and knows
    nothing about roles, so widening who counts as a lead cannot widen who may
    adjudicate. Asserted rather than assumed.
    """
    conflicts = client.get(f"/api/reviews/{scenario['review_id']}/conflicts", headers=headers(LEAD, "lead")).json()
    if conflicts["count"] == 0:
        pytest.skip("this fixture produced no conflict to adjudicate")
    conflict_id = conflicts["items"][0]["conflict_id"]

    client.post(
        f"/api/reviews/{scenario['review_id']}/conflicts/{conflict_id}:unblind",
        json={"reason": "adjudicating"},
        headers=headers(LEAD, "lead"),
    )
    # Reviewer A took part in this conflict; even as a lead they are refused.
    refused = client.post(
        f"/api/reviews/{scenario['review_id']}/conflicts/{conflict_id}:resolve",
        json={"disposition": "include", "rationale": "mine"},
        headers=headers(A, "lead"),
    )
    assert refused.status_code == 409, refused.text
    assert "cannot adjudicate" in refused.text


def test_the_creators_ledger_counts_work_done_by_other_reviewers(client):
    """The flow is a count of the review, not of what the caller can see.

    Widening authority to a review's creator left visibility behind. `/flow`
    derives from `screening_decisions`, and that policy keys off
    `app.actor_role`: a creator who holds no realm `lead` role — and none
    exists in this fleet's realm — saw only their own decisions. Records both
    reviewers had AGREED on returned zero visible votes, so the ledger filed
    them `not_yet_screened` and the export was refused permanently with
    "1 records are not yet screened" while the database held two decisions for
    each of them.

    The organiser here never screens, which is not a convenience: the database
    refuses a reviewer as adjudicator of their own conflict, so somebody has to
    stay out. That makes them exactly the caller the bug hit.
    """
    organiser = "organiser-no-realm-role"
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "ledger",
            "external_key": f"ledger-{uuid.uuid4()}",
            "title": "Organiser reads the ledger",
        },
        headers=headers(organiser),  # role=reviewer, NOT lead
    ).json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(organiser),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(organiser),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(organiser),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Agreed paper", "year": 2021}]}),
        headers={**headers(organiser), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(organiser))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(organiser)).json()["items"][0]["stage_id"]

    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [A, B]},
        headers=headers(organiser),
    )

    # Both reviewers agree — no conflict, so the organiser has nothing unblinded
    # and, before the fix, nothing visible either.
    for reviewer in (A, B):
        queue = client.get(
            f"/api/reviews/{review_id}/screening/queue",
            params={"stage_id": stage_id},
            headers=headers(reviewer),
        ).json()
        for item in queue["items"]:
            recorded = client.post(
                f"/api/reviews/{review_id}/decisions",
                json={
                    "stage_id": stage_id,
                    "review_record_id": item["review_record_id"],
                    "disposition": "include",
                },
                headers=headers(reviewer),
            )
            assert recorded.status_code == 201, recorded.text

    flow = client.get(f"/api/reviews/{review_id}/flow", headers=headers(organiser))
    assert flow.status_code == 200, flow.text
    counts = flow.json()["counts"]

    assert counts["not_yet_screened"] == 0, f"the organiser's ledger hid work done by other reviewers: {counts}"
    assert counts["records_screened"] == 1
    assert counts["included"] == 1

    # …and the export the ledger gates is therefore reachable, which is the
    # behaviour the user actually loses when this regresses.
    export = client.post(f"/api/reviews/{review_id}/exports", headers=headers(organiser))
    assert export.status_code == 201, export.text


def test_the_ledger_elevation_is_restored_after_the_read():
    """The `lead` binding is scoped to the aggregate read, not to the request.

    A transaction-scoped GUC left elevated would turn every later statement in
    the same transaction into a lead read — including the ones that enforce
    blinding, and including the export write that follows this very read.

    Asserted on the GUC itself rather than through a route, because a route
    assertion would pass for the wrong reason: the GUC is transaction-scoped,
    so a *separate* request is unaffected whether or not the restore happens.
    The only place the leak can exist is inside one transaction, so that is
    where it is measured.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.orm import Session as OrmSession

    from application.services.flow import _reading_every_reviewers_work

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with OrmSession(engine) as session, session.begin():
        session.execute(sql_text("SELECT set_config('app.actor_role', 'reviewer', true)"))

        with _reading_every_reviewers_work(session):
            elevated = session.execute(sql_text("SELECT current_setting('app.actor_role', true)")).scalar()
        restored = session.execute(sql_text("SELECT current_setting('app.actor_role', true)")).scalar()

    assert elevated == "lead", "the read must see every reviewer's work"
    assert restored == "reviewer", "the elevation outlived the read"


def test_the_ledger_elevation_is_restored_even_when_the_read_raises():
    """`finally`, not a happy path.

    A derivation that throws mid-read must not leave the transaction elevated;
    the export write that follows would then run as a lead.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.orm import Session as OrmSession

    from application.services.flow import _reading_every_reviewers_work

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})
    with OrmSession(engine) as session, session.begin():
        session.execute(sql_text("SELECT set_config('app.actor_role', 'reviewer', true)"))
        with pytest.raises(RuntimeError):
            with _reading_every_reviewers_work(session):
                raise RuntimeError("derivation blew up")
        restored = session.execute(sql_text("SELECT current_setting('app.actor_role', true)")).scalar()

    assert restored == "reviewer"


def test_the_sealed_export_is_lead_only_on_every_read_route(client):
    """The ledger's second door — `ER-004.2`, and the reason this file exists.

    `test_the_flow_ledger_is_lead_only` proves `GET /flow` refuses a reviewer and
    that `records_screened` is absent from the body. It was measuring one route.
    The same numbers are in `manifest.counts` of the export payload, verbatim,
    and `GET /exports/{id}` defaults to `with_payload=True` — so a reviewer who
    took 403 on the ledger read the whole of it, plus an `omissions` array giving
    every record's terminal bucket and exclusion reason, one URL away. Only
    `create_export` was gated, inside the service; the three reads asked
    `load_review` and nothing else.

    So the walk here is the real one — protocol, activation, import, corpus seal,
    assignment, the reviewer's own decisions, the lead's export — because the
    leak needed a genuinely sealed export to leak, and a fixture that stopped
    short would have proved the routes 404 rather than 403.
    """
    lead = "export-lead"
    reviewer = "export-reviewer"
    review_id = client.post(
        "/api/reviews",
        json={
            "project_id": "export-blinding",
            "external_key": f"exp-{uuid.uuid4()}",
            "title": "Exports are lead-only",
        },
        headers=headers(lead, "lead"),
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
        headers=headers(lead, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {"records": [{"title": "Kept paper", "year": 2021}, {"title": "Dropped paper", "year": 2022}]}
        ),
        headers={**headers(lead, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(lead, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(lead, "lead")).json()["items"][0][
        "stage_id"
    ]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [reviewer]},
        headers=headers(lead, "lead"),
    )

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(reviewer),
    ).json()
    for item, disposition in zip(queue["items"], ["include", "exclude"], strict=True):
        body = {"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": disposition}
        if disposition == "exclude":
            body["exclusion_reason_code"] = "wrong_design"
        recorded = client.post(f"/api/reviews/{review_id}/decisions", json=body, headers=headers(reviewer))
        assert recorded.status_code == 201, recorded.text

    created = client.post(f"/api/reviews/{review_id}/exports", headers=headers(lead, "lead"))
    assert created.status_code == 201, created.text
    export_id = created.json()["export_id"]

    for path in (
        f"/api/reviews/{review_id}/exports",
        f"/api/reviews/{review_id}/exports/{export_id}",
        f"/api/reviews/{review_id}/exports/{export_id}:verify",
    ):
        response = client.get(path, headers=headers(reviewer))
        assert response.status_code == 403, f"GET {path} -> {response.status_code}: {response.text}"
        # The same negative the ledger test asserts, on the body that carried it.
        assert "records_screened" not in response.text
        assert "exclusion_reason_code" not in response.text
        # Even the LIST leaks: an export exists only once every record is
        # screened and every conflict resolved, so seeing one appear tells a
        # reviewer the review is finished.
        assert "export_id" not in response.text

    # …and the gate is a gate, not a wall.
    for path in (
        f"/api/reviews/{review_id}/exports",
        f"/api/reviews/{review_id}/exports/{export_id}",
        f"/api/reviews/{review_id}/exports/{export_id}:verify",
    ):
        allowed = client.get(path, headers=headers(lead, "lead"))
        assert allowed.status_code == 200, f"GET {path} -> {allowed.status_code}: {allowed.text}"
    payload = client.get(f"/api/reviews/{review_id}/exports/{export_id}", headers=headers(lead, "lead")).json()
    assert "records_screened" in payload["payload"]["manifest"]["counts"], (
        "the leak this test guards has to be present in the body the lead reads, or a reviewer's 403 proves nothing"
    )


# --------------------------------------------------------------------------
# ER-004.2 — the audience of the governed door (`0008`)
# --------------------------------------------------------------------------


def _review_with_a_conflict_and_a_bystander(client, slug: str) -> dict:
    """Two reviewers who disagree, plus a third assigned and still pending.

    The third one is the subject of every test below: they are legitimately
    assigned to the record, so scoping the disclosure BY ASSIGNMENT does not help
    — which is what the old characterization note correctly observed.
    """
    lead, first, second, bystander = f"{slug}-lead", f"{slug}-a", f"{slug}-b", f"{slug}-pending"
    review_id = client.post(
        "/api/reviews",
        json={"project_id": slug, "external_key": f"{slug}-{uuid.uuid4()}", "title": "Who the door opens for"},
        headers=headers(lead, "lead"),
    ).json()["review_id"]

    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(lead, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Contested paper", "year": 2021}]}),
        headers={**headers(lead, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(lead, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(lead, "lead")).json()["items"][0][
        "stage_id"
    ]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [first, second, bystander]},
        headers=headers(lead, "lead"),
    )

    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(first),
    ).json()["items"][0]["review_record_id"]
    client.post(
        f"/api/reviews/{review_id}/decisions",
        json={"stage_id": stage_id, "review_record_id": record_id, "disposition": "include"},
        headers=headers(first),
    )
    client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(second),
    )

    conflict_id = client.get(f"/api/reviews/{review_id}/conflicts", headers=headers(lead, "lead")).json()["items"][0][
        "conflict_id"
    ]
    return {
        "review_id": review_id,
        "conflict_id": conflict_id,
        "lead": lead,
        "first": first,
        "second": second,
        "bystander": bystander,
    }


def test_an_unblinding_does_not_open_the_record_to_a_reviewer_it_did_not_name(client):
    """The gate. This replaces the characterization test that measured the leak.

    That test asserted `after["count"] == 2` and documented itself as "a
    MEASUREMENT of a residual this branch does not fix", ending: "If somebody
    narrows the clause this test turns red, and that is the correct outcome — it
    is the notice that the residual is gone, not a defence of it." It turned red
    on `0008`, and this is what replaced it.

    Its reasoning about WHY the obvious fix was impossible was right and still
    is: a policy on `screening_decisions` cannot subquery `screening_decisions`
    (`infinite recursion detected in policy`), and scoping by ASSIGNMENT does not
    help because the pending reviewer is assigned. A declared audience is neither
    — it is a column on `screening_unblinding_events`, whose own policy never
    mentions the decisions table.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-default")

    before = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["bystander"])).json()
    assert_sees_no_decisions(before, "a bystander read decisions before any unblinding")

    unblinded = client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
        json={"reason": "adjudicating a disagreement"},
        headers=headers(fx["lead"], "lead"),
    )
    assert unblinded.status_code == 200, unblinded.text
    assert unblinded.json()["disclosed_to_subject_ids"] == [fx["lead"]], (
        "the default audience must be the unblinding lead alone — widening blinding is something "
        "somebody chooses, never something that happens"
    )

    after = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["bystander"])).json()
    assert_sees_no_decisions(
        after,
        "a reviewer still pending on this record read the other two dispositions. They were not named "
        "in the disclosure, so the door is still keyed on the conflict rather than the caller",
    )


def test_the_policy_itself_refuses_an_actor_the_unblinding_did_not_name(client):
    """The same claim as the test above, measured one layer down — at L1.

    The test above reads `GET /decisions` over HTTP, and that route cannot fail
    on this clause. Measured: replace
    `AND current_setting(ACTOR_GUC, true) = ANY (u.disclosed_to_subject_ids)`
    with `AND TRUE` — so ANY unblinding opens the record to ANY actor in the
    tenant — and the whole file stays green. `visible_decisions` strips every row
    at a position the caller still OWES a decision on, in Python, after the query
    returns; the bystander it asserts on owes exactly that record, so their read
    is emptied by the still-owed filter whatever the policy handed back. The
    route is measuring L2, and the audience clause was never under test.

    This asserts on the policy directly, as the runtime role, because that is the
    only place the clause can be observed. Both halves read the SAME unblinding
    event on the SAME record, and differ only in whether the reader is in
    `disclosed_to_subject_ids` — so nothing else in the schema can account for
    the difference between them.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-policy")
    outsider = "audience-policy-outsider"

    unblinded = client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
        json={"reason": "panel discussion", "disclose_to": [fx["bystander"]]},
        headers=headers(fx["lead"], "lead"),
    )
    assert unblinded.status_code == 200, unblinded.text
    assert outsider not in unblinded.json()["disclosed_to_subject_ids"], (
        "the outsider must NOT be in the audience, or the negative half below asserts nothing"
    )

    engine = create_engine(DB_URL, future=True, connect_args={"prepare_threshold": 0})

    # THE POSITIVE CONTROL. The bystander was named, holds no `lead` role and has
    # decided nothing, so the first two arms of the policy give them nothing:
    # every row they can see, they can see through the audience clause alone.
    with engine.begin() as conn:
        _bind_actor(conn, fx["bystander"])
        named = (
            conn.execute(
                text("SELECT reviewer_subject_id FROM screening_decisions WHERE review_id = :r"),
                {"r": fx["review_id"]},
            )
            .scalars()
            .all()
        )

    # THE GATE. An actor in the same tenant, never assigned and never named.
    with engine.begin() as conn:
        _bind_actor(conn, outsider)
        unnamed = (
            conn.execute(
                text("SELECT reviewer_subject_id FROM screening_decisions WHERE review_id = :r"),
                {"r": fx["review_id"]},
            )
            .scalars()
            .all()
        )
    engine.dispose()

    assert sorted(named) == sorted([fx["first"], fx["second"]]), (
        f"a reviewer the disclosure names cannot read the dispositions it disclosed: {named!r}. "
        "The governed door is shut, which breaks adjudication rather than protecting it"
    )
    assert unnamed == [], (
        f"an actor the unblinding never named read {len(unnamed)} of the two dispositions: {unnamed!r}. "
        "The audience clause is being ignored, so ONE recorded unblinding opens the record to the "
        "whole tenant — which is the leak `0008` exists to close"
    )


def test_a_reviewer_the_lead_named_does_see_the_disclosed_dispositions(client):
    """The other half. A gate that refused everybody would satisfy the test above.

    Without this, deleting L1's third clause outright — closing the governed door
    entirely and breaking every panel discussion — would pass.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-named")

    unblinded = client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
        json={"reason": "panel discussion", "disclose_to": [fx["first"], fx["second"]]},
        headers=headers(fx["lead"], "lead"),
    )
    assert unblinded.status_code == 200, unblinded.text
    assert set(unblinded.json()["disclosed_to_subject_ids"]) == {fx["lead"], fx["first"], fx["second"]}, (
        "the unblinding actor must always be in the audience; adjudication has to read what it adjudicates"
    )

    named = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["first"])).json()
    assert named["count"] == 2, f"a reviewer the lead named cannot see the disclosure: {named!r}"
    assert {i["disposition"] for i in named["items"]} == {"include", "exclude"}

    # ...and naming two people did not widen it to the third.
    excluded = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["bystander"])).json()
    assert_sees_no_decisions(excluded, "naming two reviewers widened the disclosure to a third")


def test_a_disclosure_still_withholds_attribution_from_a_non_lead(client):
    """The mitigation that was already in place, kept.

    `_may_attribute` is role-based and independent of L1, so a named reviewer
    sees WHAT was decided and not BY WHOM. Asserted so that narrowing the
    audience is not mistaken for permission to widen the payload.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-attribution")

    client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
        json={"reason": "panel discussion", "disclose_to": [fx["first"]]},
        headers=headers(fx["lead"], "lead"),
    )

    named = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["first"])).json()
    assert named["count"] == 2
    assert "reviewer_subject_id" not in json.dumps(named), (
        "a named reviewer was told who decided what; the disclosure is dispositions, not attribution"
    )


def test_the_lead_can_still_adjudicate_what_they_unblinded(client):
    """The trap this design must not set.

    A lead's authority to read across reviewers comes from the `lead` role GUC,
    which a CREATOR-lead does not hold — the `review-lead` realm role does not
    exist yet, so `ActorRole.LEAD` is unreachable and lead authority collapses to
    the creator grant. Their adjudication panel therefore reads through L1's
    third clause, exactly like a reviewer's. If the audience did not include the
    unblinding actor, a lead would open a door they could not then walk through.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-lead-reads")

    client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
        json={"reason": "adjudicating"},
        headers=headers(fx["lead"], "lead"),
    )

    conflicts = client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers(fx["lead"], "lead")).json()
    dispositions = conflicts["items"][0]["dispositions"]
    assert dispositions != "redacted", (
        "the lead who recorded the unblinding cannot read the conflict they opened; the audience "
        f"does not include the actor: {conflicts!r}"
    )

    resolved = client.post(
        f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:resolve",
        json={"disposition": "include", "rationale": "the trial is eligible"},
        headers=headers(fx["lead"], "lead"),
    )
    assert resolved.status_code in (200, 201), resolved.text


def test_the_append_only_trigger_survived_the_audience_backfill():
    """`0008` disables `trg_screening_unblinding_events_append_only` to backfill.

    A trigger left disabled is silent: every write still succeeds, the table
    still looks like an audit log, and nothing in the application would ever
    notice that its rows had become editable. That is strictly worse than the
    leak `0008` closes, so the re-enable is asserted rather than assumed.

    `tgenabled = 'O'` is "fires in origin/local sessions" — the default. 'D' is
    disabled.
    """
    from sqlalchemy import create_engine, text

    url = os.getenv("MIGRATION_DATABASE_URL") or DB_URL
    engine = create_engine(url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tgname, tgenabled FROM pg_trigger "
                    "WHERE tgrelid = 'screening_unblinding_events'::regclass AND NOT tgisinternal"
                )
            ).all()
    finally:
        engine.dispose()

    assert rows, (
        "the append-only trigger is GONE from screening_unblinding_events. `0008` disables it for a "
        "backfill; if the migration failed between the DISABLE and the ENABLE the table would be "
        "editable — but a missing trigger means something dropped it entirely."
    )
    by_name = {name: enabled for name, enabled in rows}
    assert by_name.get("trg_screening_unblinding_events_append_only") == "O", (
        f"the append-only trigger is not enabled: {by_name!r}. Unblinding events are rewritable, and "
        "nothing else in this service would report that."
    )


def test_a_leads_own_queue_shows_only_their_own_decisions(client):
    """`own_decisions_for`'s subject predicate, which nothing covered.

    L1 lets a LEAD read across reviewers — that is the whole point of the role —
    so the `reviewer_subject_id = actor.subject_id` filter in
    `blinded_reads.own_decisions_for` is the ONLY thing keeping another
    reviewer's disposition out of the lead's own `my_decision`, and it is the
    reason the single-record `own_decision_for` has carried that predicate since
    it was written.

    An adversarial pass deleted the line and all 749 tests stayed green, while a
    lead assigned to a record only a REVIEWER had decided got back
    `{'disposition': 'exclude', 'reviewer_subject_id': 'other'}` in
    `my_decision`, with `decided_by_me: 1`. The batching commit's docstring
    claimed this file asserted it. It did not. Now it does.
    """
    lead, reviewer = "own-queue-lead", "own-queue-reviewer"
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "own-queue", "external_key": f"oq-{uuid.uuid4()}", "title": "Own queue only"},
        headers=headers(lead, "lead"),
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
        headers=headers(lead, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "A contested paper", "year": 2021}]}),
        headers={**headers(lead, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(lead, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(lead, "lead")).json()["items"][0][
        "stage_id"
    ]
    # BOTH assigned, so the lead has a queue row for the same record — without
    # that the lead's queue is empty and this test passes against anything.
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [reviewer, lead]},
        headers=headers(lead, "lead"),
    )

    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(reviewer),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(reviewer),
    )
    assert decided.status_code == 201, decided.text

    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(lead, "lead"),
    ).json()
    assert queue["items"], "the lead has no queue row for this record, so nothing was measured"
    row = queue["items"][0]
    assert row["my_decision"] is None, (
        "the lead's own queue is showing a decision they did not take. `my_decision` is L3's "
        f"'what have I done' field and it carries another reviewer's work: {row['my_decision']!r}"
    )
    assert queue.get("decided_by_me", 0) == 0, (
        f"the lead's own progress counts somebody else's decision: {queue.get('decided_by_me')!r}"
    )
    assert "own-queue-reviewer" not in json.dumps(queue), "the reviewer's subject id reached the lead's queue payload"


def test_an_audience_cannot_contain_an_empty_subject_id(client):
    """`""` in the audience matches every caller who presents no subject.

    `bind_actor` binds `app.actor_subject_id` to `actor.subject_id or ""`, and
    L1's third clause is `current_setting(...) = ANY(u.disclosed_to_subject_ids)`.
    So one empty string in the list opens the record to anyone holding only a
    tenant header. `0008`'s `ck_unblinding_audience_not_empty` does not catch it —
    it bounds the CARDINALITY of the list, not the emptiness of its elements.

    Measured before the fix: `{"disclose_to": [""]}` -> 200, audience
    `["", "<lead>"]`, then `GET /decisions` with only `X-Tenant-Id` -> `count: 2`.
    """
    fx = _review_with_a_conflict_and_a_bystander(client, "audience-empty")

    for hostile in ([""], ["   "], ["ok", ""]):
        refused = client.post(
            f"/api/reviews/{fx['review_id']}/conflicts/{fx['conflict_id']}:unblind",
            json={"reason": "hostile", "disclose_to": hostile},
            headers=headers(fx["lead"], "lead"),
        )
        assert refused.status_code == 422, f"disclose_to={hostile!r} was accepted: {refused.text}"

    # ...and the anonymous read stays closed, which is the thing that mattered.
    anonymous = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers={"X-Tenant-Id": TENANT})
    assert anonymous.status_code in (200, 401, 403), anonymous.text
    if anonymous.status_code == 200:
        assert_sees_no_decisions(anonymous.json(), "a caller holding only a tenant header read this review's decisions")


# --------------------------------------------------------------------------
# A lead who is also an assigned reviewer is a REVIEWER on what they still owe
# --------------------------------------------------------------------------


def _review_where_the_lead_also_screens(client, slug: str) -> dict:
    """The lead is assigned alongside a reviewer, and has decided nothing."""
    lead, reviewer = f"{slug}-lead", f"{slug}-rev"
    review_id = client.post(
        "/api/reviews",
        json={"project_id": slug, "external_key": f"{slug}-{uuid.uuid4()}", "title": "Lead screens too"},
        headers=headers(lead, "lead"),
    ).json()["review_id"]
    protocol = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(lead, "lead"),
    ).json()
    client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol['protocol_version_id']}:lock",
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol["protocol_version_id"], "rationale": "go"},
        headers=headers(lead, "lead"),
    )
    client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps({"records": [{"title": "Contested paper", "year": 2021}]}),
        headers={**headers(lead, "lead"), "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(lead, "lead"))
    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers(lead, "lead")).json()["items"][0][
        "stage_id"
    ]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [reviewer, lead]},
        headers=headers(lead, "lead"),
    )
    record_id = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id},
        headers=headers(reviewer),
    ).json()["items"][0]["review_record_id"]
    decided = client.post(
        f"/api/reviews/{review_id}/decisions",
        json={
            "stage_id": stage_id,
            "review_record_id": record_id,
            "disposition": "exclude",
            "exclusion_reason_code": "wrong_design",
        },
        headers=headers(reviewer),
    )
    assert decided.status_code == 201, decided.text
    return {"review_id": review_id, "stage_id": stage_id, "record_id": record_id, "lead": lead, "reviewer": reviewer}


def test_a_lead_who_still_owes_a_decision_cannot_read_their_partners(client):
    """THE GATE. Measured before the fix, with no unblinding and no conflict open:

        GET /decisions as the lead -> count 1, disposition 'exclude',
                                      reviewer_subject_id 'g1-rev-1'

    `GET /decisions` has no authority gate of its own — it relies entirely on
    L1, whose second clause serves a lead every reviewer's row, and
    `_may_attribute` then attaches the name. Nothing stopped the lead from being
    an assigned reviewer on the same stage.

    This bypasses the declared-audience mechanism completely: no unblinding
    event, no audience, no audit row, `/conflicts` reporting nothing. It was
    unreachable in production only because `ActorRole.LEAD` was unreachable, and
    creating the `review-lead` realm role made it live.
    """
    fx = _review_where_the_lead_also_screens(client, "owed-gate")

    seen = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["lead"], "lead")).json()
    assert_sees_no_decisions(
        seen,
        "a lead who is an assigned reviewer and has decided NOTHING read their partner's disposition "
        "before forming their own",
    )

    # ...and the queue does not leak it through `my_decision` either.
    queue = client.get(
        f"/api/reviews/{fx['review_id']}/screening/queue",
        params={"stage_id": fx["stage_id"]},
        headers=headers(fx["lead"], "lead"),
    ).json()
    assert queue["items"], "the lead has no queue row, so nothing was measured"
    assert queue["items"][0]["my_decision"] is None, queue["items"][0]


def test_once_the_lead_has_decided_they_read_across_again(client):
    """The other half, and it is what stops the fix being "leads see nothing".

    A lead must be able to adjudicate. The rule is about what they still OWE,
    not about being a lead — so the moment their own verdict is recorded the
    blinding reason is gone and the cross-reviewer read returns. Without this,
    hard-coding an empty list satisfies the gate above.
    """
    fx = _review_where_the_lead_also_screens(client, "owed-released")

    before = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["lead"], "lead")).json()
    assert_sees_no_decisions(before, "the lead still owes a decision and read across anyway")

    decided = client.post(
        f"/api/reviews/{fx['review_id']}/decisions",
        json={"stage_id": fx["stage_id"], "review_record_id": fx["record_id"], "disposition": "include"},
        headers=headers(fx["lead"], "lead"),
    )
    assert decided.status_code == 201, decided.text

    after = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["lead"], "lead")).json()
    assert after["count"] == 2, (
        f"after recording their own verdict the lead still cannot read across, so they cannot "
        f"adjudicate the conflict they just created: {after!r}"
    )
    assert {i["disposition"] for i in after["items"]} == {"include", "exclude"}


def test_a_lead_who_is_not_screening_is_unaffected(client):
    """Scoping. A lead with no assignment reads across, as they always could."""
    lead, first, second = "unowed-lead", "unowed-a", "unowed-b"
    fx = _review_with_a_conflict_and_a_bystander(client, "unowed")
    conflicts = client.get(f"/api/reviews/{fx['review_id']}/conflicts", headers=headers(fx["lead"], "lead")).json()
    assert conflicts["items"], "no conflict was produced, so nothing was measured"

    seen = client.get(f"/api/reviews/{fx['review_id']}/decisions", headers=headers(fx["lead"], "lead")).json()
    assert seen["count"] == 2, f"a lead who is NOT an assigned reviewer lost their cross-reviewer read: {seen!r}"
    assert lead and first and second
