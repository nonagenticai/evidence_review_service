"""`POST /exports` must seal the version the CORPUS IT CITES contains.

## The claim in this file changed once, and the first version was wrong

`925cacf` replaced an unordered dict comprehension with the max-per-group TAIL and
asserted that. It was right about the defect — last-row-wins by heap order — and
wrong about the replacement, which an adversarial pass caught as soon as
`apply_survivorship` made a second version reachable:

    export taken after a survivorship merge:  included version = v2
    the corpus the SAME manifest cites:       members          = v1

The artifact contradicted its own envelope, and `verify_export` answered
`verified` on it, because it re-hashes the stored payload and has no membership
check at all.

The manifest cites `corpus_version_id` and `member_manifest_hash` — it says "this
is the inclusion set drawn from corpus X". Screening is the deciding argument
rather than mere consistency: an assignment is minted from a corpus version and a
decision is taken against the record AS THE REVIEWER SAW IT, so naming any other
version credits a judgement to content nobody judged.

The original defect stays fixed. `corpus_member_versions` is one explicitly
scoped join, not a heap-order accident.

---

Original note, still true of the thing it was about:

Measured on `2ffdd5f`, `create_export` selected record versions like this:

    versions = {v.review_record_id: v for v in session.execute(
        select(ReviewRecordVersion).where(
            ReviewRecordVersion.review_id == review_id,
            ReviewRecordVersion.review_record_id.in_(included_ids or [uuid.uuid4()]),
        )).scalars().all()}

No `version_number` filter, no `ORDER BY`, and a dict comprehension — so the row
that wins is **whichever one Postgres returned last**. Three sibling reads in
`screening.py` get this right with `ORDER BY version_number DESC LIMIT 1`, and
`imports.current_record_version_ids` has carried the canonical max-per-group form
the whole time. The export used neither.

## Why this is asserted at the DBAPI and not on the response

A response-level test cannot make the old code fail, and I checked before writing
this file rather than assuming. `review_record_versions` is append-only — the
`review_record_versions_immutable` trigger refuses UPDATE and DELETE — so heap
order **is** insertion order, and the only usable index,
`ix_record_versions_record`, is `(review_record_id, version_number)` ascending
with no `ORDER BY` in the query to make the planner walk it backwards. Every plan
available returns v1 then v2, and last-row-wins therefore lands on the tail by
accident. Forcing plans with `enable_seqscan` / `enable_indexscan` does not
discriminate either; I considered it and it is not a gate.

So the observable that changed is the SQL, and a `before_cursor_execute` listener
counts exactly it — the same reasoning, and the same apparatus, as
`test_assignment_throughput.py`.

The consequence being pinned is not academic. The moment anything writes a second
version — which is what `apply_survivorship` is for, landing next — a sealed
export could carry a superseded `content_hash`, `title` and `doi`, and
`verify_export` re-hashes the **stored** payload, so it would report
`manifest_matches: true` on the wrong record content. The seal would certify its
own mistake.

## What each test in this file actually proves — measured, not assumed

Run against `2ffdd5f` with the source reverted and the tests kept, 2 failed and
2 passed:

* `test_the_export_selects_record_versions_the_corpus_sealed` — **FAILED**, on the
  unscoped `SELECT ... WHERE review_id = %s AND review_record_id IN (...)`. This
  is the gate. It was `..._by_tail_not_by_record` in `925cacf`; the assertion
  moved from "carries max(version_number)" to "joins corpus_version_members" when
  the claim was corrected, and it still fails on `2ffdd5f` for the same reason.
* `test_an_included_record_with_no_readable_version_is_refused_not_dropped` —
  FAILED, but only with `AttributeError: no attribute 'current_record_versions'`.
  That is a structural failure: it proves the refactor did not happen, not that
  the old code dropped a row. Weaker evidence than the first, and recorded as
  such rather than counted as a second gate.
* `test_the_export_seals_the_superseding_version_not_the_superseded_one` —
  **PASSED on the broken code.** See its docstring; it is a regression guard.
* `test_a_previously_sealed_export_still_verifies_after_a_new_version_appears` —
  passed both, by design. It pins what must NOT change.

## The second defect in the same six lines

`included_rows` was built with a trailing `if rid in versions`. An included record
whose version did not come back was silently dropped from the sealed payload;
`manifest.included_count` is `len(included_payload)`, so the count shrank to
match; the record appeared in neither `included` nor `omissions`; and
`verify_export` verified the truncated set. That guard is gone and a missing
version is now a refusal.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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

TENANT = tenant("tenant-export-tail")
LEAD = "export-tail-lead"
REVIEWER = "export-tail-reviewer"

CORPUS = 6


def headers(subject: str = LEAD, role: str = "lead") -> dict[str, str]:
    return {"X-Tenant-Id": TENANT, "X-Subject-Id": subject, "X-Actor-Role": role}


@dataclass
class _Capture:
    """Every statement the engine emitted, and the record-version reads among them.

    Both lists are kept for the same reason `test_assignment_throughput.py` keeps
    both: an empty `all_statements` means the listener never attached, while a
    populated `all_statements` with an empty `version_reads` means the filter
    stopped matching. The bounds below cannot tell those apart from success on
    their own — `0` satisfies every one of them.
    """

    all_statements: list[str] = field(default_factory=list)
    version_reads: list[str] = field(default_factory=list)

    def assert_the_read_was_observed(self, context: str) -> None:
        """The non-vacuity floor. Call it before asserting anything about the reads."""
        assert self.all_statements, (
            f"{context}: the before_cursor_execute listener recorded no SQL AT ALL. That is a "
            "broken capture, not a cheap route — the hook is on an engine the request did not "
            "use, or the request never reached the database."
        )
        assert self.version_reads, (
            f"{context}: {len(self.all_statements)} statements were emitted and NOT ONE selected "
            "FROM review_record_versions. The filter no longer recognises the read this file "
            "exists to inspect, so every assertion below would pass against an empty list."
        )


@contextmanager
def counting_version_reads() -> Iterator[_Capture]:
    """Every `SELECT ... FROM review_record_versions` issued inside the block.

    Attached to the engine, not a session: the route opens its own `actor_scope`
    and there is no session to hand it. `get_engine` is a module-level singleton,
    which is what makes that reachable from here.
    """
    from sqlalchemy import event

    from database import get_engine

    engine = get_engine()
    capture = _Capture()

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202, ARG001
        capture.all_statements.append(statement)
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith("SELECT") and "FROM REVIEW_RECORD_VERSIONS" in normalized:
            capture.version_reads.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield capture
    finally:
        event.remove(engine, "before_cursor_execute", _record)


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
def screened(client):  # noqa: ANN001, ANN201
    """A review screened to completion — one `POST /exports` away, not yet exported."""
    review_id = client.post(
        "/api/reviews",
        json={"project_id": "export-tail", "external_key": f"tail-{uuid.uuid4()}", "title": "Export tail"},
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
    imported = client.post(
        f"/api/reviews/{review_id}/imports?source_kind=canonical_json",
        content=json.dumps(
            {
                "records": [
                    {"title": f"Tail paper {n}", "year": 2020 + n, "doi": f"10.1000/tail.{uuid.uuid4()}"}
                    for n in range(CORPUS)
                ]
            }
        ),
        headers={**headers(), "Content-Type": "application/octet-stream"},
    )
    assert imported.status_code in (200, 201), imported.text
    client.post(f"/api/reviews/{review_id}/corpus-versions", headers=headers())

    stage_id = client.get(f"/api/reviews/{review_id}/stages", headers=headers()).json()["items"][0]["stage_id"]
    client.post(
        f"/api/reviews/{review_id}/assignments",
        json={"stage_id": stage_id, "reviewer_subject_ids": [REVIEWER]},
        headers=headers(),
    )
    queue = client.get(
        f"/api/reviews/{review_id}/screening/queue",
        params={"stage_id": stage_id, "limit": 200},
        headers=headers(REVIEWER, "reviewer"),
    ).json()
    assert len(queue["items"]) == CORPUS, queue
    for item in queue["items"]:
        decided = client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"stage_id": stage_id, "review_record_id": item["review_record_id"], "disposition": "include"},
            headers=headers(REVIEWER, "reviewer"),
        )
        assert decided.status_code == 201, decided.text

    return {"review_id": review_id, "stage_id": stage_id}


def _record_version_rows(review_id: str) -> list[dict]:
    """Read the version rows straight out of the database, as the owner."""
    import sqlalchemy as sa

    migration_url = os.environ.get("MIGRATION_DATABASE_URL") or DB_URL
    engine = sa.create_engine(migration_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            # `review_record_versions` is `FORCE ROW LEVEL SECURITY`, so the
            # tenant policy applies to the OWNER too. This helper read as the
            # owner with no `app.current_tenant` set, which returns zero rows —
            # and every caller then fails with "fixture drift", pointing at the
            # fixture rather than at the read.
            #
            # It has always passed in CI because the archetype's ephemeral
            # database runs as `ci_test`, the container SUPERUSER, and a
            # superuser bypasses RLS entirely — including FORCE. So this file's
            # RLS-sensitive reads have never actually been exercised against the
            # role split the service runs under in production, where the app
            # connects as a non-owner precisely so the policies cannot be
            # removed. Setting the GUC is what makes the test mean the same
            # thing under both.
            conn.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": TENANT})
            rows = conn.execute(
                sa.text(
                    "SELECT review_record_version_id, review_record_id, version_number, title, "
                    "content_hash, tenant_id, review_id, record_type, schema_ref, field_provenance, "
                    "source_import_row_id, created_by_subject_id "
                    "FROM review_record_versions WHERE review_id = :rid "
                    "ORDER BY review_record_id, version_number"
                ),
                {"rid": review_id},
            ).mappings()
            return [dict(r) for r in rows]
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# The gate. This is the assertion that fails on `2ffdd5f`.
# --------------------------------------------------------------------------


def test_the_export_selects_record_versions_the_corpus_sealed(client, screened) -> None:
    """Every record-version read during an export must be corpus-scoped.

    Three spellings are acceptable and all appear in this codebase: join
    `corpus_version_members`, which stores the exact version frozen at seal time;
    name the version ids explicitly; or carry the max-per-group aggregate, for
    the callers that genuinely want the review's present state. The original
    query did none of them — it filtered on `review_id` and `review_record_id`
    alone and took whatever came back last.

    This is the assertion that discriminates. On `2ffdd5f` the export emits a
    `SELECT ... FROM review_record_versions WHERE review_id = %s AND
    review_record_id IN (...)` with no `MAX(` and no version-id predicate, and
    this test fails on it.
    """
    with counting_version_reads() as capture:
        created = client.post(f"/api/reviews/{screened['review_id']}/exports", headers=headers())
    assert created.status_code == 201, created.text
    capture.assert_the_read_was_observed("export")

    unscoped = [
        stmt
        for stmt in capture.version_reads
        if "MAX(" not in stmt.upper()
        and "REVIEW_RECORD_VERSION_ID IN" not in stmt.upper()
        and "CORPUS_VERSION_MEMBERS" not in stmt.upper()
        # `_collect` reads only the id column to build the record-id set; it
        # cannot pick a wrong version because it never reads one.
        and "SELECT REVIEW_RECORD_VERSIONS.REVIEW_RECORD_ID FROM" not in " ".join(stmt.split()).upper()
    ]
    assert not unscoped, (
        "the export read record versions without scoping them to the corpus it cites. Each of "
        "these can return more than one row per record, and the caller keeps whichever arrived "
        "last:\n  " + "\n  ".join(unscoped)
    )


def test_a_version_outside_the_cited_corpus_is_never_sealed(client, screened) -> None:
    """**This assertion was inverted.** `925cacf` demanded the opposite.

    It read `sealed_row["review_record_version_id"] == str(superseding_id)` and
    called sealing v1 "the defect". That was wrong: a v2 which is not a member of
    the corpus the manifest cites has no business in that corpus's inclusion set,
    and putting it there is what made the sealed artifact contradict its own
    envelope while `verify_export` still answered `verified`.

    The v2 here is constructed directly — an INSERT is what the immutability
    trigger permits and `uq_record_version_number` accepts a `2` — and it is
    NEVER added to the sealed corpus, which is exactly the state a survivorship
    merge produces against an already-sealed corpus.

    A separate end-to-end case, `test_survivorship_does_not_move_a_sealed_export`,
    drives the same thing through the real merge path.
    """
    import sqlalchemy as sa

    review_id = screened["review_id"]
    rows = _record_version_rows(review_id)
    assert len(rows) == CORPUS, "fixture drift: expected exactly one version per record before this test"

    original = rows[0]
    migration_url = os.environ.get("MIGRATION_DATABASE_URL") or DB_URL
    engine = sa.create_engine(migration_url, future=True, connect_args={"prepare_threshold": 0})
    superseding_id = uuid.uuid4()
    try:
        with engine.begin() as conn:
            # Same reason as the read helper above, on the WITH CHECK side: the
            # tenant policy refuses an INSERT whose `tenant_id` does not match
            # the GUC, and unset is not a match. Under CI's superuser this line
            # is a no-op; under the production-shaped role split it is what lets
            # the write happen at all.
            conn.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": TENANT})
            conn.execute(
                sa.text(
                    "INSERT INTO review_record_versions ("
                    "  review_record_version_id, review_record_id, review_id, tenant_id, version_number,"
                    "  supersedes_version_id, schema_ref, content_hash, record_type, title,"
                    "  field_provenance, source_import_row_id, created_by_subject_id"
                    ") VALUES ("
                    "  :new_id, :record_id, :review_id, :tenant_id, 2,"
                    "  :supersedes, :schema_ref, :content_hash, :record_type, :title,"
                    "  :provenance, :source_row, :subject)"
                ),
                {
                    "new_id": superseding_id,
                    "record_id": original["review_record_id"],
                    "review_id": original["review_id"],
                    "tenant_id": original["tenant_id"],
                    "supersedes": original["review_record_version_id"],
                    "schema_ref": original["schema_ref"],
                    "content_hash": "sha256:" + "b" * 64,
                    "record_type": original["record_type"],
                    "title": "SUPERSEDING TITLE — v2",
                    "provenance": json.dumps(original["field_provenance"] or {}),
                    "source_row": original["source_import_row_id"],
                    "subject": LEAD,
                },
            )
    finally:
        engine.dispose()

    with counting_version_reads() as capture:
        created = client.post(f"/api/reviews/{review_id}/exports", headers=headers())
    assert created.status_code == 201, created.text
    capture.assert_the_read_was_observed("export with a superseded version present")

    payload = client.get(f"/api/reviews/{review_id}/exports/{created.json()['export_id']}", headers=headers()).json()
    included = payload["payload"]["included"]
    assert len(included) == CORPUS, (
        f"the export carried {len(included)} included rows for {CORPUS} records — a second version "
        "was counted as a second record, or an included record was dropped"
    )
    by_record = {row["review_record_id"]: row for row in included}
    sealed_row = by_record[str(original["review_record_id"])]
    assert sealed_row["review_record_version_id"] == str(original["review_record_version_id"]), (
        "the export sealed a version the corpus it cites does not contain. The manifest names "
        "a corpus whose members are all v1, while `included` carries v2 — and "
        "`verify_export` re-hashes the stored payload, so it reports `verified` on the "
        "contradiction."
    )
    assert sealed_row["title"] != "SUPERSEDING TITLE — v2"
    assert sealed_row["content_hash"] != "sha256:" + "b" * 64

    # The non-vacuity floor: the v2 really is in the database, so this test is
    # not passing because the INSERT quietly failed.
    assert len(_record_version_rows(review_id)) == CORPUS + 1, (
        "the superseding version was not written, so nothing was actually excluded"
    )
    assert superseding_id


def test_a_previously_sealed_export_still_verifies_after_a_new_version_appears(client, screened) -> None:
    """An export sealed before a v2 existed must keep verifying against what it said.

    `verify_export` re-hashes the STORED payload, deliberately. A record gaining a
    later version does not retroactively falsify an export that named the earlier
    one — the export is a statement about a moment. If this ever started failing,
    the seal would have become a live query rather than a seal.
    """
    review_id = screened["review_id"]
    exports = client.get(f"/api/reviews/{review_id}/exports", headers=headers()).json()["items"]
    assert exports, "fixture drift: the earlier tests in this module should have created exports"

    for export in exports:
        verified = client.get(f"/api/reviews/{review_id}/exports/{export['export_id']}:verify", headers=headers())
        assert verified.status_code == 200, verified.text
        body = verified.json()
        assert body["outcome"] == "verified", f"export {export['export_id']} no longer verifies: {body}"


def test_an_included_record_with_no_readable_version_is_refused_not_dropped(client, screened) -> None:
    """The silent-omission path is a refusal, and the refusal names the records.

    Driven at the seam rather than through HTTP, because there is no HTTP route
    that can produce an included record with no version row — the immutability
    trigger and the FK see to that. The reachable-in-production question is not
    the point; the point is which direction the code fails in when it is wrong,
    and the old direction was to shrink the manifest to match and verify the
    remainder.
    """
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from api.problem_details import UnprocessableStateError
    from application.services import flow as flow_module

    review_id = screened["review_id"]
    real = flow_module.corpus_member_versions

    def _drop_one(session, corpus_version_id, record_ids):  # noqa: ANN001, ANN202
        found = real(session, corpus_version_id, record_ids)
        found.pop(next(iter(found)), None)
        return found

    from actor import Actor
    from database import actor_scope

    flow_module.corpus_member_versions = _drop_one
    try:
        actor = Actor(tenant_id=TENANT, subject_id=LEAD, role="lead")
        with actor_scope(actor) as session:
            with pytest.raises(UnprocessableStateError) as caught:
                flow_module.create_export(session, actor, uuid.UUID(review_id))
    finally:
        flow_module.corpus_member_versions = real

    assert "not members of corpus version" in str(caught.value)
    assert caught.value.extra.get("missing_record_ids"), (
        "the refusal must name the records it refused over — an unnamed count is not actionable, "
        "and naming them is what distinguishes this from the old silent drop"
    )
