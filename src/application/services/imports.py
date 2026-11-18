"""Corpus import and the sealed corpus version — `ER-002`.

The import is one transaction: batch, rows, records, versions. A partial import
is not a state this service can be in, because `identified_total` in the flow
ledger is derived from `import_rows`, and a batch that half-committed would make
the conservation invariant fail for a reason nobody could diagnose.

Worker death/retry creating no duplicate record (`01-…:422`) follows from
`import_batches.raw_payload_digest` plus **the unique constraint behind it**:
re-submitting the same bytes finds the existing batch and returns it rather than
importing twice, and a submission that arrives while the first is still
in flight loses `uq_import_batch_payload_digest` and replays the winner.

## The correction, and why it is written here

This paragraph used to say the digest "plus the transaction" was the guarantee.
That was false, and false in exactly the scenario it named. A transaction gives
atomicity, not mutual exclusion: the check at the top of `import_payload` is a
`SELECT` and then an `INSERT`, and at READ COMMITTED — the level this service
runs at — two workers take that `SELECT` before either commits, neither sees the
other, and both import. Until `0012` there was no uniqueness on `import_batches`
beyond its primary key; `\\d import_batches` showed the PK and nothing else.

Measured with two threads on two sessions
(`tests/integration/test_a_second_writer_cannot_mint_a_twin.py`): two batches for
one `(review_id, raw_payload_digest)`, so every record in the payload landed
twice. Nothing downstream disagreed — `identified_total` is derived from
`import_rows`, so the ledger, `GET /records`, the sealed corpus and the export all
reported the doubled corpus consistently — and the NEXT submission of those same
bytes hit `scalar_one_or_none()` on two rows, raised `MultipleResultsFound`, and
left the route emitting a bare `500 text/plain`, outside the problem+json contract
every other error in this service honours.

The retrying worker is not a hypothetical caller here: it is the one this
docstring promised safety to.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from actor import Actor
from api.problem_details import (
    EmptySourceNotDeclaredError,
    ForbiddenDiscriminatorError,
    NotFoundError,
    UnprocessableStateError,
    UnrecognizedPayloadError,
    ValidationFailedError,
)
from application.services import events
from application.services.reviews import require_review_lead_for, require_review_open
from domain.corpus import ORDERING_RULE, member_manifest_hash, order_members
from domain.policies.denylist import find_forbidden_keys
from domain.policies.normalization import normalize_doi, normalize_pmid, normalize_text
from infrastructure.imports.import_parser import (
    PARSERS,
    ImportTooLargeError,
    ParsedRecord,
    UnreadablePayloadError,
    UnsupportedFormatError,
    parse_discovery_session_export,
    payload_digest,
    unsupported_format_blocker,
)
from models.records import (
    CorpusVersion,
    CorpusVersionMember,
    ImportBatch,
    ImportRow,
    ReviewRecord,
    ReviewRecordVersion,
)

#: **A label, and only a label** — see :data:`~.reviews.PROTOCOL_SCHEMA_REF`, the
#: one remaining sibling in the same state. :data:`~.flow.LEDGER_SCHEMA_REF` was
#: the third and has LEFT it: `urn:datacompany:review-flow-ledger:1` was authored and
#: that constant followed, which is the sequence this one is still waiting on.
#: No `review-record` document exists, nothing validates a record against it, and it is
#: echoed to clients verbatim on the import response
#: (`api/routers/imports.py`). It stayed undocumented, which made it the easiest
#: of the three to mistake for a resolvable reference.
RECORD_SCHEMA_REF = "https://contracts.nonagentic.ai/evidence/review-record.v1.schema.json"


def _content_hash(payload: Any) -> str:
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(payload)


def guard_denylist(payload: Any) -> None:
    """`ER-002.7` — reject, never silently drop.

    Applied to the *parsed* body as well as the request envelope, because a
    forbidden discriminator inside a canonical-JSON record is the case that
    actually matters: that is where a caller would assert a regulatory type.
    """
    hits = find_forbidden_keys(payload)
    if hits:
        raise ForbiddenDiscriminatorError(
            "the request carries a guarded pre-ADR regulatory discriminator; "
            "ADR-DATACOMPANY-REGULATORY-MODEL is not accepted, so this cannot be imported as a "
            "canonical regulatory entity. A regulatory source document imports as an ordinary "
            "record with record_type='report'.",
            forbidden_paths=hits,
        )


def import_payload(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    source_kind: str,
    payload: bytes,
    declared_source_ref: dict | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Import a batch into a review — **lead only** (`ER-002`).

    ## Why `require_human` was not enough

    This asked for a person and stopped, so any reviewer authenticated into the
    tenant could push records into any review in it. `load_review` in the router
    is not a second check: it proves the review is visible to the caller's
    tenant, which every reviewer's is.

    That was not a theoretical hole. In the studio a reviewer joins somebody
    else's review carrying their own unrelated Discover results, and nothing on
    the server refuses the import — the only thing standing in the way was a
    checkbox in the frontend, and a UI convention is not an authority boundary.

    It matters more than "somebody added rows". The sealed corpus is the anchor
    every later claim hangs off: `identified_total` in the flow ledger is derived
    from `import_rows`, the corpus manifest hash names the exact membership a
    screening round ran against, and the export's manifest cites both. Records
    arriving from outside the review's own provenance make all three describe a
    corpus the lead never assembled, and every one of them still reports itself
    as sealed and verified. There is no repair through this service either:
    `import_batches` and `import_rows` are append-only *by construction* — nothing
    in the API deletes or edits either — so the wrong rows can only be superseded
    by a new corpus version that a reader has to notice.

    (Append-only by construction, and NOT by trigger. Measured on `pg_trigger`
    while writing `0012`: neither table carries one, unlike `inclusion_exports`,
    `screening_decisions`, `review_records` and the rest of the history tables. So
    somebody with the database credential CAN delete these rows, which matters
    only in the direction this paragraph is not about — it is the reason `0012`'s
    docstring can describe unwinding a duplicated import at all. The sentence used
    to say "are append-only" flatly, which read as a database guarantee it does
    not have.)

    The gate is `require_review_lead_for`, matching assignment, unblinding and
    adjudication. The CREATOR still imports into their own review with no realm
    role — that is the normal first run, and the case `require_review_lead`
    exists for.
    """
    subject = require_review_lead_for(session, actor, review_id, "importing records")
    require_review_open(session, review_id, "importing records")
    digest = payload_digest(payload)

    existing = session.execute(
        select(ImportBatch).where(ImportBatch.review_id == review_id, ImportBatch.raw_payload_digest == digest)
    ).scalar_one_or_none()
    if existing is not None:
        # Identical bytes into the same review are the same import. This is what
        # makes worker death and client retry safe without a lease.
        return _batch_summary(session, existing, replayed=True)

    provenance: dict[str, Any] | None = declared_source_ref
    try:
        if source_kind == "discovery_session":
            parsed, discovered = parse_discovery_session_export(payload)
            if discovered:
                # Pinned WHEN PRESENT. `20b498e` on discovery-engine main added
                # per_source_counts/raw_total/deduped_total; older exports and
                # every file format carry none of it, and blocking on it would
                # make the common path depend on the rare one.
                provenance = {**(provenance or {}), **discovered}
        elif source_kind in PARSERS:
            parsed = PARSERS[source_kind](payload)
        else:
            raise unsupported_format_blocker(source_kind)
    except UnreadablePayloadError as exc:
        # Refused, not written: these bytes are not the declared format.
        raise UnrecognizedPayloadError(str(exc)) from exc
    except UnsupportedFormatError as exc:
        raise UnprocessableStateError(str(exc), blocker=getattr(exc, "blocker", None)) from exc
    except ImportTooLargeError as exc:
        raise ValidationFailedError(str(exc)) from exc

    if not parsed and not allow_empty:
        # The payload WAS the declared format and held no records. Refused
        # anyway, because `201 accepted=0 quarantined=0` is the shape of a
        # successful import: `identified_total` is derived from `import_rows`,
        # so the reviewer goes on to seal a corpus, screen it and export a
        # PRISMA count that is internally consistent and describes a corpus that
        # was never there. Nothing downstream can disagree with it.
        #
        # The refusal lives here rather than in the parsers because emptiness is
        # not a parse failure. `{"records": []}`, `{"results": []}` and a CSV
        # header row with no data rows are all well-formed statements that a
        # search found nothing, and a reviewer must be able to record that — with
        # `allow_empty=true`, which makes it an assertion by a named human
        # instead of an accident nobody sees.
        raise EmptySourceNotDeclaredError(
            f"the {source_kind} payload parsed cleanly and contained no records. An import of zero "
            "records is indistinguishable from a successful import once it is written, and the "
            "review's identified_total is derived from it. If this search genuinely returned "
            "nothing, re-submit with allow_empty=true to record that; otherwise check the export."
        )

    # The denylist runs over what was actually parsed, so a guarded key nested
    # inside a record body is caught, not only one in the envelope.
    guard_denylist([p.raw for p in parsed])

    # THE DIGEST IS NOT THE GUARANTEE — THE CONSTRAINT BEHIND IT IS.
    #
    # The SELECT at the top of this function is a check-then-act, and this service
    # runs at READ COMMITTED: two workers take it before either commits, both see
    # nothing, and both import. Until `0012` there was no uniqueness on
    # `import_batches` beyond its primary key — verified against the live
    # database, `\d import_batches` showed the PK and nothing else — so the
    # module docstring's "the digest plus the transaction IS the guarantee" was
    # false in exactly the scenario it named: worker death and retry.
    #
    # Reproduced with two threads on two sessions
    # (`test_two_concurrent_imports_of_one_payload_leave_one_batch`): two batches
    # for one `(review_id, raw_payload_digest)`, and with them every record in the
    # payload twice. That is the worst of the three races this pattern closes,
    # because nothing downstream disagrees with it: `identified_total` is derived
    # from `import_rows`, so the ledger, `GET /records`, the sealed corpus and the
    # export all report the doubled corpus consistently. And the NEXT import of
    # those same bytes reaches `scalar_one_or_none()` on two rows, raises
    # `MultipleResultsFound`, and leaves the route emitting a bare
    # `500 text/plain` — outside the problem+json contract every other error in
    # this service honours.
    #
    # ON CONFLICT DO NOTHING on `uq_import_batch_payload_digest`, then re-select:
    # the same shape as `flow.current_flow_revision` and
    # `screening._maybe_open_conflict`. The loser writes NO batch and therefore no
    # rows, no records and no versions — the `return` below the re-select is taken
    # before any of that is built — and it answers with the winner's batch as a
    # replay, which is what the caller wanted and what a retrying worker needs.
    #
    # Both halves are load-bearing. The constraint alone would turn the loser's
    # insert into an `IntegrityError`, which `_integrity_handler` dresses as
    # `409 state_conflict`: a retrying worker would be told its retry conflicted
    # with the state, which is the one answer it cannot act on.
    batch_id = uuid.uuid4()
    inserted = session.execute(
        pg_insert(ImportBatch)
        .values(
            import_batch_id=batch_id,
            review_id=review_id,
            tenant_id=actor.tenant_id,
            source_kind=source_kind,
            source_ref=provenance,
            raw_payload_digest=digest,
            submitted_by_subject_id=subject,
        )
        .on_conflict_do_nothing(constraint="uq_import_batch_payload_digest")
        .returning(ImportBatch.import_batch_id)
    ).scalar_one_or_none()
    session.flush()
    if inserted is None:
        # Lost the race. The winner's batch is committed by the time this
        # statement takes its own READ COMMITTED snapshot, and it is the batch
        # this call was trying to create.
        winner = session.execute(
            select(ImportBatch).where(ImportBatch.review_id == review_id, ImportBatch.raw_payload_digest == digest)
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover - the insert above either wrote it or lost to a writer who did
            raise UnprocessableStateError("the import batch disappeared while being written")
        return _batch_summary(session, winner, replayed=True)

    batch = session.get(ImportBatch, batch_id)
    if batch is None:  # pragma: no cover - inserted by this transaction one statement ago
        raise UnprocessableStateError("the import batch disappeared while being written")

    accepted = quarantined = 0
    for index, record in enumerate(parsed):
        if record.quarantine_reason:
            session.add(
                ImportRow(
                    import_row_id=uuid.uuid4(),
                    import_batch_id=batch.import_batch_id,
                    review_id=review_id,
                    tenant_id=actor.tenant_id,
                    row_index=index,
                    raw_representation=record.raw,
                    status="quarantined",
                    quarantine_reason=record.quarantine_reason,
                )
            )
            quarantined += 1
            continue

        row_id = uuid.uuid4()
        record_id = uuid.uuid4()
        session.add(
            ImportRow(
                import_row_id=row_id,
                import_batch_id=batch.import_batch_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                row_index=index,
                raw_representation=record.raw,
                status="accepted",
                review_record_id=record_id,
            )
        )
        session.add(ReviewRecord(review_record_id=record_id, review_id=review_id, tenant_id=actor.tenant_id))
        session.add(_record_version(actor, review_id, record_id, row_id, record))
        accepted += 1

    batch.accepted_count = accepted
    batch.quarantined_count = quarantined
    batch.rejected_count = 0
    session.flush()
    events.emit_import_completed(session, batch)
    return _batch_summary(session, batch, replayed=False)


#: The fields that make up a record's hashed content view, and therefore its
#: `content_hash`. RFC 8785 sorts keys, so the ORDER here is cosmetic — the KEY
#: SET is the load-bearing thing, and it now has exactly one definition. Two
#: writers building this dict by hand would produce hashes that agreed until the
#: day one of them gained a field.
RECORD_CONTENT_FIELDS = (
    "record_type",
    "title",
    "abstract",
    "publication_year",
    "authors",
    "container_title",
    "doi",
    "pmid",
    "pmcid",
    "arxiv_id",
    "url",
)


def record_content(source: Any) -> dict[str, Any]:
    """The hashed content view of a record.

    `ParsedRecord` (from an import) and `ReviewRecordVersion` (from the database)
    both satisfy it by carrying the same attribute names, which is what lets the
    import path and the survivorship path hash the same shape.
    """
    return {name: getattr(source, name) for name in RECORD_CONTENT_FIELDS}


def _field_provenance(parsed: ParsedRecord, row_id: uuid.UUID) -> dict[str, Any]:
    """Where each normalized value came from, as measured by the parser.

    `parsed.field_sources` is empty for the adapters that read one spelling per
    identifier (RIS, BibTeX, CSV), and the field's own name is then the honest
    answer. Where a JSON payload nested its identifiers, it names the key that
    actually produced the value — `external_ids.pubmed_id`, not `pmid`.
    """
    provenance: dict[str, Any] = {
        "normalized_title": "title",
        "normalized_doi": parsed.field_sources.get("doi", "doi"),
        "normalized_pmid": parsed.field_sources.get("pmid", "pmid"),
        "source_row": str(row_id),
    }
    if parsed.unusable_sources:
        # A source key that carried something unusable was skipped, not refused
        # — the record is here and complete. Recording it keeps "this row has no
        # DOI" distinguishable from "this row's DOI was unreadable", which is
        # otherwise a silent difference in an audit trail.
        provenance["unusable_sources"] = list(parsed.unusable_sources)
    return provenance


def _record_version(
    actor: Actor, review_id: uuid.UUID, record_id: uuid.UUID, row_id: uuid.UUID, parsed: ParsedRecord
) -> ReviewRecordVersion:
    normalized = parsed.normalized()
    content = record_content(parsed)
    return ReviewRecordVersion(
        review_record_version_id=uuid.uuid4(),
        review_record_id=record_id,
        review_id=review_id,
        tenant_id=actor.tenant_id,
        version_number=1,
        schema_ref=RECORD_SCHEMA_REF,
        content_hash=_content_hash(content),
        record_type=parsed.record_type,
        title=parsed.title,
        abstract=parsed.abstract,
        publication_year=parsed.publication_year,
        authors=parsed.authors,
        container_title=parsed.container_title,
        doi=parsed.doi,
        pmid=parsed.pmid,
        pmcid=parsed.pmcid,
        arxiv_id=parsed.arxiv_id,
        url=parsed.url,
        normalized_title=normalized["normalized_title"],
        normalized_doi=normalized["normalized_doi"],
        normalized_pmid=normalized["normalized_pmid"],
        # `ER-002.4` — which source field each normalized value came from, so a
        # surprising dedup match is traceable to the field that produced it.
        #
        # Read from the parser, never assumed. An identifier has more than one
        # spelling in the wild — a PMID arrives as `pmid`, as
        # `external_ids.pmid` or as `external_ids.pubmed_id` depending on which
        # source found the paper — and only the parser knows which one this row
        # actually used. Hardcoding `"pmid"` wrote a *false* statement into an
        # append-only audit column, in the one field whose whole job is making a
        # surprising merge traceable to the bytes behind it.
        field_provenance=_field_provenance(parsed, row_id),
        source_import_row_id=row_id,
        created_by_subject_id=actor.subject_id or "",
    )


def _batch_summary(session: Session, batch: ImportBatch, *, replayed: bool) -> dict[str, Any]:
    return {
        "import_batch_id": str(batch.import_batch_id),
        "review_id": str(batch.review_id),
        "source_kind": batch.source_kind,
        "raw_payload_digest": batch.raw_payload_digest,
        "accepted_count": batch.accepted_count,
        "quarantined_count": batch.quarantined_count,
        "rejected_count": batch.rejected_count,
        # Present only when the source supplied it. `null` here is honest:
        # "we were not told", not "zero".
        "source_ref": batch.source_ref,
        "replayed": replayed,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


def _tail_subquery(review_id: uuid.UUID, record_ids: Collection[uuid.UUID] | None = None) -> Any:
    """`review_record_id -> max(version_number)`, optionally for a named subset.

    Restricting the groups cannot change any surviving group's max, so pushing
    `record_ids` into the subquery is exact rather than an approximation — and it
    lets the planner use `ix_record_versions_record` instead of grouping the
    whole review.
    """
    stmt = (
        select(
            ReviewRecordVersion.review_record_id.label("rid"),
            func.max(ReviewRecordVersion.version_number).label("v"),
        )
        .where(ReviewRecordVersion.review_id == review_id)
        .group_by(ReviewRecordVersion.review_record_id)
    )
    if record_ids is not None:
        stmt = stmt.where(ReviewRecordVersion.review_record_id.in_(record_ids))
    return stmt.subquery()


def current_record_version_ids(session: Session, review_id: uuid.UUID) -> list[uuid.UUID]:
    """The tail of every record's version chain, in this review.

    "Tail" is `version_number = max(version_number)` per record. Using the
    supersession chain instead would give the same answer and cost a recursive
    CTE; the unique `(review_record_id, version_number)` makes the max form
    exact.
    """
    subquery = _tail_subquery(review_id)
    rows = session.execute(
        select(ReviewRecordVersion.review_record_version_id)
        .join(
            subquery,
            (ReviewRecordVersion.review_record_id == subquery.c.rid)
            & (ReviewRecordVersion.version_number == subquery.c.v),
        )
        .where(ReviewRecordVersion.review_id == review_id)
    ).all()
    return [r[0] for r in rows]


def current_record_versions(
    session: Session, review_id: uuid.UUID, record_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, ReviewRecordVersion]:
    """Tail version per record, for a NAMED subset. One statement, keyed by record.

    The sibling of :func:`current_record_version_ids` for callers that need the
    rows rather than the ids. It exists because the obvious spelling —

        {v.review_record_id: v for v in session.execute(
            select(ReviewRecordVersion).where(review_record_id.in_(ids))).scalars()}

    — is **last-row-wins by heap order**, not "the current version". That is
    exactly what `create_export` did, and while a record has only one version the
    two are accidentally the same answer. They stop being the same the moment
    anything writes a second version, and the caller that noticed would be the
    export seal, signing a superseded `content_hash` and then verifying against
    it happily.
    """
    if not record_ids:
        return {}
    subquery = _tail_subquery(review_id, record_ids)
    rows = (
        session.execute(
            select(ReviewRecordVersion)
            .join(
                subquery,
                (ReviewRecordVersion.review_record_id == subquery.c.rid)
                & (ReviewRecordVersion.version_number == subquery.c.v),
            )
            .where(
                ReviewRecordVersion.review_id == review_id,
                ReviewRecordVersion.review_record_id.in_(record_ids),
            )
        )
        .scalars()
        .all()
    )
    return {v.review_record_id: v for v in rows}


#: The fields a merge's survivorship may choose between. Identity, tenancy,
#: lineage and provenance are NOT choosable.
#:
#: This is the security half of applying survivorship. While the chosen values
#: were only recorded in `duplicate_survivorship_fields` and read by nobody,
#: `getattr(version, field_name, None)` with no validation was inert. Applied, it
#: becomes a caller-chosen write target on `review_record_versions`: a
#: `field_name` of `"review_id"` would mint a version of one review's record
#: carrying another review's id, and RLS on that table is scoped to the TENANT,
#: not the review, so the `WITH CHECK` predicate would not refuse it.
#:
#: Enforced here AND as `ck_survivorship_field_name` in `0007`. `59bc339` is the
#: reason for both: a closed vocabulary that lives only in the migration is not
#: enforced where it is used, and one that lives only in Python is not a
#: constraint.
SURVIVABLE_FIELDS = frozenset(RECORD_CONTENT_FIELDS)


def apply_survivorship(
    session: Session,
    actor: Actor,
    review_id: uuid.UUID,
    *,
    survivor_record_id: uuid.UUID,
    sources: dict[str, ReviewRecordVersion],
    resolution_id: uuid.UUID,
    cluster_id: uuid.UUID,
    subject: str,
) -> ReviewRecordVersion | None:
    """Write the survivor's new version after a merge chose its fields (`ER-003.4`).

    Called only from `deduplication.resolve_cluster`, and only for a `merge`.
    Returns the new version, or `None` when the choice changed nothing.

    ## Why this is an INSERT and lives in this module

    `review_record_versions` carries a `review_record_versions_immutable` trigger
    (`0003`) that refuses UPDATE and DELETE, so "apply" can only ever mean
    appending a superseding version — which is also the design: there is no
    `is_current` column anywhere in this schema, and "current" is the tail of a
    supersession chain. It lives here because `imports._record_version` was the
    only writer of a version row and owns `RECORD_SCHEMA_REF`, `record_content`
    and `_content_hash`; a second writer anywhere else would drift from it.

    It must NOT create a new `ReviewRecord`. That would raise `records_total`
    while `identified_total` (derived from `import_rows`) stayed put, and
    `check_conservation` would then raise on every subsequent `/flow` read,
    including the one `create_export` depends on.

    ## Why the row is locked

    `version_number = previous + 1` computed from a read is racy, and two clusters
    from two dedup runs can name the same survivor, so two concurrent merges on
    one record is reachable. `uq_record_version_number` and
    `uq_record_version_supersession_linear` (both `0003`) make a fork structurally
    impossible either way — the question is only whether the loser gets a usable
    outcome or a 409 on a legitimate request.

    `SELECT ... FOR UPDATE` on `review_records` serialises them: the loser blocks,
    re-reads the tail (now v2) and writes v3 superseding it, so both leads' choices
    land in one linear attributable chain.

    **That is the two-CLUSTER race.** On the same cluster it never gets this far:
    `uq_resolution_one_head_per_cluster` refuses the second resolution first, so
    the second caller takes a `409` before `apply_survivorship` is reached —
    measured, A `200` / B `409`, `version_count: 2`, chain linear. An earlier
    version of this paragraph described the v3 outcome as if it were the common
    case. `tests/integration/test_record_version_race.py` drives the two-cluster
    shape deliberately, and removing the lock turns it red with
    `UniqueViolation on uq_record_version_number`. `ON CONFLICT DO NOTHING` would be wrong
    here — that idiom works in `current_flow_revision` because the loser's row is
    the row it wanted, and that is false for survivorship, where accepting the
    winner's version would discard an attributed human decision in silence.

    `FOR UPDATE` is a row lock, not an UPDATE, so the immutability trigger on
    `review_records` does not fire.
    """
    unknown = sorted(set(sources) - SURVIVABLE_FIELDS)
    if unknown:
        raise UnprocessableStateError(
            f"survivorship cannot choose {', '.join(repr(f) for f in unknown)}: a survivor field is a "
            "choice between values a reviewer was shown, and identity, tenancy and provenance are not "
            "among them",
            allowed=sorted(SURVIVABLE_FIELDS),
            rejected=unknown,
        )

    session.execute(
        select(ReviewRecord.review_record_id)
        .where(ReviewRecord.review_record_id == survivor_record_id)
        .with_for_update()
    ).scalar_one_or_none()

    previous = session.execute(
        select(ReviewRecordVersion)
        .where(ReviewRecordVersion.review_record_id == survivor_record_id)
        .order_by(ReviewRecordVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous is None:
        raise UnprocessableStateError("the surviving record has no version to supersede")

    content = record_content(previous)
    for field_name, source in sources.items():
        content[field_name] = getattr(source, field_name)

    if content == record_content(previous):
        # The lead re-affirmed the survivor's own values. A version whose content
        # is byte-identical to its predecessor makes "which version is current?"
        # a distinction without a difference, and it would move the next corpus
        # manifest for no semantic change. `seal_corpus_version` refuses an
        # identical manifest for the same reason, one level up.
        return None

    version = ReviewRecordVersion(
        review_record_version_id=uuid.uuid4(),
        review_record_id=survivor_record_id,
        review_id=previous.review_id,
        tenant_id=actor.tenant_id,
        version_number=previous.version_number + 1,
        supersedes_version_id=previous.review_record_version_id,
        schema_ref=RECORD_SCHEMA_REF,
        content_hash=_content_hash(content),
        # Recomputed, never copied. Copying `normalized_title` from a source that
        # also supplied `title` happens to agree; the day someone survives `doi`
        # alone, a copied `normalized_doi` and a derived one diverge, and the
        # dedup baseline stops being a deterministic function of stored bytes.
        normalized_title=normalize_text(content["title"]),
        normalized_doi=normalize_doi(content["doi"]),
        normalized_pmid=normalize_pmid(content["pmid"]),
        field_provenance={
            "normalized_title": "title",
            "normalized_doi": "doi",
            "normalized_pmid": "pmid",
            "source_row": str(previous.source_import_row_id) if previous.source_import_row_id else None,
            # THE read path for a survivorship choice, on the artifact the export
            # actually seals — field -> the original record version it came from.
            # `01-…:453` ("every survivor field resolves to an original
            # record/version") made readable, rather than left in a side table
            # nothing joins to.
            "derived_from": {
                "kind": "duplicate_merge_survivorship",
                "cluster_id": str(cluster_id),
                "resolution_id": str(resolution_id),
                "supersedes_version_id": str(previous.review_record_version_id),
                "fields": {name: str(src.review_record_version_id) for name, src in sorted(sources.items())},
            },
        },
        # Carried forward, not nulled. The FK to `import_rows` is what ties this
        # record to the bytes that identified it, and `ck_import_row_outcome_consistent`
        # guarantees exactly one accepted row per record. Per-field truth lives in
        # `derived_from` above.
        source_import_row_id=previous.source_import_row_id,
        created_by_subject_id=subject,
        **{name: content[name] for name in RECORD_CONTENT_FIELDS},
    )
    session.add(version)
    session.flush()
    return version


def corpus_member_versions(
    session: Session, corpus_version_id: uuid.UUID, record_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, ReviewRecordVersion]:
    """The version of each named record AS SEALED INTO `corpus_version_id`.

    The sibling of :func:`current_record_versions` for callers that are making a
    statement ABOUT a corpus rather than about the review's present state. One
    statement, joined through `corpus_version_members`, which stores the exact
    `review_record_version_id` frozen at seal time.

    The two disagree the moment anything writes a second version — which is what
    `apply_survivorship` does. `create_export` needs THIS one: its manifest cites
    `corpus_version_id` and `member_manifest_hash`, so naming a version the
    corpus does not contain makes the sealed artifact contradict its own
    envelope, and `verify_export` cannot see it (it re-hashes the stored payload
    and has no membership check).

    A record with no row here is genuinely not in that corpus. The caller must
    refuse rather than drop it — `corpus_version_members` is immutable and
    verified by a deferred trigger at COMMIT, so an absent member is a fact about
    the corpus, never a read that went wrong.
    """
    if not record_ids:
        return {}
    rows = (
        session.execute(
            select(ReviewRecordVersion)
            .join(
                CorpusVersionMember,
                CorpusVersionMember.review_record_version_id == ReviewRecordVersion.review_record_version_id,
            )
            .where(
                CorpusVersionMember.corpus_version_id == corpus_version_id,
                ReviewRecordVersion.review_record_id.in_(record_ids),
            )
        )
        .scalars()
        .all()
    )
    return {v.review_record_id: v for v in rows}


def seal_corpus_version(session: Session, actor: Actor, review_id: uuid.UUID) -> dict[str, Any]:
    """Seal the current record set into a new immutable `CorpusVersion`. **Lead only.**

    Members, count, ordering rule and manifest hash are written in one
    transaction and re-derived by a deferred trigger at commit. If the trigger
    disagrees the transaction is refused — which is `ER-V011` / `DM-AT-05` as a
    constraint rather than as a test.

    The authority check is the same one `import_payload` documents, and it is
    needed independently rather than as symmetry: sealing takes no input at all,
    so a non-lead who could only seal — never import — could still mint a new
    corpus version over whatever records happen to be present and move the
    membership every subsequent assignment, decision and export is anchored to.
    Supersession makes that visible in the chain; it does not make it authorised.
    """
    subject = require_review_lead_for(session, actor, review_id, "sealing a corpus version")
    require_review_open(session, review_id, "sealing a corpus version")
    version_ids = current_record_version_ids(session, review_id)
    if not version_ids:
        raise UnprocessableStateError("this review has no records to seal")

    previous = session.execute(
        select(CorpusVersion)
        .where(CorpusVersion.review_id == review_id)
        .order_by(CorpusVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()

    ordered = order_members(version_ids)
    manifest = member_manifest_hash(version_ids)
    if previous is not None and previous.member_manifest_hash == manifest:
        # Identical membership is the same corpus. Minting a second version with
        # the same manifest would make "which corpus was this run against?"
        # ambiguous for no gain.
        return corpus_summary(previous, replayed=True)

    corpus = CorpusVersion(
        corpus_version_id=uuid.uuid4(),
        review_id=review_id,
        tenant_id=actor.tenant_id,
        version_number=(previous.version_number + 1) if previous else 1,
        supersedes_version_id=previous.corpus_version_id if previous else None,
        member_count=len(ordered),
        member_ordering_rule=ORDERING_RULE,
        member_manifest_hash=manifest,
        created_by_subject_id=subject,
    )
    session.add(corpus)
    session.flush()

    by_version = {
        str(v_id): r_id
        for v_id, r_id in session.execute(
            select(ReviewRecordVersion.review_record_version_id, ReviewRecordVersion.review_record_id).where(
                ReviewRecordVersion.review_record_version_id.in_(version_ids)
            )
        ).all()
    }
    for ordinal, version_id in enumerate(ordered, start=1):
        session.add(
            CorpusVersionMember(
                corpus_version_id=corpus.corpus_version_id,
                review_id=review_id,
                tenant_id=actor.tenant_id,
                review_record_id=by_version[version_id],
                review_record_version_id=uuid.UUID(version_id),
                member_ordinal=ordinal,
            )
        )
    session.flush()
    return corpus_summary(corpus, replayed=False)


def corpus_summary(corpus: CorpusVersion, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "corpus_version_id": str(corpus.corpus_version_id),
        "review_id": str(corpus.review_id),
        "version_number": corpus.version_number,
        "member_count": corpus.member_count,
        "member_ordering_rule": corpus.member_ordering_rule,
        "member_manifest_hash": corpus.member_manifest_hash,
        "supersedes_version_id": str(corpus.supersedes_version_id) if corpus.supersedes_version_id else None,
        "replayed": replayed,
        "created_at": corpus.created_at.isoformat() if corpus.created_at else None,
    }


def latest_corpus(session: Session, review_id: uuid.UUID) -> CorpusVersion:
    corpus = session.execute(
        select(CorpusVersion)
        .where(CorpusVersion.review_id == review_id)
        .order_by(CorpusVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if corpus is None:
        raise NotFoundError("this review has no sealed corpus version")
    return corpus
