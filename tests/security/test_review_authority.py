"""Who may govern a review, and who may claim one — `ER-001`, `01-…:687`.

`require_review_lead` is this service's whole answer to "may you do this to this
review": the realm `lead` role, or having created it. Nine holes made that
answer untrue, and none of them was visible from the function itself — each was
an absence at a call site, which is the one defect a guard cannot show you.

* **The grant could be squatted.** `create_review` returned the existing row on a
  `(project_id, external_key)` collision and called it idempotence. It is
  idempotence only for the subject who created the row; for anybody else it
  answered `201 Created` with a review they do not govern — because
  `created_by_subject_id`, the column the grant reads, stays with whoever
  inserted FIRST — and silently discarded the title they submitted.
* **One write skipped the check entirely.** `patch_review` asked `require_human`
  and nothing more, so any reviewer in the tenant could rename any
  pre-activation review anybody had created. `reviews.title` is an in-place
  `UPDATE` with no history row, so the rename was unattributable as well as
  unauthorised.
* **So did the corpus.** `import_payload` and `seal_corpus_version` asked
  `require_human` too, so any reviewer in the tenant could push their own records
  into somebody else's review and seal a new corpus version over it — the anchor
  the flow ledger, the corpus manifest and the export all cite.
* **And so did the entire lifecycle.** `transition` — which backs `:activate`,
  `:pause`, `:resume`, `:close`, `:reopen` and `:archive` — plus `activate`,
  `create_protocol_version`, `lock_protocol_version`, `deduplication.create_run`
  and `deduplication.resolve_cluster`. Six more call sites, found because
  `patch_review`'s own docstring asserted they were already gated. They were not:
  measured on `393ae61`, a reviewer who had not created the review got `201` from
  `POST /protocol-versions`, `200` from `:lock`, `:activate`, `:pause` and
  `:close`, `201` from `POST /deduplication-runs`, and `200` from a cluster
  `:resolve` with `resolution_type=merge`.

These are separated from `test_blinding_layers.py` on purpose: nothing here is
about what a reviewer may SEE. It is about what they may take.
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

TENANT = tenant("tenant-authority")
PROJECT = "authority-project"


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


def _create(client, subject: str, external_key: str, title: str, role: str = "reviewer"):  # noqa: ANN202, ANN001
    return client.post(
        "/api/reviews",
        json={"project_id": PROJECT, "external_key": external_key, "title": title},
        headers=headers(subject, role),
    )


# ---------------------------------------------------------------------------
# Squatting: a collision is not a replay unless it is YOUR replay
# ---------------------------------------------------------------------------


def test_a_second_subject_claiming_an_external_key_is_refused_not_handed_the_review(client):
    """The privilege escalation, from the direction that makes it one.

    The first caller need not be privileged. `require_review_lead` grants on
    `created_by_subject_id`, so whoever inserts first governs the review — and
    the second caller was handed that same row with a `201 Created`, which reads
    as "this is yours". It is not: the first subject can read `/flow` and
    `/exports` on it and the second cannot make them stop, because the grant
    follows a column neither of them can change.

    So the collision is refused. The 409 is actionable in the way the code it
    replaces assumed was impossible — pick another key, or ask whoever holds it.
    """
    key = f"squat-{uuid.uuid4()}"
    first = _create(client, "squatter-first", key, "The first title")
    assert first.status_code == 201, first.text
    review_id = first.json()["review_id"]

    second = _create(client, "squatter-second", key, "The second title")
    assert second.status_code == 409, f"a foreign key collision returned {second.status_code}: {second.text}"

    body = second.json()
    assert body["code"] == "idempotency_conflict", body
    assert body["type"].endswith("/idempotency_conflict")
    assert body["status"] == 409
    # The two fields the caller supplied come back so the failure is
    # self-describing. Nothing else does.
    assert body["project_id"] == PROJECT
    assert body["external_key"] == key
    # The 409 must not disclose WHOSE the key is, nor which review it is. That
    # the key is unavailable is all the caller needs to act.
    rendered = second.text
    assert "squatter-first" not in rendered, "the refusal names the other subject"
    assert review_id not in rendered, "the refusal hands over the other subject's review id"

    # And the refusal changed nothing: the row still belongs to, and is still
    # named by, the subject who created it. The old behaviour dropped the second
    # caller's title on the floor while telling them it had been created.
    unchanged = client.get(f"/api/reviews/{review_id}", headers=headers("squatter-first")).json()
    assert unchanged["title"] == "The first title"
    assert unchanged["created_by_subject_id"] == "squatter-first"


def test_the_realm_lead_role_does_not_let_a_lead_take_a_reviewers_review(client):
    """The reported repro, verbatim, because the roles make it look legitimate.

    A `lead` re-creating a key a reviewer already used got a 201 and a review the
    REVIEWER governs — the escalation runs backwards, which is why it survived
    review: the account that ends up with authority is the less privileged one,
    and the more privileged one is the party being misled.
    """
    key = f"squat-lead-{uuid.uuid4()}"
    assert _create(client, "authority-reviewer", key, "Reviewer's review").status_code == 201
    taken = _create(client, "authority-lead", key, "Lead's review", role="lead")
    assert taken.status_code == 409, taken.text
    assert taken.json()["code"] == "idempotency_conflict"


def test_the_same_subject_replaying_a_create_still_gets_their_own_review(client):
    """Idempotence is the half that was right and must not be lost.

    A retried `POST /api/reviews` from the SAME subject is the same review, and
    the unique key means there is nothing else it could be. The 409 above keys on
    the creator, not on the collision.
    """
    key = f"replay-{uuid.uuid4()}"
    first = _create(client, "replay-subject", key, "Replayed review")
    assert first.status_code == 201, first.text

    again = _create(client, "replay-subject", key, "Replayed review")
    assert again.status_code == 201, again.text
    assert again.json()["review_id"] == first.json()["review_id"]

    # A replay is not an edit: the title is not re-applied, and creatorship does
    # not move. The second property is the one the adversary confirmed and it
    # must not regress — it is what makes the 409 above necessary rather than
    # cosmetic.
    assert again.json()["created_by_subject_id"] == "replay-subject"
    renamed = client.patch(
        f"/api/reviews/{first.json()['review_id']}",
        json={"title": "Corrected after the replay"},
        headers=headers("replay-subject"),
    )
    assert renamed.status_code == 200, renamed.text
    third = _create(client, "replay-subject", key, "Replayed review")
    assert third.json()["title"] == "Corrected after the replay", (
        "a retried create silently reverted a correction, which makes the replay an edit"
    )


# ---------------------------------------------------------------------------
# PATCH: the one write on this resource that had no authority check
# ---------------------------------------------------------------------------


def test_renaming_a_review_requires_lead_authority_over_that_review(client):
    """`patch_review` called `require_human` and stopped.

    `require_human` proves the caller is a person, not that they are *this*
    person. Every other write on the resource — `:activate`, `:pause`, `:close`,
    the exports, the ledger — is `require_review_lead`; the rename was the gap,
    so any reviewer in the tenant could rename any pre-activation review. There
    is no history row for a title change, so it was unattributable too.
    """
    key = f"patch-{uuid.uuid4()}"
    created = _create(client, "patch-creator", key, "Original title")
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

    stranger = client.patch(
        f"/api/reviews/{review_id}",
        json={"title": "Renamed by a stranger"},
        headers=headers("patch-stranger"),
    )
    assert stranger.status_code == 403, f"a stranger renamed the review: {stranger.status_code} {stranger.text}"
    assert stranger.json()["code"] == "forbidden"

    # Asserted on the STORED title, not on the refusal. A 403 that still wrote
    # would pass a status-code-only test.
    assert client.get(f"/api/reviews/{review_id}", headers=headers("patch-creator")).json()["title"] == "Original title"

    # …and the gate is a gate. Both forms of lead authority still work: the
    # creator without any realm role, which is the case `require_review_lead`
    # exists for, and a realm lead who did not create it.
    by_creator = client.patch(
        f"/api/reviews/{review_id}", json={"title": "Fixed by the creator"}, headers=headers("patch-creator")
    )
    assert by_creator.status_code == 200, by_creator.text
    assert by_creator.json()["title"] == "Fixed by the creator"

    by_lead = client.patch(
        f"/api/reviews/{review_id}", json={"title": "Fixed by the lead"}, headers=headers("patch-lead", "lead")
    )
    assert by_lead.status_code == 200, by_lead.text
    assert by_lead.json()["title"] == "Fixed by the lead"


# ---------------------------------------------------------------------------
# The corpus: the anchor every later claim hangs off, with no gate on it
# ---------------------------------------------------------------------------


def test_importing_and_sealing_a_corpus_require_lead_authority_over_that_review(client):
    """`import_payload` and `seal_corpus_version` asked `require_human` and stopped.

    The same gap as the rename, on the write that matters most. Any reviewer in
    the tenant could push their own unrelated records into somebody else's review
    and seal a new corpus version over it; the only thing in the way was a
    checkbox in the studio, which is a UI convention and not an authority
    boundary.

    The consequence is not "extra rows". `identified_total` in the flow ledger is
    derived from `import_rows`, the corpus manifest hash names the exact
    membership a screening round ran against, and the export's manifest cites
    both — so foreign records make all three describe a corpus the lead never
    assembled, while every one of them still reports itself as sealed and
    verified. `import_batches` and `import_rows` are append-only, so there is no
    repair either.

    Sealing is asserted separately rather than assumed from the import refusal:
    it takes no input at all, so a gate on only the import would still let a
    stranger mint a new corpus version over whatever records are present and move
    the membership every later decision is anchored to.
    """
    key = f"corpus-{uuid.uuid4()}"
    creator = "corpus-creator"
    stranger = "corpus-stranger"
    review_id = _create(client, creator, key, "Corpus authority").json()["review_id"]

    def _import(subject: str, title: str, role: str = "reviewer"):  # noqa: ANN202
        return client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps({"records": [{"title": title, "year": 2021}]}),
            headers={**headers(subject, role), "Content-Type": "application/octet-stream"},
        )

    # The creator's own first run, with no realm lead role. This has to keep
    # working or the gate is a wall.
    mine = _import(creator, "The creator's paper")
    assert mine.status_code == 201, mine.text

    refused = _import(stranger, "A stranger's paper")
    assert refused.status_code == 403, f"a stranger imported into the review: {refused.status_code} {refused.text}"
    assert refused.json()["code"] == "forbidden"

    refused_seal = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(stranger))
    assert refused_seal.status_code == 403, f"a stranger sealed a corpus: {refused_seal.status_code}"
    assert refused_seal.json()["code"] == "forbidden"

    # Asserted on the CORPUS, not on the status codes. A 403 that had already
    # written the rows would pass everything above.
    sealed = client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(creator))
    assert sealed.status_code == 201, sealed.text
    assert sealed.json()["member_count"] == 1, (
        "the stranger's record reached the sealed corpus; the refusal came after the write"
    )

    # …and a realm lead who did not create it is still allowed, same as every
    # other write on this resource.
    lead_import = _import("corpus-realm-lead", "A lead's paper", role="lead")
    assert lead_import.status_code == 201, lead_import.text


def test_a_service_principal_still_cannot_rename_a_review(client):
    """`require_review_lead` calls `require_human` first, and that must stay true.

    Swapping one guard for another is where a second guarantee gets dropped by
    accident: `12-…:121-124` refuses a service principal on any human judgement,
    and it was `require_human` that enforced it here. `require_review_lead` calls
    it first — asserted, not assumed from reading the call.

    A tenant with no subject is what a service principal looks like on the
    header path; under real auth `is_service_principal` comes off the claims.
    """
    key = f"patch-svc-{uuid.uuid4()}"
    review_id = _create(client, "svc-owner", key, "Human owned").json()["review_id"]
    response = client.patch(
        f"/api/reviews/{review_id}",
        json={"title": "Renamed by a robot"},
        headers={"X-Tenant-Id": TENANT},
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "human_principal_required", response.text
    assert client.get(f"/api/reviews/{review_id}", headers=headers("svc-owner")).json()["title"] == "Human owned"


# ---------------------------------------------------------------------------
# The lifecycle, the protocol and the dedup writes: six more call sites that
# asked `require_human` and stopped
# ---------------------------------------------------------------------------

PROTOCOL_BODY = {
    "inclusion_criteria": {"population": "adults"},
    "exclusion_criteria": {"design": "case report"},
    "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
    "exclusion_reason_vocabulary": ["wrong_design"],
    "reviewers_required": 2,
}

#: Two records share a DOI so the dedup baseline proposes exactly one cluster,
#: and a third is unique so the corpus is not entirely duplicates.
DEDUP_CORPUS = {
    "records": [
        {"title": "A trial of something", "doi": "10.1000/authority-1", "year": 2021},
        {"title": "A Trial Of Something", "doi": "https://doi.org/10.1000/authority-1", "year": 2021},
        {"title": "An unrelated study", "doi": "10.1000/authority-2", "year": 2019},
    ]
}


def _protocol(client, review_id: str, subject: str, role: str = "reviewer"):  # noqa: ANN202, ANN001
    return client.post(
        f"/api/reviews/{review_id}/protocol-versions", json=PROTOCOL_BODY, headers=headers(subject, role)
    )


def _locked_protocol(client, review_id: str, subject: str) -> str:
    """A protocol version, locked, by the review's creator. No realm role."""
    created = _protocol(client, review_id, subject)
    assert created.status_code == 201, created.text
    protocol_id = created.json()["protocol_version_id"]
    locked = client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers(subject))
    assert locked.status_code == 200, locked.text
    return protocol_id


def test_a_stranger_cannot_drive_the_lifecycle_of_someone_elses_review(client):
    """`transition` asked `require_human`, and every lifecycle command goes through it.

    `:activate`, `:pause`, `:resume`, `:close`, `:reopen` and `:archive` are six
    URLs over one service function, so the single missing gate there was the
    whole authority story for the review lifecycle. Measured on `393ae61`: a
    reviewer who had not created this review closed it.

    Two of these are not merely disruptive. `archived` has no outgoing edge in
    `LEGAL_TRANSITIONS`, so a stranger could put a review beyond every subsequent
    write, permanently and with no remedy. And a reopen reactivates a review
    whose decisions have already been exported — on an attestation supplied by
    the same caller who is not entitled to be issuing it.

    Every refusal is asserted against the STORED state at the end, not only
    against its status code: a 403 emitted after the state event was appended
    would pass a status-code-only test, and the history is append-only, so it
    would not be repairable either.
    """
    key = f"lifecycle-{uuid.uuid4()}"
    creator, stranger = "lifecycle-creator", "lifecycle-stranger"
    review_id = _create(client, creator, key, "Lifecycle authority").json()["review_id"]
    protocol_id = _locked_protocol(client, review_id, creator)

    def _state() -> str:
        return client.get(f"/api/reviews/{review_id}", headers=headers(creator)).json()["state"]

    def _command(name: str, subject: str, role: str = "reviewer", **body):  # noqa: ANN202
        return client.post(
            f"/api/reviews/{review_id}:{name}",
            json={"rationale": f"{name} by {subject}", **body},
            headers=headers(subject, role),
        )

    # `:activate` has its own body and its own service function, which reads the
    # protocol version BEFORE transitioning — so it is asserted separately rather
    # than assumed from the shared handler.
    stolen_activation = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "activated by a stranger"},
        headers=headers(stranger),
    )
    assert stolen_activation.status_code == 403, (
        f"a stranger activated the review: {stolen_activation.status_code} {stolen_activation.text}"
    )
    assert stolen_activation.json()["code"] == "forbidden"
    assert _state() == "draft", "the refusal came after the state event was appended"

    # The creator, with no realm role, drives their own review the whole way.
    # Without this the gate could be a wall and every assertion above would still
    # pass.
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "the creator's own activation"},
        headers=headers(creator),
    )
    assert activated.status_code == 200, activated.text

    assert _command("pause", stranger).status_code == 403
    assert _command("pause", creator).status_code == 200
    assert _command("resume", stranger).status_code == 403
    assert _command("resume", creator).status_code == 200

    assert _command("close", stranger).status_code == 403
    assert _command("close", creator).status_code == 200

    # From `closed`, both remaining commands are legal transitions — so a 403
    # here is the gate and not the router's `from_states` check answering 409.
    reopened = _command("reopen", stranger, approval_attestation_ref="fr-attestation-forged")
    assert reopened.status_code == 403, f"a stranger reopened a closed review: {reopened.text}"
    archived = _command("archive", stranger)
    assert archived.status_code == 403, f"a stranger archived a review permanently: {archived.text}"

    assert _state() == "closed", "one of the stranger's lifecycle writes landed"

    # …and a realm lead who did not create it is still allowed, same as every
    # other write on this resource.
    assert _command("archive", "lifecycle-realm-lead", role="lead").status_code == 200
    assert _state() == "archived"


def test_a_stranger_cannot_write_or_lock_a_protocol_version(client):
    """`create_protocol_version` and `lock_protocol_version` asked `require_human`.

    The lock is the one that cannot be undone. The immutability trigger permits
    the `UPDATE` only while `locked_at IS NULL`, there is no unlock route, and
    the lock materialises the `ReviewStage` rows every assignment and decision
    then references. A reviewer who could reach it could freeze somebody else's
    half-written protocol permanently.

    Writing a version is not additive either: it supersedes the previous one, and
    `activate` binds ONE version as the criteria every decision in the review is
    judged against.
    """
    key = f"protocol-{uuid.uuid4()}"
    creator, stranger = "protocol-creator", "protocol-stranger"
    review_id = _create(client, creator, key, "Protocol authority").json()["review_id"]

    def _versions() -> list[dict]:
        return client.get(f"/api/reviews/{review_id}/protocol-versions", headers=headers(creator)).json()["items"]

    refused = _protocol(client, review_id, stranger)
    assert refused.status_code == 403, f"a stranger wrote a protocol version: {refused.text}"
    assert refused.json()["code"] == "forbidden"
    assert _versions() == [], "the refusal came after the version was written"

    mine = _protocol(client, review_id, creator)
    assert mine.status_code == 201, mine.text
    protocol_id = mine.json()["protocol_version_id"]

    stolen_lock = client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers(stranger)
    )
    assert stolen_lock.status_code == 403, f"a stranger locked the protocol: {stolen_lock.text}"
    assert stolen_lock.json()["code"] == "forbidden"
    # Asserted on the STORED lock, which is irreversible: a 403 that had already
    # written `locked_at` would leave nothing to repair.
    assert _versions()[0]["locked"] is False
    assert client.get(f"/api/reviews/{review_id}/stages", headers=headers(creator)).json()["count"] == 0

    assert (
        client.post(
            f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers(creator)
        ).status_code
        == 200
    )
    assert _versions()[0]["locked"] is True

    # A realm lead who did not create it may still re-protocol, like every other
    # write on this resource.
    assert _protocol(client, review_id, "protocol-realm-lead", role="lead").status_code == 201


def test_a_stranger_cannot_start_a_dedup_run_or_merge_records_away(client):
    """`deduplication.create_run` and `resolve_cluster` asked `require_human`.

    The run is what makes the merge reachable: `resolve_cluster` can only be
    pointed at a cluster some run proposed, so an unauthorised run hands the
    caller the cluster ids as well.

    A merge does not delete a record — `test_merge_split_merge_is_reversible_and_
    attributable` pins that no source row is mutated — but it does move what the
    review COUNTS. `flow.py:142-162` walks each cluster's resolution-chain tail
    and drops every member except the survivor from the screened population, so
    an unauthorised merge changes `duplicates_removed` in the PRISMA ledger and
    the membership the export seals. The refusal is therefore asserted against
    the resolution CHAIN, not against `GET /records`, whose total a merge is
    supposed to leave alone.
    """
    key = f"dedup-{uuid.uuid4()}"
    creator, stranger = "dedup-creator", "dedup-stranger"
    review_id = _create(client, creator, key, "Dedup authority").json()["review_id"]
    protocol_id = _locked_protocol(client, review_id, creator)
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol_id, "rationale": "dedup fixture"},
            headers=headers(creator),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps(DEDUP_CORPUS),
            headers={**headers(creator), "Content-Type": "application/octet-stream"},
        ).status_code
        == 201
    )
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers(creator)).status_code == 201

    refused_run = client.post(f"/api/reviews/{review_id}/deduplication-runs", json={}, headers=headers(stranger))
    assert refused_run.status_code == 403, f"a stranger started a dedup run: {refused_run.text}"
    assert refused_run.json()["code"] == "forbidden"
    listed = client.get(f"/api/reviews/{review_id}/deduplication-runs", headers=headers(creator)).json()
    assert listed["count"] == 0, "the refusal came after the run was written"

    mine = client.post(f"/api/reviews/{review_id}/deduplication-runs", json={}, headers=headers(creator))
    assert mine.status_code == 201, mine.text
    cluster = client.get(f"/api/reviews/{review_id}/duplicate-clusters", headers=headers(creator)).json()["items"][0]
    merge_body = {
        "resolution_type": "merge",
        "rationale": "a stranger's merge",
        "survivor_record_id": cluster["members"][0]["review_record_id"],
    }

    refused_merge = client.post(
        f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
        json=merge_body,
        headers=headers(stranger),
    )
    assert refused_merge.status_code == 403, f"a stranger merged records away: {refused_merge.text}"
    assert refused_merge.json()["code"] == "forbidden"

    def _chain() -> dict:
        return client.get(
            f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}/resolutions",
            headers=headers(creator),
        ).json()

    assert _chain()["count"] == 0, "the refusal came after the resolution was appended"

    # The creator's own merge lands, so the assertion above is not passing
    # because the route is broken for everyone.
    assert (
        client.post(
            f"/api/reviews/{review_id}/duplicate-clusters/{cluster['cluster_id']}:resolve",
            json={**merge_body, "rationale": "the creator's own merge"},
            headers=headers(creator),
        ).status_code
        == 200
    )
    chain = _chain()
    assert chain["count"] == 1
    assert chain["items"][0]["decided_by_subject_id"] == creator


# --------------------------------------------------------------------------
# The `review-lead` realm role — lead authority that can be HANDED OVER
# --------------------------------------------------------------------------


def test_a_client_role_confers_lead_and_not_only_a_realm_role() -> None:
    """`_role_from` read `realm_access` and a bare `roles` list, and stopped.

    Keycloak puts REALM roles in `realm_access.roles` and CLIENT roles in
    `resource_access.<client_id>.roles`. A `review-lead` granted as a client role
    on `portal-bff` — the normal way to scope a role to one application instead
    of making it visible to every client in the realm — was invisible here.

    Silently, which is the part that matters: there is no error for "you hold a
    role this service cannot see". The caller became `ActorRole.REVIEWER`,
    `require_review_lead` fell back to the creator grant, and the only person who
    could close the review was whoever created it.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from actor import ActorRole
    from api.dependencies import _role_from

    assert _role_from({"resource_access": {"portal-bff": {"roles": ["review-lead"]}}}) is ActorRole.LEAD
    assert _role_from({"realm_access": {"roles": ["review-lead"]}}) is ActorRole.LEAD
    assert _role_from({"roles": ["review-lead"]}) is ActorRole.LEAD

    # Any client, not one named client. Pinning a client id would make authority
    # depend on which client minted the token, so the same person would be a lead
    # through the studio and a reviewer through a script.
    assert _role_from({"resource_access": {"some-other-client": {"roles": ["review-lead"]}}}) is ActorRole.LEAD


def test_an_unrecognised_role_is_still_a_reviewer() -> None:
    """The fail-closed default, and the malformed shapes that must not crash it.

    Widening where the roles are read from is exactly the change that could turn
    "anything unrecognised is a reviewer" into an escalation, so both directions
    are pinned.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from actor import ActorRole
    from api.dependencies import _role_from

    assert _role_from({}) is ActorRole.REVIEWER
    assert _role_from({"resource_access": {"portal-bff": {"roles": ["reviewer"]}}}) is ActorRole.REVIEWER
    assert _role_from({"resource_access": {"portal-bff": {"roles": ["review-leader"]}}}) is ActorRole.REVIEWER
    # Malformed claim shapes: a string where a dict belongs, a null grant, roles
    # that are not a list. None of these may raise, and none may confer LEAD.
    assert _role_from({"resource_access": "not-a-dict"}) is ActorRole.REVIEWER
    assert _role_from({"resource_access": {"c": None}}) is ActorRole.REVIEWER
    assert _role_from({"resource_access": {"c": {}}}) is ActorRole.REVIEWER
    assert _role_from({"realm_access": None}) is ActorRole.REVIEWER


def test_a_lead_who_did_not_create_the_review_can_close_it(client):
    """THE HANDOVER — with an honest note about what this does and does not gate.

    `require_review_lead` falls through to a creator grant when the caller is not
    `ActorRole.LEAD`. Until the `review-lead` realm role existed, LEAD was
    unreachable by any token, so a review whose creator's account was gone could
    never be closed or re-protocoled, and there was no way to hand that over.

    **This test does not gate the `resource_access` change it was shipped with.**
    An adversarial pass deleted that entire block and this stayed green: it drives
    `X-Actor-Role: lead`, the AUTH_BYPASS header path, which produced
    `ActorRole.LEAD` before the change too. It was listed as one of three gates
    for it and gated nothing it changed. The real one is
    `test_a_client_role_confers_lead_and_not_only_a_realm_role`, which exercises
    `_role_from` directly — deleting the block kills that and only that.

    What this test IS: the end-to-end assertion that lead authority is not
    creator-bound, however LEAD was obtained. Worth having, and the reason the
    name lost `realm_` — nothing here proves the role came from a realm.

    It also now calls `:close` — the route path; the operation_id is `close_review`,
    which is what the old name promised and the old
    body never did.
    """
    creator, successor = f"handover-creator-{uuid.uuid4().hex[:8]}", "handover-successor"
    key = f"handover-{uuid.uuid4()}"

    created = _create(client, creator, key, "A review whose owner left")
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

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
    )
    assert protocol.status_code == 201, protocol.text

    # The control. A plain reviewer who did not create it is still refused, so
    # this test cannot pass by the authority check having been removed.
    refused = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(successor),
    )
    assert refused.status_code == 403, refused.text

    # The handover: the same subject, holding the lead role.
    allowed = client.post(
        f"/api/reviews/{review_id}/protocol-versions",
        json={
            "inclusion_criteria": {},
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "TA"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 1,
        },
        headers=headers(successor, "lead"),
    )
    assert allowed.status_code == 201, (
        "a lead who did not create the review cannot govern it, so there is no handover: if the "
        f"creator's account is gone the review is permanently stuck. {allowed.text}"
    )

    # ...and `:close_review` itself, the operation the handover exists for.
    protocol_id = allowed.json()["protocol_version_id"]
    locked = client.post(
        f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock",
        headers=headers(successor, "lead"),
    )
    assert locked.status_code == 200, locked.text
    activated = client.post(
        f"/api/reviews/{review_id}:activate",
        json={"protocol_version_id": protocol_id, "rationale": "the successor activates"},
        headers=headers(successor, "lead"),
    )
    assert activated.status_code == 200, activated.text

    # The control first, so this cannot pass by the authority check being gone.
    refused = client.post(
        f"/api/reviews/{review_id}:close",
        json={"rationale": "a stranger closing it"},
        headers=headers(successor),
    )
    assert refused.status_code == 403, f"a plain reviewer closed somebody else's review: {refused.text}"

    closed = client.post(
        f"/api/reviews/{review_id}:close",
        json={"rationale": "the successor closes it"},
        headers=headers(successor, "lead"),
    )
    assert closed.status_code == 200, (
        f"a lead who did not create the review cannot CLOSE it, which is the whole point of the handover: {closed.text}"
    )
