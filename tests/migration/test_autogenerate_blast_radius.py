"""`alembic revision --autogenerate` IS destructive here. This pins how much.

Autogenerate output in this repository MUST NOT be applied without hand-review.
Run against a correctly migrated database it proposes, right now:

    remove_fk 60, remove_constraint 52, remove_index 26, remove_table 1

(Re-measured at `0012_er_m012_replay_keys`: `remove_index` moved 27 -> 26 and
nothing else moved. That migration adds three UNIQUE constraints and drops one
index, and the count fell by exactly one rather than rising by three, because the
three constraints ARE expressed in `Base.metadata` — so autogenerate has nothing
to propose about them. It read `remove_index 27` at `0010`, which added one index
and no constraint, column or key, and `remove_fk 59 … remove_index 25` at `0008`,
where `0009`'s FK and index were the two that moved. These counts are prose — the
assertions below compare SETS, so a stale number here never turns the gate red and
has to be re-measured by hand when the schema grows.)

Applying that file drops every CHECK constraint and every partial unique index in
the schema — measured at `0008` on a real database: `checks 43 -> 0`,
`fks 59 -> 24`, `indexes 67 -> 33`. Those three are damage-after-applying figures
and are NOT re-measured here, because measuring them means applying the loaded
gun; they are left at the revision they were taken on rather than being adjusted
by arithmetic. On the damaged schema a single INSERT then writes TWO current
decisions for the same reviewer on the same record with disposition `'BANANA'`,
because `ck_decision_disposition` and `uq_decision_one_current_per_reviewer` are
both gone. Reviewer blinding and append-only history stop being enforced.

This is not a bug in alembic. The models in this service are deliberately typed
accessors: the integrity rules live in `alembic/versions/` as raw DDL and have no
ORM expression, so alembic — comparing the database against `Base.metadata` —
correctly reports them as "in the database, not in the models" and correctly
proposes removing them. The tool is right. The output is still a loaded gun.

## What this test asserts, and why it is the whole set

An earlier version of this file pinned ONLY `remove_table`, on the argument that
the constraint and index drops were "noise" and "NOT a defect". That was wrong,
and the docstring saying so has been deleted rather than softened. `remove_table`
was green while the comparison proposed dropping `ck_export_hashes_are_distinct`,
`ck_state_reopen_requires_attestation` and `uq_decision_one_current_per_reviewer`
— a guard that passes while the schema's integrity is destroyed manufactures
false confidence, which is worse than having no guard at all.

So every destructive operation the comparison can report is pinned by exact
equality: `remove_table`, `remove_column`, `remove_index`, `remove_constraint`,
`remove_fk`. Nothing is allowed by prefix or by count.

Pinning the whole set is viable because the set is STABLE. Measured before
writing it down: the comparison is byte-identical across repeated runs against
one database, and byte-identical again against a second, independently created
and freshly migrated database. It is not order-dependent and not drifting.

## When this test fails

It is not asking you to make the diff empty — the diff is inherent to the
accessor design and will never be empty. It is asking you to notice a CHANGE in
what autogenerate would destroy:

* A name ADDED to the diff means new DDL in a migration that the ORM does not
  express. Usually correct and expected — add it to the pin, which is the moment
  you acknowledge that autogenerate would silently drop your new constraint.
* A name REMOVED from the diff means the ORM started expressing it, or the object
  is gone from the database. If you did not intend that, a migration or a model
  changed under you.
* Whole tables appearing in `remove_table` mean a model stopped being imported by
  `src/models/__init__.py` — the empty-metadata defect, which proposes dropping
  every table in the database.

## Not asserted here, but true

Of the 59 foreign keys the comparison removes it re-adds only 24, and those 24
come back WITHOUT the `ON DELETE RESTRICT` the migrations declare (the models
spell `ForeignKey(...)` with no `ondelete`). So autogenerate output also silently
weakens referential behaviour on the keys it does keep. That is an `add_fk`
rather than a destructive op, so it is outside this test's contract; it is
recorded here because it is part of the same hazard.
"""

from __future__ import annotations

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

#: Reflection only — this test issues no DDL and is safe against the shared
#: database. The owner is used because it is the role alembic itself connects as.
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not OWNER_URL.startswith("postgresql"),
    reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
)

#: Every operation `compare_metadata` can report that destroys a schema object.
#: `add_*` and `modify_*` are not pinned: they cost a review, not data.
DESTRUCTIVE_OPERATIONS = ("remove_table", "remove_column", "remove_index", "remove_constraint", "remove_fk")

#: A floor, because an empty database yields `add_table` for everything and NO
#: removals at all — the assertion below would pass while comparing against
#: nothing.
MINIMUM_REFLECTED_TABLES = 20

#: Exactly what `alembic revision --autogenerate` would destroy today, keyed by
#: operation and named `table.object` (bare table name for `remove_table`).
#:
#: `operation_idempotency` is the one deliberate `remove_table`: migration 0001
#: creates it, no application code reads or writes it, and a mapped class nothing
#: uses would be a second definition to keep in sync for no benefit.
#:
#: ⚠️ `artifact_records` (`ER-M025`, `FND-009`) WAS the second, for one commit.
#: While no ORM class existed the whole table sat in `remove_table` and none of
#: its CHECKs appeared at all — autogenerate does not report `remove_constraint`
#: for a table it is already dropping wholesale. Adding `ArtifactRecord` to
#: `src/models/foundation.py` moved it: the table left `remove_table` and its
#: twenty CHECKs arrived in `remove_constraint`, exactly as the note here
#: predicted. Its two lookup indexes stay in `remove_index`, because the ORM
#: declares the columns and not the indexes.
#:
#: That transition is worth keeping written down: a table pinned as
#: `remove_table` HIDES every constraint on it, so the pin looks smaller and
#: quieter than the DDL actually is. `operation_idempotency` is still in that
#: state, and its CHECKs are likewise invisible here.
#:
#: Everything else here is DDL the migrations declare and the models do not
#: express. It is pinned, not excused — see the module docstring.
PINNED_BLAST_RADIUS: dict[str, frozenset[str]] = {
    "remove_table": frozenset(
        {
            "operation_idempotency",
        }
    ),
    "remove_column": frozenset(),
    "remove_index": frozenset(
        {
            "artifact_records.ix_artifact_records_artifact",
            "artifact_records.ix_artifact_records_owning_aggregate",
            "corpus_version_members.ix_corpus_members_record",
            "deduplication_runs.ix_dedup_runs_review",
            "domain_inbox.ix_domain_inbox_progress",
            "domain_outbox.ix_domain_outbox_aggregate",
            "domain_outbox.ix_domain_outbox_occurred_at",
            "duplicate_cluster_members.ix_cluster_members_record",
            "duplicate_resolutions.ix_resolutions_cluster",
            "duplicate_resolutions.uq_resolution_one_head_per_cluster",
            "duplicate_resolutions.uq_resolution_supersession_linear",
            "durable_jobs.ix_durable_jobs_claimable",
            "durable_jobs.ix_durable_jobs_lease_expiry",
            "durable_jobs.ix_durable_jobs_review",
            "durable_jobs.uq_durable_jobs_idempotency",
            "event_deliveries.ix_event_deliveries_claimable",
            "event_deliveries.ix_event_deliveries_lease",
            "event_deliveries.uq_event_deliveries_consumer_event",
            "evidence_acceptances.ix_acceptances_subject",
            "evidence_acceptances.uq_acceptance_one_chain_per_subject",
            "evidence_acceptances.uq_acceptance_supersession_linear",
            "evidence_snapshot_members.ix_snapshot_members_order",
            "evidence_snapshots.ix_evidence_snapshots_content_hash",
            "evidence_snapshots.ix_evidence_snapshots_review",
            "evidence_snapshots.uq_snapshot_one_chain_per_review",
            "evidence_snapshots.uq_snapshot_one_per_job",
            "evidence_snapshots.uq_snapshot_supersession_linear",
            "extraction_dataset_versions.uq_extraction_dataset_supersession_linear",
            "flow_projections.ix_flow_projections_review",
            "import_rows.ix_import_rows_batch",
            "import_rows.ix_import_rows_review_status",
            "job_attempts.ix_job_attempts_job",
            "observation_versions.ix_observation_versions_observation",
            "observation_versions.uq_observation_supersession_linear",
            "observations.ix_observations_dataset",
            "research_assertion_support.ix_assertion_support_observation",
            "research_assertion_versions.uq_research_assertion_supersession_linear",
            "research_domain_events.ix_research_domain_events_order",
            "research_domain_events.ix_research_domain_events_position",
            "research_object_records.ix_research_object_records_object",
            "research_object_records.uq_research_object_record_one_primary",
            "research_object_versions.ix_research_object_versions_object",
            "research_object_versions.uq_research_object_version_supersession_linear",
            "research_objects.ix_research_objects_review",
            "review_protocol_versions.ix_review_protocol_versions_review",
            "review_record_versions.ix_record_versions_doi",
            "review_record_versions.ix_record_versions_pmid",
            "review_record_versions.ix_record_versions_record",
            "review_record_versions.ix_record_versions_title_year",
            "review_stages.ix_review_stages_review",
            "review_state_history.ix_review_state_history_review",
            "screening_adjudications.uq_adjudication_one_current_per_conflict",
            "screening_adjudications.uq_adjudication_supersession_linear",
            "screening_assignments.ix_assignment_corpus_version",
            "screening_assignments.ix_assignments_queue",
            "screening_conflicts.ix_conflicts_review",
            "screening_decisions.ix_decisions_record",
            "screening_decisions.ix_decisions_reviewer_progress",
            "screening_decisions.uq_decision_one_current_per_reviewer",
            "screening_decisions.uq_decision_supersession_linear",
            "screening_unblinding_events.ix_unblinding_conflict",
        }
    ),
    "remove_constraint": frozenset(
        {
            "corpus_version_members.uq_corpus_member_ordinal",
            "corpus_versions.uq_corpus_version_supersession_linear",
            "domain_inbox.uq_domain_inbox_consumer_event",
            "duplicate_clusters.uq_cluster_key_per_run",
            "duplicate_pair_scores.uq_pair_per_run",
            "evidence_snapshot_members.uq_snapshot_member_identity",
            "evidence_snapshots.uq_snapshot_version_per_review",
            "extraction_dataset_versions.uq_extraction_dataset_version_number",
            "extraction_datasets.uq_extraction_dataset_key",
            "extraction_datasets.uq_extraction_dataset_tenant",
            "flow_revisions.uq_flow_revision_number",
            "job_attempts.uq_job_attempts_ordinal",
            "observation_versions.uq_observation_version_number",
            "observations.uq_observation_tenant",
            "observations.uq_observation_variable",
            "projection_checkpoints.uq_projection_checkpoint",
            "research_assertion_support.uq_assertion_support_once",
            "research_assertion_versions.uq_research_assertion_version_number",
            "research_assertions.uq_research_assertion_key",
            "research_assertions.uq_research_assertion_tenant",
            "research_domain_events.research_domain_events_log_position_key",
            "research_object_records.uq_research_object_record_unique_report",
            "research_object_versions.uq_research_object_version_number",
            "research_objects.uq_research_object_key",
            "research_objects.uq_research_object_tenant",
            "review_record_versions.uq_record_version_supersession_linear",
            "screening_assignments.uq_assignment_once",
            "screening_conflicts.uq_conflict_once",
            "snapshot_artifacts.uq_snapshot_artifact_kind",
        }
    ),
    "remove_fk": frozenset(
        {
            "corpus_version_members.corpus_version_members_corpus_version_id_fkey",
            "corpus_version_members.corpus_version_members_review_id_fkey",
            "corpus_version_members.corpus_version_members_review_record_id_fkey",
            "corpus_version_members.corpus_version_members_review_record_version_id_fkey",
            "corpus_versions.corpus_versions_review_id_fkey",
            "corpus_versions.corpus_versions_supersedes_version_id_fkey",
            "deduplication_runs.deduplication_runs_corpus_version_id_fkey",
            "deduplication_runs.deduplication_runs_review_id_fkey",
            "duplicate_cluster_members.duplicate_cluster_members_cluster_id_fkey",
            "duplicate_cluster_members.duplicate_cluster_members_review_record_id_fkey",
            "duplicate_cluster_members.duplicate_cluster_members_review_record_version_id_fkey",
            "duplicate_clusters.duplicate_clusters_dedup_run_id_fkey",
            "duplicate_clusters.duplicate_clusters_review_id_fkey",
            "duplicate_pair_scores.duplicate_pair_scores_dedup_run_id_fkey",
            "duplicate_pair_scores.duplicate_pair_scores_left_record_id_fkey",
            "duplicate_pair_scores.duplicate_pair_scores_right_record_id_fkey",
            "duplicate_resolutions.duplicate_resolutions_cluster_id_fkey",
            "duplicate_resolutions.duplicate_resolutions_review_id_fkey",
            "duplicate_resolutions.duplicate_resolutions_supersedes_resolution_id_fkey",
            "duplicate_resolutions.duplicate_resolutions_survivor_record_id_fkey",
            "duplicate_survivorship_fields.duplicate_survivorship_fields_resolution_id_fkey",
            "duplicate_survivorship_fields.duplicate_survivorship_fields_source_record_version_id_fkey",
            "durable_jobs.durable_jobs_review_id_fkey",
            "evidence_acceptances.evidence_acceptances_extraction_dataset_version_id_fkey",
            "evidence_acceptances.evidence_acceptances_observation_version_id_fkey",
            "evidence_acceptances.evidence_acceptances_protocol_version_id_fkey",
            "evidence_acceptances.evidence_acceptances_research_object_version_id_fkey",
            "evidence_acceptances.evidence_acceptances_review_id_fkey",
            "evidence_acceptances.evidence_acceptances_supersedes_acceptance_id_fkey",
            "extraction_dataset_versions.extraction_dataset_versions_protocol_version_id_fkey",
            "extraction_dataset_versions.extraction_dataset_versions_review_id_fkey",
            "extraction_dataset_versions.extraction_dataset_versions_supersedes_version_id_fkey",
            "extraction_dataset_versions.fk_extraction_dataset_version_same_tenant",
            "extraction_datasets.extraction_datasets_review_id_fkey",
            "extraction_datasets.fk_extraction_dataset_object_same_tenant",
            "flow_projections.flow_projections_flow_revision_id_fkey",
            "flow_projections.flow_projections_review_id_fkey",
            "flow_revisions.flow_revisions_review_id_fkey",
            "import_batches.import_batches_review_id_fkey",
            "import_rows.import_rows_import_batch_id_fkey",
            "import_rows.import_rows_review_id_fkey",
            "inclusion_exports.inclusion_exports_corpus_version_id_fkey",
            "inclusion_exports.inclusion_exports_flow_revision_id_fkey",
            "inclusion_exports.inclusion_exports_review_id_fkey",
            "job_attempts.job_attempts_job_id_fkey",
            "observation_versions.fk_observation_version_same_tenant",
            "observation_versions.observation_versions_extraction_dataset_version_id_fkey",
            "observation_versions.observation_versions_protocol_version_id_fkey",
            "observation_versions.observation_versions_review_id_fkey",
            "observation_versions.observation_versions_supersedes_version_id_fkey",
            "observations.fk_observation_dataset_same_tenant",
            "observations.observations_review_id_fkey",
            "research_assertion_support.research_assertion_support_observation_version_id_fkey",
            "research_assertion_support.research_assertion_support_research_assertion_version_id_fkey",
            "research_assertion_support.research_assertion_support_review_id_fkey",
            "research_assertion_versions.fk_research_assertion_version_same_tenant",
            "research_assertion_versions.research_assertion_versions_protocol_version_id_fkey",
            "research_assertion_versions.research_assertion_versions_review_id_fkey",
            "research_assertion_versions.research_assertion_versions_supersedes_version_id_fkey",
            "research_assertions.research_assertions_review_id_fkey",
            "research_object_records.fk_research_object_record_same_tenant",
            "research_object_records.research_object_records_corpus_version_id_fkey",
            "research_object_records.research_object_records_review_id_fkey",
            "research_object_records.research_object_records_review_record_id_fkey",
            "research_object_records.research_object_records_review_record_version_id_fkey",
            "research_object_versions.fk_research_object_version_same_tenant",
            "research_object_versions.research_object_versions_protocol_version_id_fkey",
            "research_object_versions.research_object_versions_review_id_fkey",
            "research_object_versions.research_object_versions_supersedes_version_id_fkey",
            "research_objects.research_objects_review_id_fkey",
            "review_protocol_versions.review_protocol_versions_review_id_fkey",
            "review_protocol_versions.review_protocol_versions_supersedes_version_id_fkey",
            "review_record_versions.review_record_versions_review_id_fkey",
            "review_record_versions.review_record_versions_review_record_id_fkey",
            "review_record_versions.review_record_versions_source_import_row_id_fkey",
            "review_record_versions.review_record_versions_supersedes_version_id_fkey",
            "review_records.review_records_review_id_fkey",
            "review_stages.review_stages_protocol_version_id_fkey",
            "review_stages.review_stages_review_id_fkey",
            "review_state_history.review_state_history_protocol_version_id_fkey",
            "review_state_history.review_state_history_review_id_fkey",
            "screening_adjudications.screening_adjudications_conflict_id_fkey",
            "screening_adjudications.screening_adjudications_review_id_fkey",
            "screening_adjudications.screening_adjudications_supersedes_adjudication_id_fkey",
            "screening_assignments.screening_assignments_corpus_version_id_fkey",
            "screening_assignments.screening_assignments_review_id_fkey",
            "screening_assignments.screening_assignments_review_record_id_fkey",
            "screening_assignments.screening_assignments_stage_id_fkey",
            "screening_conflicts.screening_conflicts_review_id_fkey",
            "screening_conflicts.screening_conflicts_review_record_id_fkey",
            "screening_conflicts.screening_conflicts_stage_id_fkey",
            "screening_decisions.screening_decisions_assignment_id_fkey",
            "screening_decisions.screening_decisions_review_id_fkey",
            "screening_decisions.screening_decisions_review_record_id_fkey",
            "screening_decisions.screening_decisions_review_record_version_id_fkey",
            "screening_decisions.screening_decisions_stage_id_fkey",
            "screening_decisions.screening_decisions_supersedes_decision_id_fkey",
            "screening_unblinding_events.screening_unblinding_events_conflict_id_fkey",
            "screening_unblinding_events.screening_unblinding_events_review_id_fkey",
        }
    ),
}


def _name(operation: str, diff: tuple[object, ...]) -> str:
    """`table.object` for the diff tuple, matching how the pin is written."""
    if operation == "remove_table":
        return str(getattr(diff[1], "name", diff[1]))
    if operation == "remove_column":
        # ("remove_column", schema, table_name, Column)
        return f"{diff[2]}.{getattr(diff[3], 'name', diff[3])}"
    obj = diff[1]
    table = getattr(obj, "table", None)
    return f"{getattr(table, 'name', '?')}.{getattr(obj, 'name', obj)}"


def _destructive_operations(diffs: list[object]) -> dict[str, set[str]]:
    """Flatten alembic's diff tree into `{operation: {qualified names}}`.

    `compare_metadata` nests per-table diffs one list deep, so a flat scan would
    silently see zero constraint removals and make this test vacuous.
    """
    found: dict[str, set[str]] = {operation: set() for operation in DESTRUCTIVE_OPERATIONS}

    def walk(items: list[object]) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item)
            elif isinstance(item, tuple) and item and item[0] in found:
                found[str(item[0])].add(_name(str(item[0]), item))

    walk(diffs)
    return found


def test_autogenerate_destructive_operations_match_the_pinned_blast_radius() -> None:
    import models  # noqa: F401 - populating Base.metadata is the point
    from database import Base

    engine = create_engine(OWNER_URL, future=True, connect_args={"prepare_threshold": 0})
    try:
        reflected = set(inspect(engine).get_table_names())
        assert len(reflected) >= MINIMUM_REFLECTED_TABLES, (
            f"only {len(reflected)} tables in the database — is it migrated? Comparing against an "
            "empty schema would make this test pass without checking anything"
        )
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()

    found = _destructive_operations(list(diffs))

    unexpected = {
        operation: sorted(found[operation] - PINNED_BLAST_RADIUS[operation])
        for operation in DESTRUCTIVE_OPERATIONS
        if found[operation] - PINNED_BLAST_RADIUS[operation]
    }
    no_longer_dropped = {
        operation: sorted(PINNED_BLAST_RADIUS[operation] - found[operation])
        for operation in DESTRUCTIVE_OPERATIONS
        if PINNED_BLAST_RADIUS[operation] - found[operation]
    }

    assert not unexpected and not no_longer_dropped, (
        "`alembic revision --autogenerate` would destroy a different set of objects than this file "
        "pins. Autogenerate output here is ALREADY unsafe to apply unreviewed; this test exists so "
        "that changes to HOW unsafe it is are noticed.\n"
        f"  newly destroyed (add to PINNED_BLAST_RADIUS once you have confirmed the DDL is "
        f"intentionally unexpressed by the ORM): {unexpected}\n"
        f"  no longer destroyed (the ORM now expresses it, or the object is gone from the "
        f"database — shrink the pin): {no_longer_dropped}"
    )

    # A backstop against the equality above going vacuous. If a future change to
    # alembic's diff shape made `_destructive_operations` stop recognising
    # anything, every set would be empty — and somebody emptying the pin to
    # "fix" the resulting failure would leave a test that asserts nothing at all.
    # A schema this size cannot genuinely produce zero removed foreign keys.
    assert found["remove_fk"], (
        "the comparison reported no removed foreign keys at all, which the migrated schema cannot "
        "produce. Either PINNED_BLAST_RADIUS has been emptied and this test now asserts nothing, or "
        "alembic's diff shape changed and `_destructive_operations` is no longer reading it."
    )
