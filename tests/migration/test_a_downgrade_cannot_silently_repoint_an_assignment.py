"""`0009`'s round trip re-pointed an assignment at a corpus nobody chose.

`0009.upgrade` backfills `screening_assignments.corpus_version_id` as *"the
newest corpus version of this review that already existed when the assignment
was written"*. That is a sound RECONSTRUCTION for rows written before the column
existed. It is **not the same fact** as the column: `create_assignments` takes an
explicit `corpus_version_id` from the caller, so a lead may deliberately assign
against an older corpus while a newer one exists.

Measured through the real API on a database at head, a review holding v1 and v2
with the assignment minted explicitly against v1, then `downgrade 0008` and
`upgrade head`:

    BEFORE  96ffb76c-…   (v1, as the lead chose)
    AFTER   a799f0a0-…   (v2, as the backfill guessed)

Since `ER-M009` that column decides which version the screening queue serves, and
since the decision-version fix it decides which version a recorded judgement
cites. So the round trip does not lose an audit detail — it changes what a
reviewer is shown and what their decision is attributed to, silently, leaving a
value that looks deliberate.

The column is dropped by the downgrade, so the fact is unrecoverable and there is
nothing to repair afterwards. `downgrade` therefore refuses.

## The guard had to un-FORCE RLS, or it would have checked nothing

This file's migration documents at length that a migration session sets no
`app.current_tenant`, so a count run under `FORCE ROW LEVEL SECURITY` matches
nothing and reports zero. A guard written the obvious way would have reported
"0 mismatched" on every database and let the downgrade through — the exact hole
`0009.upgrade` was written to describe. Both tables are un-FORCEd, because the
subquery reads `corpus_versions` too, and `total` is reported beside `mismatched`
so "0 because nothing was visible" cannot present as "0 because everything
agreed". The refusal below asserts on the ratio for that reason.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.migration.test_migration_round_trip import (  # noqa: F401 - fixture re-export
    _alembic,
    scratch_database,
)

REPO = Path(__file__).resolve().parents[2]
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not OWNER_URL.startswith("postgresql"),
        reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
    ),
]

#: The scratch database's tenant. It used to be the readable label
#: `"tenant-downgrade-guard"`, which said in one word what this database exists
#: for. `0014` made all 25 `tenant_id` columns `uuid`, so postgres now refuses
#: that literal itself — measured, on the first `SEED` insert:
#: `psycopg.errors.InvalidTextRepresentation: invalid input syntax for type
#: uuid: "tenant-downgrade-guard"`. The label survives in this comment because
#: it cannot survive in the value.
#:
#: Nothing downstream reads it as a name: the tenant is only ever a scoping key
#: here — `set_config('app.current_tenant', …)` plus the four `tenant_id`
#: columns the assignment's foreign keys demand — and no assertion in this file
#: mentions it. So the change of spelling costs the test nothing.
_GUARD_TENANT = uuid.UUID("0195f48b-d000-7000-8000-00000000d014")

#: Bound as `str`, not as the `UUID`, and deliberately. `:tenant` is reused by
#: `set_config(text, text, bool)` in the same `SEED` script as by the `uuid`
#: columns; psycopg would adapt a `uuid.UUID` with the `uuid` OID and postgres
#: has no `set_config(text, uuid, bool)` to resolve it to. A uuid-shaped string
#: casts cleanly in both places, which is how every other id in `ids` is bound.
TENANT = str(_GUARD_TENANT)

#: Both corpus versions carry the sha256 of the EMPTY member list, because a
#: deferred trigger recomputes the manifest from `corpus_version_members` at
#: COMMIT and refuses a mismatch — `ER-V011` as a constraint rather than a test.
#: Seeding zero members and a made-up hash is refused, correctly, which is how
#: this value was arrived at rather than guessed.
#:
#: The minimum graph the assignment's four foreign keys demand. Written as SQL
#: rather than driven through the API because the API is bound to the suite's
#: shared database and this has to happen inside a scratch one — `downgrade`
#: here is destructive, and the round-trip fixture exists precisely so that a
#: destructive migration test cannot reach the shared schema.
SEED = """
SELECT set_config('app.current_tenant', :tenant, false);

INSERT INTO reviews (review_id, tenant_id, project_id, external_key, title, created_by_subject_id)
VALUES (:review, :tenant, 'p', :key, 'Downgrade guard', 'lead');

INSERT INTO review_protocol_versions
  (protocol_version_id, review_id, tenant_id, version_number, schema_ref, content_hash,
   inclusion_criteria, exclusion_criteria, stage_definitions, exclusion_reason_vocabulary,
   reviewers_required, created_by_subject_id)
VALUES (:protocol, :review, :tenant, 1, 'urn:test', 'sha256:' || repeat('0', 64), '{}'::jsonb, '{}'::jsonb,
        '[]'::jsonb, '[]'::jsonb, 1, 'lead');

INSERT INTO review_stages
  (stage_id, review_id, tenant_id, protocol_version_id, stage_key, stage_order, name, reviewers_required)
VALUES (:stage, :review, :tenant, :protocol, 'ta', 1, 'TA', 1);

INSERT INTO review_records (review_record_id, review_id, tenant_id)
VALUES (:record, :review, :tenant);

INSERT INTO corpus_versions
  (corpus_version_id, review_id, tenant_id, version_number, member_count, member_ordering_rule,
   member_manifest_hash, created_by_subject_id, created_at)
VALUES (:v1, :review, :tenant, 1, 0, 'record_version_id_asc', 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'lead', now() - interval '2 hours'),
       (:v2, :review, :tenant, 2, 0, 'record_version_id_asc', 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'lead', now() - interval '1 hour');
"""

ASSIGN = """
INSERT INTO screening_assignments
  (assignment_id, review_id, tenant_id, stage_id, round_number, review_record_id,
   reviewer_subject_id, assigned_by_subject_id, assigned_at, corpus_version_id)
VALUES (:assignment, :review, :tenant, :stage, 1, :record, 'rv', 'lead', now(), :corpus);
"""


@pytest.fixture()
def seeded(scratch_database: str) -> Iterator[tuple[str, dict]]:  # noqa: F811
    """A scratch database at head, holding one review with two corpus versions."""
    _alembic("upgrade", "head", scratch_database)
    ids = {
        "tenant": TENANT,
        "review": str(uuid.uuid4()),
        "key": f"k-{uuid.uuid4()}",
        "protocol": str(uuid.uuid4()),
        "stage": str(uuid.uuid4()),
        "record": str(uuid.uuid4()),
        "v1": str(uuid.uuid4()),
        "v2": str(uuid.uuid4()),
    }
    engine = create_engine(scratch_database, future=True)
    with engine.begin() as conn:
        for statement in SEED.strip().split(";\n"):
            if statement.strip():
                conn.execute(text(statement), ids)
    engine.dispose()
    yield scratch_database, ids


def _assign(url: str, ids: dict, corpus: str) -> None:
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :tenant, false)"), ids)
        conn.execute(text(ASSIGN), {**ids, "assignment": str(uuid.uuid4()), "corpus": corpus})
    engine.dispose()


def _downgrade(url: str):  # noqa: ANN202
    """The same subprocess shape `_alembic` uses, but WITHOUT its success assert.

    This test is about a downgrade that must FAIL, so it needs the result rather
    than a helper that raises on a non-zero exit.
    """
    import subprocess
    import sys

    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", "downgrade", "0008_er_m008"],
        cwd=str(REPO),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 - alembic only wants a writable HOME
            "MIGRATION_DATABASE_URL": url,
            "DATABASE_URL": url,
            "ENABLE_AUTH": "false",
            "ENVIRONMENT": "test",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_deliberately_older_corpus_refuses_the_downgrade(seeded) -> None:  # noqa: ANN001
    """THE GUARD. The lead chose v1 while v2 existed; that cannot be reconstructed."""
    url, ids = seeded
    _assign(url, ids, ids["v1"])

    result = _downgrade(url)
    assert result.returncode != 0, (
        "the downgrade dropped a column holding a choice its own upgrade would not reconstruct, "
        "so re-upgrading silently re-points the reviewer:\n" + result.stdout
    )
    combined = result.stdout + result.stderr
    assert "refusing to drop" in combined, combined[-2000:]
    assert "1 of 1" in combined, (
        "the guard did not report a ratio, so a count that saw NO rows would read the same as one "
        "that saw them all — which is the RLS trap this migration documents: " + combined[-2000:]
    )


def test_the_reconstructable_case_still_downgrades(seeded) -> None:  # noqa: ANN001
    """NEGATIVE CONTROL. Refusing every downgrade would pass the test above.

    The assignment names the newest corpus that existed when it was written,
    which is exactly what the backfill would produce, so nothing is lost and the
    downgrade must proceed.
    """
    url, ids = seeded
    _assign(url, ids, ids["v2"])

    result = _downgrade(url)
    assert result.returncode == 0, (
        "the assignment names the corpus the backfill would reconstruct, so nothing is lost and the "
        "downgrade must proceed:\n" + result.stdout + result.stderr
    )


def test_an_empty_table_downgrades(seeded) -> None:  # noqa: ANN001
    """NEGATIVE CONTROL. No assignments at all is not a mismatch.

    This is the shape production is in, so a guard that refused here would block
    a downgrade on every database that has never screened anything.
    """
    url, _ = seeded
    result = _downgrade(url)
    assert result.returncode == 0, result.stdout + result.stderr
