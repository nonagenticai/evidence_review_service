"""`flow:audit` — is the sealed evidence still supported by the history? Read-only.

## Why this is not `flow:rebuild`, and why `flow:rebuild` is not changed

`flow:rebuild` is a **cache-drift detector** and it is correct as one. It derives
the ledger from zero, compares the result against the stored projection, reports
`matches_previous_projection`, and — deliberately — calls
`compute_flow(persist=True)` so the drifted cache is corrected. A cache that has
drifted SHOULD be corrected, and saying so is the whole point of the operation.
Nothing here asks it to change.

It is simply not a tamper detector, for two reasons that are properties of its
design rather than bugs in it:

* **It is one-shot and self-erasing.** The comparison is against the newest
  projection, and the same call overwrites that projection with the new hash. So
  the FIRST rebuild after somebody rewrites an input reports
  `matches_previous_projection: false` — and destroys the only record that the
  two ever disagreed. Every rebuild after it reports `true`. A detector whose
  evidence is consumed by the act of detecting cannot be operated: whoever reads
  the response is the only person who will ever see the finding, and a scheduled
  rebuild swallows it before a human runs one.
* **It is a WRITE**, so it cannot be run by a reader, cannot be run against a
  closed review without adding history, and cannot be run repeatedly to
  corroborate a finding.

`:verify` does not cover it either: `verify_export` re-hashes the STORED payload
and compares it against itself, which its own docstring says. Rewrite
`import_rows` underneath a sealed export and `:verify` still answers
`outcome: "verified"` — correctly, because the artefact is intact. It is the
world the artefact describes that moved.

## What this reads, and what it will not say

The anchor is the **sealed export**, not the projection. `inclusion_exports` is
append-only and carries `canonical_counts_hash` INSIDE the sealed manifest, so it
is the one number in the system that a rebuild cannot overwrite. The audit derives
the ledger from the live history and compares it against every export the review
has sealed.

A divergence is not, on its own, an accusation. A review that was reopened and
screened further legitimately produces counts that no longer match a seal taken
before that work — and reporting *that* as tampering would be the same over-claim
`verify_export` goes to some length to avoid when it separates "unverifiable"
from "tampered". So the audit asks a second question of every divergence: **has
anything been APPENDED since the seal?**

Every input `flow._collect` reads is append-only after `0015`, so:

* counts differ **and** new history exists since the seal → ``diverged_after_new_history``.
  The review moved on. Not a finding.
* counts differ **and** nothing was appended since the seal →
  ``inputs_rewritten``. There is no legitimate way for a derivation over
  append-only tables to change while nothing was appended. This is the finding.
* the ledger cannot be derived at all (`ConservationError`) → ``underivable``.
  Deleting rows out of the middle of a duplicate cluster produces exactly this,
  and it is reported rather than raised: a 500 from an audit endpoint is
  indistinguishable from an audit endpoint that is broken.

`duplicate_cluster_members` carries no timestamp of its own and is not in the
census below. It does not need to be: a member row exists only inside a cluster
(`duplicate_cluster_members_cluster_id_fkey`, `ON DELETE RESTRICT`) and
`deduplication.create_run` writes both in one transaction, so
`duplicate_clusters.created_at` bounds it.

## "Persists nothing" is a property, not an intention

This module deliberately does NOT call `compute_flow`, and not only because of
`persist`. `compute_flow(persist=False)` still calls `current_flow_revision`,
which INSERTS a `flow_revisions` row on first use — `test_flow_revision_race`
pins exactly that. An audit that opened a revision would write to the review it
is auditing, on a closed review, from a reader's request.

So it collects and derives directly, and then asserts that the session holds no
pending write. That assertion is not decoration: `_collect` belongs to another
module and a future edit to it that starts writing would otherwise make this
endpoint a silent writer. `persisted: false` in the response body is the value
that assertion guarantees.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from actor import Actor
from application.services.flow import _collect, _reading_every_reviewers_work
from application.services.reviews import require_review_lead_for
from domain.policies.flow_derivation import ConservationError, canonical_counts_hash, derive
from log_config import get_logger
from models.screening import FlowProjection, InclusionExport

logger = get_logger(__name__)

#: `table -> the column that records when a row was appended`, verified against
#: `information_schema.columns` on a database at `0015` rather than recalled.
#: These are every table `flow._collect` derives from that can gain a row, and
#: the census over them is what separates "the review moved on" from "somebody
#: rewrote the inputs".
#:
#: Written out rather than reflected: a table added to `_collect` later must make
#: this list WRONG in the loud direction. An omission here can only ever turn a
#: legitimate divergence into an `inputs_rewritten` finding — a false alarm a
#: human investigates — and never the reverse, which would be a tamper reported
#: as normal progress.
APPEND_CENSUS: dict[str, str] = {
    "import_rows": "created_at",
    "review_record_versions": "created_at",
    "duplicate_clusters": "created_at",
    "duplicate_resolutions": "decided_at",
    "screening_decisions": "decided_at",
    "screening_conflicts": "opened_at",
    "screening_adjudications": "decided_at",
    "review_stages": "created_at",
}

AGREES = "agrees"
DIVERGED_AFTER_NEW_HISTORY = "diverged_after_new_history"
INPUTS_REWRITTEN = "inputs_rewritten"
UNDERIVABLE = "underivable"


def _appends_since(session: Session, review_id: uuid.UUID, moment: Any) -> dict[str, int]:
    """How many rows each derivation input has gained since `moment`.

    One statement per table and no join: these are small, indexed-by-review
    tables and an audit runs on demand, not on every read.

    Run under `_reading_every_reviewers_work` for the same reason `_collect`
    holds it, and it is not a nicety. `screening_decisions`'s policy keys
    visibility off `app.actor_role`, and a lead who holds the seat by CREATING
    the review carries no realm `lead` role — so an unelevated count would return
    0 decisions on exactly those reviews. Zero appends is the discriminator that
    decides `inputs_rewritten`, so the unelevated version would accuse a review
    of tampering for the crime of having been screened after its export. The
    elevation is transaction-scoped and restored in `finally` by the helper.
    """
    census: dict[str, int] = {}
    with _reading_every_reviewers_work(session):
        for table, column in APPEND_CENSUS.items():
            census[table] = int(
                session.execute(
                    # `table` and `column` are literals from `APPEND_CENSUS`,
                    # never caller input; `:r` and `:t` are bound.
                    text(f"SELECT count(*) FROM {table} WHERE review_id = :r AND {column} > :t"),  # nosec B608
                    {"r": review_id, "t": moment},
                ).scalar_one()
            )
    return census


def audit_flow(session: Session, actor: Actor, review_id: uuid.UUID) -> dict[str, Any]:
    """Re-derive the ledger and hold it against every seal. **Lead only.** Writes nothing.

    The gate is held here rather than only in the router, for the reason
    `require_review_lead_for`'s own docstring gives and `rebuild_flow` repeats: a
    check written in a router looks identical to no check at all when the service
    function is read. It is the same gate `flow:rebuild` and `GET /flow` hold —
    the derivation counts every reviewer's decisions, so the answer is a lead's.
    """
    require_review_lead_for(session, actor, review_id, "auditing the flow ledger")

    counts: dict[str, Any] | None = None
    digest: str | None = None
    underivable_reason: str | None = None
    try:
        counts = derive(_collect(session, review_id))["counts"]
        digest = canonical_counts_hash(counts)
    except ConservationError as exc:
        # REPORTED, NOT RAISED. `_collect` fails closed when a duplicate group
        # has lost every member — which is precisely what deleting rows out of
        # `duplicate_cluster_members` produces — and an audit that answers 500
        # to the state it exists to detect is indistinguishable from an audit
        # that is broken.
        underivable_reason = str(exc)

    exports = list(
        session.execute(
            select(InclusionExport).where(InclusionExport.review_id == review_id).order_by(InclusionExport.created_at)
        )
        .scalars()
        .all()
    )

    findings: list[dict[str, Any]] = []
    for export in exports:
        manifest = export.payload.get("manifest", {}) if isinstance(export.payload, dict) else {}
        sealed_hash = manifest.get("canonical_counts_hash")
        sealed_counts = manifest.get("counts") or {}

        if underivable_reason is not None:
            outcome = UNDERIVABLE
            appended: dict[str, int] = {}
        elif sealed_hash == digest:
            outcome = AGREES
            appended = {}
        else:
            appended = _appends_since(session, review_id, export.created_at)
            outcome = DIVERGED_AFTER_NEW_HISTORY if any(appended.values()) else INPUTS_REWRITTEN

        findings.append(
            {
                "export_id": str(export.export_id),
                "sealed_at": export.created_at.isoformat() if export.created_at else None,
                "outcome": outcome,
                "sealed_canonical_counts_hash": sealed_hash,
                "recomputed_canonical_counts_hash": digest,
                "sealed_counts": sealed_counts,
                "recomputed_counts": counts,
                # The count keys that actually moved. A hash says "something
                # differs" and nothing else; the investigation starts from which
                # bucket, and deriving it here means nobody has to diff two JSON
                # blobs by eye to begin one.
                "differing_counts": (
                    sorted(
                        key
                        for key in set(sealed_counts) | set(counts or {})
                        if sealed_counts.get(key) != (counts or {}).get(key)
                    )
                    if outcome in (DIVERGED_AFTER_NEW_HISTORY, INPUTS_REWRITTEN)
                    else []
                ),
                "appended_since_seal": appended,
            }
        )

    # The projection is reported and is NOT the anchor: it is a cache, it is
    # writable, and `flow:rebuild` overwrites it. It is here so a reader can see
    # what `flow:rebuild` would have compared against, beside the seal that
    # cannot be overwritten.
    cached = session.execute(
        select(FlowProjection)
        .where(FlowProjection.review_id == review_id)
        .order_by(FlowProjection.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    tampered = [f for f in findings if f["outcome"] == INPUTS_REWRITTEN]
    if tampered or underivable_reason is not None:
        logger.warning(
            "flow_audit.finding",
            review_id=str(review_id),
            rewritten_exports=[f["export_id"] for f in tampered],
            underivable=underivable_reason is not None,
        )

    # THE GUARANTEE, ASSERTED RATHER THAN CLAIMED. `_collect` lives in another
    # module; if it ever starts writing, this endpoint becomes a silent writer on
    # a closed review and `persisted: false` below becomes a lie. Checked before
    # the body is built so the response can never carry a value that is already
    # false.
    pending = (len(session.new), len(session.dirty), len(session.deleted))
    if any(pending):
        raise AssertionError(
            f"flow:audit is read-only and its session holds pending writes {pending} "
            "(new, dirty, deleted); something it calls has started writing"
        )

    return {
        "review_id": str(review_id),
        "audited": True,
        # Asserted immediately above, not asserted by this literal.
        "persisted": False,
        "derivable": underivable_reason is None,
        "underivable_reason": underivable_reason,
        "recomputed_counts": counts,
        "recomputed_canonical_counts_hash": digest,
        "projection_canonical_counts_hash": cached.canonical_counts_hash if cached else None,
        "projection_computed_at": cached.computed_at.isoformat() if cached and cached.computed_at else None,
        # A cache disagreeing is drift, and `flow:rebuild` is the operation for
        # it. Reported here so the two answers can be read together.
        "projection_stale": bool(cached and digest is not None and cached.canonical_counts_hash != digest),
        "exports_audited": len(exports),
        "export_findings": findings,
        # The one field an alert should branch on. `false` with
        # `derivable: false` is not "all clear" — read `derivable` too, which is
        # why it is not folded into this boolean.
        "inputs_rewritten": bool(tampered),
    }
