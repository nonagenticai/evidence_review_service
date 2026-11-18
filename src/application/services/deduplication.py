"""Deduplication runs and reversible resolution — `ER-003`.

The engine proposes clusters; a human resolves them. Reversal is an insert, so
"merge -> split -> merge" leaves three attributable rows and the current answer
is the tail of the chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from actor import Actor
from api.problem_details import NotFoundError, StateConflictError, UnprocessableStateError
from application.services import events
from application.services.imports import SURVIVABLE_FIELDS, apply_survivorship, latest_corpus
from application.services.reviews import require_review_lead_for, require_review_open
from domain.policies.deduplication_engine import (
    DEFAULT_CONFIG,
    ENGINE_ID,
    ENGINE_VERSION,
    CandidateRecord,
    MergedGroup,
    build_clusters,
    canonical_presentation,
    components_without_survivor,
    find_pairs,
    input_digest,
)
from models.records import CorpusVersionMember, ReviewRecordVersion
from models.screening import (
    DeduplicationRun,
    DuplicateCluster,
    DuplicateClusterMember,
    DuplicatePairScore,
    DuplicateResolution,
    DuplicateSurvivorshipField,
)

#: Exact matching has no uncertainty to express. The column exists for a
#: qualified similarity extension (`ER-003.2`) that can populate it later
#: without a migration that rewrites history.
EXACT_SCORE = 1


def _candidates(session: Session, corpus_version_id: uuid.UUID) -> list[CandidateRecord]:
    """The sealed membership, as comparable projections.

    Read from `corpus_version_members`, not from "all current records" — the run
    is frozen against a corpus version (`ER-003.1`), so a record imported after
    the seal must not silently enter the comparison.
    """
    rows = session.execute(
        select(
            ReviewRecordVersion.review_record_id,
            ReviewRecordVersion.review_record_version_id,
            ReviewRecordVersion.normalized_doi,
            ReviewRecordVersion.normalized_pmid,
            ReviewRecordVersion.normalized_title,
            ReviewRecordVersion.publication_year,
        )
        .join(
            CorpusVersionMember,
            CorpusVersionMember.review_record_version_id == ReviewRecordVersion.review_record_version_id,
        )
        .where(CorpusVersionMember.corpus_version_id == corpus_version_id)
        .order_by(ReviewRecordVersion.review_record_id)
    ).all()
    return [CandidateRecord(*row) for row in rows]


def create_run(session: Session, actor: Actor, review_id: uuid.UUID, *, config: dict | None = None) -> dict[str, Any]:
    """Freeze a dedup run against the latest sealed corpus — **lead only**.

    `require_human` was the whole check, so any reviewer in the tenant could
    start a run on any review. A run is not a read: it writes
    `duplicate_clusters` and `duplicate_pair_scores`, and those clusters are the
    only things `resolve_cluster` can be pointed at — so an unauthorised run is
    what MAKES the merge below reachable, on a cluster set the lead never
    proposed and whose `config` the caller chose.
    """
    subject = require_review_lead_for(session, actor, review_id, "starting a deduplication run")
    require_review_open(session, review_id, "starting a deduplication run")
    corpus = latest_corpus(session, review_id)
    effective = {**DEFAULT_CONFIG, **(config or {})}
    digest = input_digest(corpus.corpus_version_id, effective)

    existing = session.execute(
        select(DeduplicationRun).where(DeduplicationRun.review_id == review_id, DeduplicationRun.input_digest == digest)
    ).scalar_one_or_none()
    if existing is not None:
        # Same corpus, same engine, same config is the same run. Recomputing it
        # would produce identical clusters with different ids, and any
        # resolution already taken would point at the older ones — two answers
        # to "which cluster set is current?".
        return run_summary(session, existing, replayed=True)

    candidates = _candidates(session, corpus.corpus_version_id)
    pairs = find_pairs(candidates, effective)
    clusters = build_clusters(pairs)
    by_record = {c.review_record_id: c for c in candidates}

    # AND THE SELECT ABOVE DOES NOT ENFORCE "THE SAME RUN".
    #
    # It is a check-then-act at READ COMMITTED: two leads (or two tabs, or a
    # retrying worker, or a second pod) both take it before either commits, both
    # see nothing, and both write a run. Until `0012` the only index on this key
    # was `ix_dedup_runs_digest`, which is NOT unique — verified against the live
    # database — so nothing underneath the check enforced what the check was for.
    #
    # Reproduced with two threads on two sessions
    # (`test_two_concurrent_dedup_runs_over_one_corpus_leave_one_run`): two runs
    # over one `(corpus, engine, config)`, each proposing its own cluster ids over
    # the same records. That is precisely the two-answers-to-one-question the
    # replay check was written to prevent, and it is worse than a wasted
    # computation: `resolve_cluster` accepts either set, resolutions taken against
    # one are invisible to the other, and a merge recorded against the losing set
    # removes nothing from the review while reading as done.
    #
    # ON CONFLICT DO NOTHING on `uq_dedup_run_input_digest`, then re-select — the
    # house pattern (`flow.current_flow_revision`,
    # `screening._maybe_open_conflict`). The loser writes NO run, and therefore no
    # pair scores, no clusters and no members: it returns before any of that is
    # built. Both halves are needed; the constraint alone would hand the loser an
    # `IntegrityError` where the documented answer is `replayed: true`.
    run_id = uuid.uuid4()
    inserted = session.execute(
        pg_insert(DeduplicationRun)
        .values(
            dedup_run_id=run_id,
            review_id=review_id,
            tenant_id=actor.tenant_id,
            corpus_version_id=corpus.corpus_version_id,
            engine_id=effective.get("engine_id", ENGINE_ID),
            engine_version=effective.get("engine_version", ENGINE_VERSION),
            config=effective,
            input_digest=digest,
            cluster_count=len(clusters),
            pair_count=len(pairs),
            created_by_subject_id=subject,
        )
        .on_conflict_do_nothing(constraint="uq_dedup_run_input_digest")
        .returning(DeduplicationRun.dedup_run_id)
    ).scalar_one_or_none()
    session.flush()
    if inserted is None:
        winner = session.execute(
            select(DeduplicationRun).where(
                DeduplicationRun.review_id == review_id, DeduplicationRun.input_digest == digest
            )
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover - the insert above either wrote it or lost to a writer who did
            raise UnprocessableStateError("the deduplication run disappeared while being written")
        return run_summary(session, winner, replayed=True)

    run = session.get(DeduplicationRun, run_id)
    if run is None:  # pragma: no cover - inserted by this transaction one statement ago
        raise UnprocessableStateError("the deduplication run disappeared while being written")

    for pair in pairs:
        session.add(
            DuplicatePairScore(
                pair_score_id=uuid.uuid4(),
                dedup_run_id=run.dedup_run_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                left_record_id=pair.left,
                right_record_id=pair.right,
                matched_on=pair.matched_on,
                score=EXACT_SCORE,
            )
        )

    for cluster in clusters:
        cluster_id = uuid.uuid4()
        session.add(
            DuplicateCluster(
                cluster_id=cluster_id,
                dedup_run_id=run.dedup_run_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                cluster_key=cluster.cluster_key,
                member_count=len(cluster.members),
            )
        )
        for record_id in cluster.members:
            session.add(
                DuplicateClusterMember(
                    cluster_id=cluster_id,
                    review_record_id=record_id,
                    review_id=review_id,
                    tenant_id=actor.tenant_id,
                    review_record_version_id=by_record[record_id].review_record_version_id,
                )
            )

    session.flush()
    events.emit_duplicate_cluster_proposed(session, run)
    return run_summary(session, run, replayed=False)


def run_summary(session: Session, run: DeduplicationRun, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "dedup_run_id": str(run.dedup_run_id),
        "review_id": str(run.review_id),
        "corpus_version_id": str(run.corpus_version_id),
        "engine_id": run.engine_id,
        "engine_version": run.engine_version,
        "config": run.config,
        "input_digest": run.input_digest,
        "cluster_count": run.cluster_count,
        "pair_count": run.pair_count,
        "replayed": replayed,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _resolution_tail(rows: Sequence[DuplicateResolution]) -> DuplicateResolution | None:
    """Walk one cluster's supersession chain to its head. Pure; no I/O."""
    if not rows:
        return None
    by_supersedes = {r.supersedes_resolution_id: r for r in rows}
    node = by_supersedes.get(None)
    if node is None:
        return None
    seen = {node.resolution_id}
    while node.resolution_id in by_supersedes:
        node = by_supersedes[node.resolution_id]
        if node.resolution_id in seen:  # pragma: no cover - the unique index forbids a cycle
            break
        seen.add(node.resolution_id)
    return node


def current_resolutions(session: Session, cluster_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, DuplicateResolution]:
    """:func:`current_resolution` for many clusters, in ONE statement.

    The chain walk was always pure Python; only the fetch was per-cluster.
    `flow._collect` called the single form inside a loop over every cluster in
    the review, and `_collect` runs on EVERY `/flow` read.
    """
    if not cluster_ids:
        return {}
    grouped: dict[uuid.UUID, list[DuplicateResolution]] = {}
    for row in (
        session.execute(select(DuplicateResolution).where(DuplicateResolution.cluster_id.in_(cluster_ids)))
        .scalars()
        .all()
    ):
        grouped.setdefault(row.cluster_id, []).append(row)
    tails = {cid: _resolution_tail(rows) for cid, rows in grouped.items()}
    return {cid: tail for cid, tail in tails.items() if tail is not None}


def cluster_members(session: Session, cluster_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """`cluster_id -> [review_record_id]` for many clusters, in ONE statement."""
    if not cluster_ids:
        return {}
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for cluster_id, record_id in session.execute(
        select(DuplicateClusterMember.cluster_id, DuplicateClusterMember.review_record_id).where(
            DuplicateClusterMember.cluster_id.in_(cluster_ids)
        )
    ).all():
        out.setdefault(cluster_id, []).append(record_id)
    return out


def current_resolution(session: Session, cluster_id: uuid.UUID) -> DuplicateResolution | None:
    """The tail of the supersession chain for a cluster.

    Walked from the head rather than taken as "the newest row", because
    `decided_at` ties are possible and the chain is the authority. The two
    partial uniques guarantee this walk terminates and is unique.
    """
    rows = (
        session.execute(select(DuplicateResolution).where(DuplicateResolution.cluster_id == cluster_id)).scalars().all()
    )
    return _resolution_tail(rows)


def merged_groups(session: Session, review_id: uuid.UUID) -> list[MergedGroup]:
    """Every cluster in this review whose CURRENT resolution is a merge.

    The same three statements `flow._collect` uses to build its removal set, in
    the same shape, so the write-time refusal and the derivation-time backstop
    are answering one question and cannot drift into two.
    """
    cluster_ids = list(
        session.execute(select(DuplicateCluster.cluster_id).where(DuplicateCluster.review_id == review_id))
        .scalars()
        .all()
    )
    if not cluster_ids:
        # ONE statement, not three, for a review that has never been deduplicated
        # — and `screening_queue` calls this on every page, so the common case is
        # the one that has to be cheap.
        return []
    resolutions = current_resolutions(session, cluster_ids)
    members = cluster_members(session, cluster_ids)
    groups: list[MergedGroup] = []
    for cluster_id in cluster_ids:
        resolution = resolutions.get(cluster_id)
        if resolution is None or resolution.resolution_type != "merge" or resolution.survivor_record_id is None:
            continue
        groups.append(
            MergedGroup(
                cluster_id=cluster_id,
                members=frozenset(members.get(cluster_id, [])),
                survivor=resolution.survivor_record_id,
            )
        )
    return groups


def resolve_cluster(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    cluster_id: uuid.UUID,
    *,
    resolution_type: str,
    rationale: str,
    survivor_record_id: uuid.UUID | None = None,
    survivorship: dict[str, uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Record a resolution, superseding the current one when there is one. **Lead only.**

    `require_human` was the whole check. A `merge` is the most destructive write
    any non-lead could reach in this service: it takes records OUT of the current
    record set, so `GET /records` total drops, the next sealed corpus version has
    a different `member_manifest_hash`, and the export's `included` set loses
    them. It is reversible only in the sense that a `split` is a further row in
    the chain — the merge itself is never unwritten, and every artefact sealed in
    between still cites the membership it produced.
    """
    subject = require_review_lead_for(session, actor, review_id, "resolving a duplicate cluster")
    require_review_open(session, review_id, "resolving a duplicate cluster")
    cluster = session.get(DuplicateCluster, cluster_id)
    if cluster is None or cluster.review_id != review_id:
        raise NotFoundError("no such duplicate cluster for this review")

    if resolution_type == "merge":
        if survivor_record_id is None:
            raise UnprocessableStateError("a merge must name the surviving record")
        member_rows = (
            session.execute(select(DuplicateClusterMember).where(DuplicateClusterMember.cluster_id == cluster_id))
            .scalars()
            .all()
        )
        members = {m.review_record_id for m in member_rows}
        cluster_member_version_ids = {m.review_record_version_id for m in member_rows}
        if survivor_record_id not in members:
            raise UnprocessableStateError("the survivor must be a member of the cluster being merged")

        # A merge takes records OUT of the current record set, and `flow` unions
        # those removals across every merged cluster in the review. Nothing
        # checked that the union leaves a survivor standing, so two runs over the
        # same pair with different survivors removed BOTH — a study in no bucket
        # at all, sealed into an export that verifies. Refused here, at the moment
        # the lead makes the choice, because the alternative is telling them at
        # export time about a resolution they took days earlier.
        #
        # Checked on the PROPOSAL rather than after writing: this resolution
        # supersedes any current one on the same cluster, so the candidate set is
        # "every other cluster's current merge, plus this". Nothing is written if
        # it fails.
        proposed = [group for group in merged_groups(session, review_id) if group.cluster_id != cluster_id]
        proposed.append(MergedGroup(cluster_id=cluster_id, members=frozenset(members), survivor=survivor_record_id))
        emptied = components_without_survivor(proposed)
        if emptied:
            component, cluster_ids = emptied[0]
            others = sorted(str(c) for c in cluster_ids if c != cluster_id)
            raise UnprocessableStateError(
                "this merge would remove every record of a duplicate group, so the study would leave the review "
                "with no exclusion reason and no decision. "
                f"Records: {sorted(str(r) for r in component)}. "
                f"Conflicting resolutions on clusters: {others}. "
                "Split one of those clusters, or name the record they already keep as this survivor."
            )
    else:
        # A non-merge cannot carry survivorship at all (refused below), so an
        # empty set is the honest value rather than a lookup nothing will use.
        cluster_member_version_ids = set()
        if survivor_record_id is not None:
            raise UnprocessableStateError(f"a {resolution_type} resolution has no survivor")
        if survivorship:
            # Accepted silently until now. Survivorship is a choice between the
            # values of records being merged INTO one; a resolution with no
            # survivor has nothing to apply it to, so the rows were written and
            # could never mean anything.
            raise UnprocessableStateError(
                f"a {resolution_type} resolution has no survivor, so it cannot carry survivorship fields"
            )

    previous = current_resolution(session, cluster_id)
    if previous is not None and previous.resolution_type == resolution_type and resolution_type != "merge":
        raise StateConflictError(f"cluster is already resolved as {resolution_type}")

    resolution = DuplicateResolution(
        resolution_id=uuid.uuid4(),
        cluster_id=cluster_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        resolution_type=resolution_type,
        supersedes_resolution_id=previous.resolution_id if previous else None,
        survivor_record_id=survivor_record_id,
        rationale=rationale,
        decided_by_subject_id=subject,
    )
    session.add(resolution)
    session.flush()

    sources: dict[str, ReviewRecordVersion] = {}
    for field_name, source_version_id in (survivorship or {}).items():
        # The FK does the real work: a survivor field cannot name a version that
        # does not exist, and ON DELETE RESTRICT means the original it names
        # cannot later be removed.
        #
        # `session.get` by id, NOT a tail lookup. Survivorship names an explicit
        # source version out of `canonical_presentation` — that is what makes it
        # "a choice between the values the reviewer was shown" rather than a
        # choice between whatever those records say today.
        version = session.get(ReviewRecordVersion, source_version_id)
        if version is None or version.review_id != review_id:
            raise UnprocessableStateError(
                f"survivorship field {field_name!r} names a record version outside this review"
            )
        if version.review_record_version_id not in cluster_member_version_ids:
            # REVIEW SCOPE WAS NOT ENOUGH. `SURVIVABLE_FIELDS` is documented as
            # "a choice between the values a reviewer was shown", and what they
            # were shown is `canonical_presentation`, which only ever offers
            # members of THIS cluster. Checking `review_id` alone let a lead take
            # a value from an unrelated record — measured: `200`, with
            # `applied_version_id` set — and the same path rewrites `doi` and
            # `pmid`, which `apply_survivorship` then recomputes the normalized
            # forms from. That moves the dedup baseline of a record using content
            # that was never part of the duplicate proposal anybody resolved.
            raise UnprocessableStateError(
                f"survivorship field {field_name!r} names a record version that is not a member of "
                "this cluster; a survivor field is a choice between the values the reviewer was "
                "shown for these records",
                cluster_id=str(cluster_id),
            )
        if field_name not in SURVIVABLE_FIELDS:
            raise UnprocessableStateError(
                f"survivorship cannot choose {field_name!r}: a survivor field is a choice between values "
                "a reviewer was shown, and identity, tenancy and provenance are not among them",
                allowed=sorted(SURVIVABLE_FIELDS),
                rejected=[field_name],
            )
        sources[field_name] = version
        session.add(
            DuplicateSurvivorshipField(
                survivorship_field_id=uuid.uuid4(),
                resolution_id=resolution.resolution_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                field_name=field_name,
                value={"value": getattr(version, field_name, None)},
                source_record_version_id=source_version_id,
            )
        )
    session.flush()

    # Until now this function ended here, and the survivor record was BYTE-IDENTICAL
    # after a merge that chose its fields. The rows above were write-only: nothing
    # in `src/` ever selected `duplicate_survivorship_fields`, so a lead's choice
    # produced no observable change on any endpoint, and `GET /records/{id}`'s
    # advertised "full version history" was always exactly one version.
    applied = None
    if sources and survivor_record_id is not None:
        applied = apply_survivorship(
            session,
            actor,
            review_id,
            survivor_record_id=survivor_record_id,
            sources=sources,
            resolution_id=resolution.resolution_id,
            cluster_id=cluster_id,
            subject=subject,
        )
    events.emit_duplicate_cluster_resolved(session, resolution)
    return resolution_summary(resolution, applied_version=applied)


def resolution_summary(
    resolution: DuplicateResolution, *, applied_version: ReviewRecordVersion | None = None
) -> dict[str, Any]:
    return {
        # `null` distinguishes "no survivorship, or a choice that changed nothing"
        # from "a new version was minted". Without it the caller cannot tell a
        # no-op re-affirmation from a real edit.
        "applied_version_id": (str(applied_version.review_record_version_id) if applied_version else None),
        "resolution_id": str(resolution.resolution_id),
        "cluster_id": str(resolution.cluster_id),
        "resolution_type": resolution.resolution_type,
        "supersedes_resolution_id": (
            str(resolution.supersedes_resolution_id) if resolution.supersedes_resolution_id else None
        ),
        "survivor_record_id": str(resolution.survivor_record_id) if resolution.survivor_record_id else None,
        "rationale": resolution.rationale,
        "decided_by_subject_id": resolution.decided_by_subject_id,
        "decided_at": resolution.decided_at.isoformat() if resolution.decided_at else None,
    }


def list_clusters(session: Session, review_id: uuid.UUID, dedup_run_id: uuid.UUID) -> list[dict[str, Any]]:
    clusters = (
        session.execute(
            select(DuplicateCluster)
            .where(DuplicateCluster.review_id == review_id, DuplicateCluster.dedup_run_id == dedup_run_id)
            .order_by(DuplicateCluster.cluster_key)
        )
        .scalars()
        .all()
    )

    out: list[dict[str, Any]] = []
    for cluster in clusters:
        # Raw AND normalized, both. The normalized values are what matched; the
        # raw values are what the source actually said. Showing only the
        # normalized ones — which an earlier version of this function did, under
        # the key `doi` — presents a casefolded, prefix-stripped identifier as
        # the record's DOI, and a reviewer comparing it against the publisher
        # page would find it does not match.
        members = session.execute(
            select(
                ReviewRecordVersion.review_record_id,
                ReviewRecordVersion.review_record_version_id,
                ReviewRecordVersion.normalized_doi,
                ReviewRecordVersion.normalized_pmid,
                ReviewRecordVersion.normalized_title,
                ReviewRecordVersion.publication_year,
                ReviewRecordVersion.title,
                ReviewRecordVersion.doi,
                ReviewRecordVersion.pmid,
            )
            .join(
                DuplicateClusterMember,
                DuplicateClusterMember.review_record_version_id == ReviewRecordVersion.review_record_version_id,
            )
            .where(DuplicateClusterMember.cluster_id == cluster.cluster_id)
            .order_by(ReviewRecordVersion.review_record_id)
        ).all()
        # `*row[:6]` then the three source fields the query already selects at
        # 6/7/8 — `canonical_presentation` offers them because they, not the
        # normalized forms, are what survivorship may choose between.
        candidates = [CandidateRecord(*row[:6], title=row[6], doi=row[7], pmid=row[8]) for row in members]
        resolution = current_resolution(session, cluster.cluster_id)
        out.append(
            {
                "cluster_id": str(cluster.cluster_id),
                "cluster_key": cluster.cluster_key,
                "member_count": cluster.member_count,
                "members": [
                    {
                        "review_record_id": str(row[0]),
                        "review_record_version_id": str(row[1]),
                        "title": row[6],
                        "doi": row[7],
                        "pmid": row[8],
                        "normalized_doi": row[2],
                        "normalized_pmid": row[3],
                        "publication_year": row[5],
                    }
                    for row in members
                ],
                "canonical_presentation": canonical_presentation(candidates),
                "current_resolution": resolution_summary(resolution) if resolution else None,
            }
        )
    return out


def resolution_chain(session: Session, review_id: uuid.UUID, cluster_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every resolution ever recorded for a cluster, oldest first.

    This is the artifact `01-…:456` asks for: after merge -> split -> merge the
    chain has three rows, each with its own actor and rationale, and no row was
    edited to produce it.

    `review_id` is not decoration. This used to filter on `cluster_id` alone,
    and row-level security is scoped to the TENANT, not to the review — so a
    caller holding review A could name a cluster belonging to review B in the
    path and read back B's rationales and `decided_by_subject_id`. Every sibling
    here (`resolve_cluster`, `list_clusters`, `get_export`) already checks the
    cluster against the review in the URL; this one was the omission.
    """
    cluster = session.get(DuplicateCluster, cluster_id)
    if cluster is None or cluster.review_id != review_id:
        # 404, matching `resolve_cluster`: a 403 would confirm the cluster
        # exists, and under reviewer blinding that confirmation is itself the
        # disclosure (`api.problem_details.NotFoundError`).
        raise NotFoundError("no such duplicate cluster for this review")

    rows = (
        session.execute(
            select(DuplicateResolution)
            .where(DuplicateResolution.cluster_id == cluster_id)
            .order_by(DuplicateResolution.decided_at)
        )
        .scalars()
        .all()
    )

    # The read path for `duplicate_survivorship_fields`. It had none — nothing in
    # `src/` selected the table, so the record of which value a lead chose, and
    # which original it came from, was written and never shown anywhere.
    #
    # It stays AUDIT, not runtime authority: computing a survivor's current
    # content by replaying these rows would be a derived `is_current` projection,
    # which this schema deliberately does not have. `review_record_versions` is
    # the authority; this is the evidence of the human choice that produced it.
    # One `IN` read, not one per resolution.
    survivorship: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if rows:
        for field in (
            session.execute(
                select(DuplicateSurvivorshipField)
                .where(DuplicateSurvivorshipField.resolution_id.in_([r.resolution_id for r in rows]))
                .order_by(DuplicateSurvivorshipField.resolution_id, DuplicateSurvivorshipField.field_name)
            )
            .scalars()
            .all()
        ):
            survivorship.setdefault(field.resolution_id, []).append(
                {
                    "field_name": field.field_name,
                    "value": (field.value or {}).get("value"),
                    "source_record_version_id": str(field.source_record_version_id),
                }
            )

    return [{**resolution_summary(r), "survivorship": survivorship.get(r.resolution_id, [])} for r in rows]
