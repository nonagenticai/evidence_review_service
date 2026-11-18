"""Every `supersedes_*` column is either scope-guarded or listed as not. Ratcheted.

## The defect this generalises from

`ER-M021` shipped `evidence_snapshots.supersedes_snapshot_id` with the two
partial unique indexes `0018` uses — one root per review, one successor per
node — and a docstring claiming `0018`'s idiom "verbatim". It was not verbatim.
`0018` backs those indexes with a TRIGGER, and without it neither index says the
successor is in the same review.

A row in review R2 superseding R1's tail satisfied both indexes, left R2 with no
root at all, and `snapshots._entity_of` — which walks the chain without
filtering by review — then handed R2's chain R1's root as its `entity_id`. That
is precisely the identity collision `entity_id` exists to prevent, arriving by
the one route the indexes do not cover.

An adversarial review found it. Nothing in the suite would have.

## Why the whole schema and not just that table

Because the same reasoning applies to every supersession chain here, and
measuring showed the gap is wider than the table that prompted it: twelve
columns, four guarded. The eight unguarded ones belong to five other work
packages and their fix is a migration each — not a side effect of `ER-007`.

So this is a ratchet, not an assertion. A NEW supersession column that is
neither guarded nor consciously exempted fails immediately, which is the whole
point: the next one of these must not reach a reviewer.

⚠️ Trigger NAMES prove nothing. An earlier version of this analysis matched
trigger function bodies for the word "supersedes" and got a false positive on
`screening_decisions_append_only`, which mentions it only in an error message:
"a correction supersedes; a decision is never overwritten". `GUARDED` therefore
names the exact trigger, and `test_every_guard_is_a_trigger_that_exists` checks
it is installed rather than trusting the map.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

#: `table -> the trigger that keeps its chain inside one parent`. Each was read,
#: not inferred from its name.
GUARDED: dict[str, str] = {
    "duplicate_resolutions": "trg_duplicate_resolutions_same_cluster",
    "evidence_acceptances": "trg_evidence_acceptances_witnesses_its_subject",
    "evidence_snapshots": "trg_evidence_snapshots_supersede_within_one_review",
    "research_object_versions": "trg_research_object_versions_supersession_scope",
}

#: `table -> the package that owes the guard`. Measured 2026-09-03: each of
#: these has a `supersedes_*` column and NO trigger that constrains the
#: predecessor to the same parent scope, so a chain can in principle cross one.
#:
#: No service path constructs such a row today — every writer sets
#: `supersedes_*` from a row it just read in the same review. That is why these
#: are a ratchet and not an outage. It is also exactly what was true of
#: `evidence_snapshots` before a reviewer looked.
UNGUARDED: dict[str, str] = {
    "corpus_versions": "ER-002",
    "extraction_dataset_versions": "ER-006",
    "observation_versions": "ER-006",
    "research_assertion_versions": "ER-006",
    "review_protocol_versions": "ER-001",
    "review_record_versions": "ER-002",
    "screening_adjudications": "ER-004",
    "screening_decisions": "ER-004",
}


def _engine():  # noqa: ANN202
    return create_engine(DB_URL)


def _supersession_tables() -> set[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                " WHERE table_schema = 'public' AND column_name LIKE 'supersedes%'"
            )
        ).scalars()
        result = set(rows)
    engine.dispose()
    return result


def _triggers_on(table: str) -> set[str]:
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tgname FROM pg_trigger  WHERE NOT tgisinternal AND tgrelid = CAST(:t AS regclass)"),
            {"t": table},
        ).scalars()
        result = set(rows)
    engine.dispose()
    return result


def test_every_supersession_table_has_been_classified() -> None:
    """A new chain column must be guarded or consciously exempted. Both directions.

    Failing here means somebody added a supersession column and did not decide
    whether its chain may cross its parent. That decision is cheap now and
    expensive after a consumer keys on the chain's root — which is what
    `entity_id` does.
    """
    measured = _supersession_tables()
    classified = set(GUARDED) | set(UNGUARDED)

    unclassified = measured - classified
    assert unclassified == set(), (
        f"{sorted(unclassified)} have a `supersedes_*` column and are in neither GUARDED nor "
        "UNGUARDED. Add a trigger keeping the chain inside one parent (see "
        "`evidence_snapshots_supersede_within_one_review` in ER-M021), or record the owing "
        "package in UNGUARDED and say why it is safe today."
    )

    stale = classified - measured
    assert stale == set(), (
        f"{sorted(stale)} are classified here but have no `supersedes_*` column. A stale "
        "exemption silently covers a future table of the same name."
    )


def test_every_guard_is_a_trigger_that_exists() -> None:
    """The map names real, installed triggers — not hopeful ones.

    A guard listed here and absent from the database is worse than no entry: it
    reads as protection, and the table it names is the one nobody will re-check.
    """
    missing = {table: trigger for table, trigger in GUARDED.items() if trigger not in _triggers_on(table)}
    assert missing == {}, f"guards named here are not installed: {missing}"


#: The exact set measured on 2026-09-03. Frozen by IDENTITY, not by size.
#:
#: ⚠️ This was `len(UNGUARDED) <= 8` and a review showed the promise was empty:
#: guard `screening_decisions`, move it to `GUARDED`, and add a brand-new
#: unguarded column in its place — the count is still 8, every other assertion
#: still passes, and a new exemption has joined a list whose whole purpose is
#: that nothing joins it. A ratchet that counts is not a ratchet, it is a
#: quota.
_UNGUARDED_AT_MEASUREMENT: frozenset[str] = frozenset(
    {
        "corpus_versions",
        "extraction_dataset_versions",
        "observation_versions",
        "research_assertion_versions",
        "review_protocol_versions",
        "review_record_versions",
        "screening_adjudications",
        "screening_decisions",
    }
)


def test_the_unguarded_set_only_shrinks() -> None:
    """The ratchet. A table may leave UNGUARDED; none may join it — by NAME.

    Joining would mean a guard was deleted, which no migration should do, or a
    new column was added and exempted — which
    `test_every_supersession_table_has_been_classified` already forces somebody
    to argue for rather than default into.
    """
    for table in UNGUARDED:
        assert table not in GUARDED, f"{table} is in both sets; it cannot be"

    joined = set(UNGUARDED) - _UNGUARDED_AT_MEASUREMENT
    assert joined == set(), (
        f"{sorted(joined)} JOINED the unguarded list, which is one-way. Guard the chain "
        "instead (see `evidence_snapshots_supersede_within_one_review` in ER-M021). If a "
        "table genuinely cannot be guarded, that is a decision to argue in review — say so "
        "here and move it into `_UNGUARDED_AT_MEASUREMENT` deliberately."
    )


def test_the_guard_this_generalises_from_actually_refuses() -> None:
    """The positive control, and it is not optional.

    Every assertion above compares catalogue metadata against a hand-written
    map. All of them would pass against a trigger that was installed and did
    nothing — the shape `a-ratchet-must-floor-on-offenders-found` and
    `a-verification-that-cannot-go-red` both describe.

    So one guard is exercised for real: a snapshot in one review is told to
    supersede a snapshot in another, directly against the database, and the
    trigger must refuse it.
    """
    import uuid

    tenant = uuid.uuid4()
    engine = _engine()
    # ⚠️ The rows this writes are NEVER cleaned up, and cannot be: every table
    # it touches is append-only with a no-delete trigger, which is the property
    # under test. They are bounded and collision-free instead — a fresh
    # `uuid4()` tenant and `external_key` per run — so a re-run never sees the
    # last one. On CI's ephemeral `postgres:18` that is free; on a long-lived
    # dev database it accumulates a handful of rows per run, by design.
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        conn.execute(text("SELECT set_config('app.actor_subject_id', 'guard-probe', true)"))
        conn.execute(text("SELECT set_config('app.actor_role', 'lead', true)"))

        reviews = []
        for _ in range(2):
            rid = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO reviews (review_id, tenant_id, project_id, external_key, title,"
                    " created_by_subject_id) VALUES (:r, :t, :p, :k, 'guard probe', 'guard-probe')"
                ),
                {"r": rid, "t": tenant, "p": uuid.uuid4(), "k": f"guard-{uuid.uuid4()}"},
            )
            reviews.append(rid)

        # A protocol version and a job, because `evidence_snapshots` requires both.
        protocol = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO review_protocol_versions (protocol_version_id, review_id, tenant_id,"
                " version_number, schema_ref, content_hash, inclusion_criteria, exclusion_criteria,"
                " stage_definitions, exclusion_reason_vocabulary, reviewers_required,"
                " created_by_subject_id) VALUES (:pv, :r, :t, 1, 'x', :h, '[]'::jsonb, '[]'::jsonb,"
                " '[]'::jsonb, '[]'::jsonb, 1, 'guard-probe')"
            ),
            {"pv": protocol, "r": reviews[0], "t": tenant, "h": "sha256:" + "a" * 64},
        )
        job = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO durable_jobs (job_id, tenant_id, job_type, review_id, command_hash)"
                " VALUES (:j, :t, 'evidence_snapshot', :r, :h)"
            ),
            {"j": job, "t": tenant, "r": reviews[0], "h": "sha256:" + "b" * 64},
        )

        root = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO evidence_snapshots (snapshot_id, tenant_id, review_id, version_number,"
                " schema_ref, intended_use, protocol_version_id, member_count,"
                " member_manifest_hash, member_ordering_rule, content_hash, gates_evaluated,"
                " job_id, created_by_subject_id)"
                " VALUES (:s, :t, :r, 1, 'urn:datacompany:evidence-snapshot-manifest:1', 'probe', :pv,"
                " 0, :h, 'pinned_ref_key_asc', :h, '{}'::text[], :j, 'guard-probe')"
            ),
            {
                "s": root,
                "t": tenant,
                "r": reviews[0],
                "pv": protocol,
                "h": "sha256:" + "c" * 64,
                "j": job,
            },
        )

    # The control: the SAME insert into the SAME review succeeds, so the refusal
    # below is about crossing reviews and not about a malformed row.
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        job2 = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO durable_jobs (job_id, tenant_id, job_type, review_id, command_hash)"
                " VALUES (:j, :t, 'evidence_snapshot', :r, :h)"
            ),
            {"j": job2, "t": tenant, "r": reviews[0], "h": "sha256:" + "d" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO evidence_snapshots (snapshot_id, tenant_id, review_id, version_number,"
                " supersedes_snapshot_id, schema_ref, intended_use, protocol_version_id,"
                " member_count, member_manifest_hash, member_ordering_rule, content_hash,"
                " gates_evaluated, job_id, created_by_subject_id)"
                " VALUES (:s, :t, :r, 2, :prev, 'urn:datacompany:evidence-snapshot-manifest:1',"
                " 'probe', :pv, 0, :h, 'pinned_ref_key_asc', :h, '{}'::text[], :j, 'guard-probe')"
            ),
            {
                "s": uuid.uuid4(),
                "t": tenant,
                "r": reviews[0],
                "prev": root,
                "pv": protocol,
                "h": "sha256:" + "e" * 64,
                "j": job2,
            },
        )

    # And the refusal.
    with pytest.raises(Exception) as excinfo, engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        job3 = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO durable_jobs (job_id, tenant_id, job_type, review_id, command_hash)"
                " VALUES (:j, :t, 'evidence_snapshot', :r, :h)"
            ),
            {"j": job3, "t": tenant, "r": reviews[1], "h": "sha256:" + "f" * 64},
        )
        conn.execute(
            text(
                "INSERT INTO evidence_snapshots (snapshot_id, tenant_id, review_id, version_number,"
                " supersedes_snapshot_id, schema_ref, intended_use, protocol_version_id,"
                " member_count, member_manifest_hash, member_ordering_rule, content_hash,"
                " gates_evaluated, job_id, created_by_subject_id)"
                " VALUES (:s, :t, :other, 2, :prev, 'urn:datacompany:evidence-snapshot-manifest:1',"
                " 'probe', :pv, 0, :h, 'pinned_ref_key_asc', :h, '{}'::text[], :j, 'guard-probe')"
            ),
            {
                "s": uuid.uuid4(),
                "t": tenant,
                "other": reviews[1],
                "prev": root,
                "pv": protocol,
                "h": "sha256:" + "0" * 64,
                "j": job3,
            },
        )
    assert "one review" in str(excinfo.value), str(excinfo.value)
    engine.dispose()
