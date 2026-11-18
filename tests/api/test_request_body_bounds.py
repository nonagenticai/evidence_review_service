"""A nested request body must leave as problem+json, never as a bare 500.

`09-new-service-specifications.md` and `INT-KAG-PROXY` both require an intact
`application/problem+json` body to survive the gateway, and
`api.problem_details` exists so that every failure this service raises carries a
stable, branchable `type`. Three request bodies did not honour that, and all
three failed the same way: a recursive walk hit the interpreter's ~1,000-frame
limit, `RecursionError` derives from `RuntimeError` rather than `ValueError`, so
it went past every handler between the walk and the edge and left as
`500 text/plain` — no body for the gateway to forward and nothing for a client
to branch on.

Measured on `393ae61`:

* `POST /api/reviews/{id}/protocol-versions` with a 1,200-level
  `inclusion_criteria` → **500 `text/plain`**. `ProtocolBody`'s criteria are
  `dict[str, Any]`, so Pydantic validates the outer shape and nothing inside.
  The guard runs before `actor_scope`, so a tenant header is the whole
  prerequisite.
* `POST /api/reviews/{id}/deduplication-runs` with a 1,200-level `config` →
  **500 `text/plain`** on a review with a sealed corpus. Note the qualifier: with
  a nonexistent review id the route 404s before it ever reaches the crash, so a
  test that skipped the fixture would pass against the defect.
* `POST /api/reviews/{id}/imports` with a 1,200-level JSON *envelope* → **500
  `text/plain`**. The parser's `MAX_JSON_DEPTH` does not cover this: the router
  guards the raw envelope at `routers/imports.py:73`, before any parser runs.

And one that was not a crash at all: `DedupRunBody.config` reached neither the
depth bound nor the denylist, so a body carrying a guarded pre-ADR regulatory
discriminator returned **`201`** and was persisted into `deduplication_runs.config`
and into the RFC 8785 `input_digest`.

Every test here asserts the exact status, the media type and the `code`, not
merely "not 500". A 4xx with a plain-text body would satisfy a status-code-only
assertion and would still be the failure this file exists to prevent — and,
symmetrically, the `code` alone does not pin the status, because the two are
declared together on one line in `problem_details.py` and a change to that line
moves the status while leaving the code identical. See `_assert_typed_refusal`
for what that costs when it is left loose; it was measured, not assumed.
"""

from __future__ import annotations

import json
import os
import tracemalloc
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

DB_URL = os.getenv("DATABASE_URL", "")
SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: A uuid, not the readable `"tenant-body-bounds"` it used to be.
#:
#: `0014` made every `tenant_id` column `uuid` and `api/dependencies._tenant_uuid`
#: refuses a header that will not parse as one. Measured with the old literal:
#: every request in this file, fixture included, came back
#: `401 {"code":"unauthenticated","detail":"the tenant named by X-Tenant-Id is
#: not a UUID"}` — so the file asserted the tenant boundary nine times over and
#: the body bounds it exists for not at all. The label lives in this comment now.
TENANT = "0195f48b-d000-7000-8000-0000000b0dc5"
LEAD = "bounds-lead"

#: Comfortably past the interpreter's ~1,000-frame limit, and small — the whole
#: point of this class of payload is that it costs the caller nothing.
#:
#: Note that this constant sits BELOW the range where the denylist walk becomes a
#: memory amplifier, which is why
#: `test_the_envelope_depth_guard_refuses_before_the_denylist_walk_does_any_work`
#: builds its own, much deeper body instead of reusing it. A bound that is only
#: ever exercised at a safe depth is not a gate.
CRASHING_DEPTH = 1_200

#: Ceiling on Python allocation for refusing one amplifying envelope.
#:
#: Measured on the same 48 KiB request: **289.7 MiB without the envelope guard,
#: 1.1 MiB with it**. 32 MiB sits ~30x above the guarded figure and ~9x below the
#: unguarded one, so it is not a squeeze in either direction.
#:
#: Deliberately `tracemalloc` rather than wall time or RSS. Wall time does not
#: discriminate — the unguarded refusal takes 0.04s, because the amplification
#: here is in how much is ALIVE AT ONCE, not in how long it takes; an earlier
#: version of this test asserted a time bound and would have passed against the
#: defect. And `resource.ru_maxrss` is a process high-water mark that never
#: falls, so any earlier test that peaked higher would silently make it vacuous.
AMPLIFIER_BUDGET_MIB = 32

#: The prefix only the ENVELOPE depth guard produces.
#:
#: `"nests more than"` alone is not discriminating: the parser's `_audit_json`
#: emits `"payload nests more than 64 levels deep"` for the same body one layer
#: later, AFTER the amplifying walk has already run. Measured — without the
#: envelope guard, a 40,000-deep body is still refused with a `validation_failed`
#: whose detail matches `"nests more than"`. Asserting the loose substring would
#: have made this test green against the very defect it exists to pin, which is
#: the same mistake its predecessor made with `CRASHING_DEPTH`.
ENVELOPE_GUARD_DETAIL = "the import envelope nests more than"

#: The deepest thing an exporter or a studio actually emits, roughly. The bound
#: must not refuse this, or it has traded a 500 for a 422 on real traffic.
REALISTIC_PROTOCOL_CRITERIA: dict[str, Any] = {
    "population": {"any_of": [{"term": "adults", "source": {"vocabulary": "MeSH", "id": "D000328"}}]},
    "intervention": {"any_of": [{"term": "physiotherapy"}]},
}


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


def nested(depth: int) -> dict[str, Any]:
    """`{"a": {"a": {"a": …}}}`, `depth` levels of it."""
    node: dict[str, Any] = {}
    cursor = node
    for _ in range(depth):
        cursor["a"] = {}
        cursor = cursor["a"]
    return node


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
def review(client) -> str:  # noqa: ANN001
    """A review with a locked protocol and a sealed corpus.

    The dedup route needs all of it. `create_run` calls `latest_corpus` before it
    ever touches `config`, so without a sealed corpus the deep-config request
    fails on the corpus and the depth defect is never reached — the test would
    pass against the bug.
    """
    created = client.post(
        "/api/reviews",
        json={"project_id": "bounds", "external_key": f"bounds-{uuid.uuid4()}", "title": "Body bounds"},
        headers=headers(),
    )
    assert created.status_code == 201, created.text
    review_id = created.json()["review_id"]

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
    )
    assert protocol.status_code == 201, protocol.text
    protocol_id = protocol.json()["protocol_version_id"]
    assert (
        client.post(f"/api/reviews/{review_id}/protocol-versions/{protocol_id}:lock", headers=headers()).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}:activate",
            json={"protocol_version_id": protocol_id, "rationale": "bounds fixture"},
            headers=headers(),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
            content=json.dumps({"records": [{"title": "A study", "doi": "10.1000/bounds-1", "year": 2020}]}),
            headers={**headers(), "Content-Type": "application/octet-stream"},
        ).status_code
        == 201
    )
    assert client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers()).status_code == 201
    return review_id


def _assert_typed_refusal(response, code: str, status: int) -> None:
    """The exact status, `application/problem+json`, and the named code. All three.

    `status` is a literal at every call site, and it is passed in rather than
    read off the exception class, because `ProblemError` declares status and slug
    on the same line — `status, slug, title = 422, "validation_failed", ...`.
    Deriving the expectation from that line would make this function assert only
    that the app agrees with itself, which is not an assertion.

    It used to accept any `400 <= status < 500`, on the reasoning that the `code`
    is the branchable identity and the band was therefore only belt-and-braces.
    Measured, that reasoning was wrong: editing `ValidationFailedError.status`
    from `422` to `400` leaves the whole 710-test suite green. Every refusal in
    this file is a `validation_failed`, so the loose band was the only thing
    standing between that edit and a silent contract change — and the `code`
    assertion cannot see it, because the code is exactly what does NOT change.

    The band was not redundant everywhere, though. The same edit applied to
    `ForbiddenDiscriminatorError` (`400` -> `422`) is caught, by two tests in
    `test_e2e_acceptance.py` that assert the status directly. The point is that
    nothing was doing that job for `validation_failed`, and this file is where a
    reader would reasonably expect it to be done.
    """
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code} with body {response.text[:200]!r}"
    )
    assert response.headers["content-type"].startswith("application/problem+json"), (
        f"the refusal is not problem+json: {response.headers.get('content-type')!r} / {response.text[:200]!r}"
    )
    assert response.json()["code"] == code, response.text


def test_a_deeply_nested_protocol_body_is_a_typed_refusal_not_a_500(client, review) -> None:
    """The reported case. `500 text/plain` on `393ae61`."""
    response = client.post(
        f"/api/reviews/{review}/protocol-versions",
        json={
            "inclusion_criteria": nested(CRASHING_DEPTH),
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(),
    )
    _assert_typed_refusal(response, "validation_failed", 422)
    assert "64" in response.json()["detail"], "the refusal must say what the bound is"


def test_the_protocol_depth_guard_needs_only_a_tenant_header(client) -> None:
    """`_guard` runs before `actor_scope`, which is what made this cheap to reach.

    Asserted against a review id that does not exist: the refusal must still be
    the typed depth one, proving nothing about authority or existence is required
    to get past the crash site.

    "Only a tenant header" is the claim, and it is unchanged: no `X-Subject-Id`,
    no `X-Actor-Role`, no review that exists. What DID change is that the header
    must now name a real uuid. `ActorDep` is a FastAPI dependency, so `get_actor`
    has always resolved before the handler — and therefore before `_guard` —
    which was invisible while any non-empty string was an acceptable tenant.
    Since `0014` it is not, and a malformed one is refused `401` at the boundary
    (see `test_a_tenant_header_that_is_not_a_uuid_is_refused_at_the_boundary` in
    `tests/security/test_token_verification.py`, which pins that separately).
    Leaving the old junk value here would have swapped this test's subject from
    the depth guard to the credential check without renaming it.
    """
    response = client.post(
        f"/api/reviews/{uuid.uuid4()}/protocol-versions",
        json={
            "inclusion_criteria": nested(CRASHING_DEPTH),
            "exclusion_criteria": {},
            "stage_definitions": [{"key": "ta"}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers={"X-Tenant-Id": TENANT},
    )
    _assert_typed_refusal(response, "validation_failed", 422)


def test_a_deeply_nested_dedup_config_is_a_typed_refusal_not_a_500(client, review) -> None:
    """`config` feeds `input_digest`, which canonicalises under RFC 8785 — recursively."""
    response = client.post(
        f"/api/reviews/{review}/deduplication-runs",
        json={"config": nested(CRASHING_DEPTH)},
        headers=headers(),
    )
    _assert_typed_refusal(response, "validation_failed", 422)


def test_a_guarded_discriminator_in_a_dedup_config_is_refused(client, review) -> None:
    """`201` on `393ae61`. Not a crash — an absence.

    `ER-002.7` is "reject, never silently drop", and the reviews router's guard
    describes itself as applying to every inbound body. This body was simply not
    wired to it, so the value was persisted to `deduplication_runs.config` and
    folded into the run's `input_digest` — making the run's identity depend on a
    field the programme has refused to accept.
    """
    response = client.post(
        f"/api/reviews/{review}/deduplication-runs",
        json={"config": {"rules": ["normalized_doi"], "work_type": "regulatory_instrument"}},
        headers=headers(),
    )
    _assert_typed_refusal(response, "forbidden_discriminator", 400)
    assert response.json()["forbidden_paths"] == ["$.work_type"]


def test_a_deeply_nested_import_envelope_is_a_typed_refusal_not_a_500(client, review) -> None:
    """The envelope path the parser's `MAX_JSON_DEPTH` does not cover.

    `routers/imports.py` guards the raw envelope before any parser runs, so
    `_audit_json` — the bound that closes this for record payloads — has not
    executed yet. Measured `500 text/plain` on `393ae61`.

    The refusal must come from the ENVELOPE DEPTH GUARD specifically, which is
    what `nests more than` in the detail asserts. That precision is the lesson
    from this test's first version, which asserted only "some typed 4xx" and was
    therefore green for the wrong reason: at this depth `json.dumps(inner)`
    raises `RecursionError` and the pre-existing re-encode catch converts it to
    the same `validation_failed` status. The test passed while the guard it
    exists to pin was absent — and while everything before that catch had already
    run. See the amplifier test below for what "everything before" cost.
    """
    response = client.post(
        f"/api/reviews/{review}/imports",
        json={
            "source_kind": "canonical_json",
            "payload": {"records": [{"title": "A study", "year": 2020, "authors": nested(CRASHING_DEPTH)}]},
        },
        headers=headers(),
    )
    _assert_typed_refusal(response, "validation_failed", 422)
    assert ENVELOPE_GUARD_DETAIL in response.json()["detail"], (
        f"the envelope was refused by something other than the depth guard: {response.json()['detail']!r}"
    )


def test_the_envelope_depth_guard_refuses_before_the_denylist_walk_does_any_work(client, review) -> None:
    """The regression the iterative walk introduced, and the reason for the order.

    Making `find_forbidden_keys` iterative fixed a `500` and created something
    worse in its place. The `RecursionError` was also, accidentally, what
    TERMINATED the walk; without it the walk runs to completion, building an
    O(depth) path string per node. That is O(nodes x depth) — a memory
    amplifier, on a body the caller can write in one line.

    Measured through the real ASGI app on the intermediate commit, before this
    guard existed: the 48 KiB body below allocated **289.7 MiB** to refuse — and
    a 371 KiB variant reached 3.28 GB RSS — against a deployment `limits.memory`
    of 512Mi with `replicas: 1` and `strategy: Recreate`. Ten concurrent copies
    reached 1.94 GB, linear in concurrency, and the concurrency is itself new:
    the import handler now runs in the threadpool instead of serialising on the
    event loop. On `393ae61` the same request was a `500` in 0.01s at a flat
    140 MB. With the guard it is 1.1 MiB.

    Reachable before `actor_scope`, before `load_review`, before
    `require_review_lead_for` and before the parser's size and depth bounds, so a
    tenant header is the entire prerequisite.

    Two assertions, and both discriminate:

    * the detail must be the ENVELOPE guard's. Not merely "some typed 4xx", and
      not even the looser `"nests more than"` — measured without the guard, the
      same body is still refused `422`, and its detail still contains
      `"nests more than"`, because the parser's `_audit_json` catches it one
      layer later, AFTER the amplifying walk has run. The two are told apart by
      which subject the message names: `"the import envelope"` here, `"payload"`
      there. See :data:`ENVELOPE_GUARD_DETAIL`.
    * peak Python allocation for the request, because the defect IS the
      allocation. A correct-looking status code is exactly what it produced.

    ## The body shape is the measurement, not a detail of it

    The amplifier is not "deep" — it is deep AND wide. Every child of a node is
    pushed onto the walk's stack carrying its own full path, so a level with
    20,000 siblings 5,000 levels down means 20,000 paths of 5,000 segments alive
    simultaneously. A deep CHAIN does not reproduce it at all: each path is
    transient, and a 234 KiB 40,000-deep chain costs 0.09s and 27 MiB — under any
    reasonable budget. The shape below is the reported one, `48 KiB`, and it is
    what the caller would actually send.

    The payload is built as TEXT rather than with `json.dumps`, which would
    itself recurse and fail inside the test before the request was ever sent.
    """
    depth, width = 5_000, 20_000
    body = '{"source_kind":"canonical_json","payload":' + "[" * depth + "1," * (width - 1) + "1" + "]" * depth + "}"
    assert len(body) < 64 * 1024, "the point is that a tiny body costs the server a great deal"

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        response = client.post(
            f"/api/reviews/{review}/imports",
            content=body,
            headers={**headers(), "Content-Type": "application/json"},
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    peak_mib = peak / (1024 * 1024)

    _assert_typed_refusal(response, "validation_failed", 422)
    assert ENVELOPE_GUARD_DETAIL in response.json()["detail"], (
        "the envelope reached the denylist walk before being refused for depth; the guard is "
        f"in the wrong order or missing. detail={response.json()['detail']!r}"
    )
    assert peak_mib < AMPLIFIER_BUDGET_MIB, (
        f"refusing a {len(body) // 1024} KiB envelope allocated {peak_mib:.0f} MiB; the depth guard "
        "is not running before the O(nodes x depth) denylist walk, and the pod limit is 512Mi"
    )


def test_the_envelope_accepts_exactly_what_the_raw_upload_accepts(client, review) -> None:
    """The `+ 1` in `ENVELOPE_DEPTH_BOUND` is an accounting fact, pinned from both sides.

    The envelope wraps the payload root in one more object, so a node the parser
    measures at depth D sits at D + 1 here. Bounding the envelope at
    `MAX_BODY_DEPTH` flat would refuse, through one URL shape, a payload the
    other URL shape accepts — the kind of difference nobody thinks to look for
    because both routes are "the import".

    So: the same payload, at the parser's exact limit, through both forms. Both
    must behave identically. Asserted at the boundary rather than at a safe depth,
    because an off-by-one is invisible anywhere else.
    """
    from infrastructure.imports.import_parser import MAX_JSON_DEPTH

    # `_audit_json` measures the payload root as depth 1. `{"records": [ {...} ]}`
    # puts a record's field values at depth 4, so the chain below tops out at
    # exactly MAX_JSON_DEPTH.
    chain = nested(MAX_JSON_DEPTH - 4)
    records = [{"title": "A boundary study", "doi": f"10.1000/boundary-{uuid.uuid4()}", "extra": chain}]

    raw = client.post(
        f"/api/reviews/{review}/imports?source_kind=canonical_json",
        content=json.dumps({"records": records}),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    enveloped = client.post(
        f"/api/reviews/{review}/imports",
        json={"source_kind": "canonical_json", "payload": {"records": records}},
        headers=headers(),
    )
    assert raw.status_code == enveloped.status_code, (
        f"the same payload was accepted by one import form and refused by the other: "
        f"raw={raw.status_code} {raw.text[:160]!r} envelope={enveloped.status_code} {enveloped.text[:160]!r}"
    )
    assert raw.status_code == 201, raw.text


def test_a_protocol_body_at_a_realistic_depth_still_creates_a_version(client, review) -> None:
    """The positive control, and it is load-bearing.

    A bound of 1 would satisfy every refusal above. This is what says the guard
    refuses only what nothing legitimate emits.
    """
    response = client.post(
        f"/api/reviews/{review}/protocol-versions",
        json={
            "inclusion_criteria": REALISTIC_PROTOCOL_CRITERIA,
            "exclusion_criteria": {"design": {"none_of": [{"term": "case report"}]}},
            "stage_definitions": [{"key": "ta", "name": "Title/abstract", "reviewers_required": 2}],
            "exclusion_reason_vocabulary": ["wrong_design"],
            "reviewers_required": 2,
        },
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["inclusion_criteria"] == REALISTIC_PROTOCOL_CRITERIA


def test_a_dedup_config_at_a_realistic_depth_still_starts_a_run(client, review) -> None:
    """The positive control for the config guard: `DEFAULT_CONFIG`'s own shape."""
    response = client.post(
        f"/api/reviews/{review}/deduplication-runs",
        json={"config": {"rules": ["normalized_doi", "normalized_pmid"]}},
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["config"]["rules"] == ["normalized_doi", "normalized_pmid"]
