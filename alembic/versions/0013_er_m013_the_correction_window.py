"""`review_stages` is append-only *once the stage set has been used*, not always.

`0011` made `review_stages` append-only with an unconditional `BEFORE UPDATE OR
DELETE` trigger and revoked `UPDATE, DELETE` from the runtime role. That was
right about what it was defending — since `#42` a `canonical_counts_hash` sealed
into an append-only export depends on `stage_order` — and it is left standing
here for every stage that has ever been used.

It was also the database half of a refusal whose application half was total.
`lock_protocol_version` compared `existing_orders & incoming_orders`, where
`incoming_orders` is `{1..N}` and `existing_orders` always contains 1, so the
intersection was non-empty for EVERY second protocol version of a review,
regardless of its stage keys. Measured end to end against the API:

    create v1 (stage named 'Titel/abstrakt')                 -> 201
    lock   v1                                                -> 200
    create v2 (typo fixed)                                   -> 201
    lock   v2                                                -> 409  stage_orders: [1]
    activate v2                                              -> 422
    create v3 (keys screening-alpha / screening-beta)        -> 201
    lock   v3                                                -> 409  stage_orders: [1]

A typo in a stage name, caught within seconds and before a single record had been
imported, therefore ended the review — and the 409's own advice, "start a new
review", is unavailable, because `uq_reviews_project_external_key` is
`(tenant_id, project_id, external_key)` and the new review cannot carry the
identity the old one already claimed.

## The shape: conditional, exactly like the trigger next door

`review_protocol_versions_locked_immutable` (`0002`) is the precedent and this
follows it deliberately: it permits the `UPDATE` while `locked_at IS NULL` and
refuses it afterwards, because "an unlocked draft is still being written; the
lock is the moment it becomes evidence". A stage set has the same two lives. The
moment it becomes evidence is not the INSERT, it is the first thing that
references it, and the references are enumerable: the only foreign keys into
`review_stages` are `screening_assignments.stage_id`,
`screening_decisions.stage_id` and `screening_conflicts.stage_id`, all three
`ON DELETE RESTRICT` (`0005`).

So a `DELETE` is permitted only while the review has

* zero `screening_assignments`,
* zero `screening_decisions`,
* zero `screening_conflicts`,
* zero `inclusion_exports`, and
* no `review_state_history` row with `to_state = 'active'`.

`inclusion_exports` and the activation are NOT implied by the other three and
were added after asking what each one leaves behind.

* An export of a review whose corpus is empty passes both of `create_export`'s
  negative controls — `not_yet_screened` is 0 and there are no conflicts — so a
  sealed, append-only artefact can exist for a review with no assignment and no
  decision. Its `canonical_counts_hash` covers counts only, so re-minting the
  stages would not falsify the hash; it would still silently change which stage
  set the sealed artefact was drawn under, which is a provenance claim the seal
  is supposed to fix. Refused rather than reasoned about.
* `:activate` names ONE protocol version as the criteria every decision in the
  review is judged against and records it in the append-only
  `review_state_history`. Re-minting the stage set afterwards changes what that
  recorded activation activated, with nothing anywhere saying so.

## Re-granting DELETE: what it costs, stated explicitly

It costs less than it looks, and the accounting is worth writing down because
`0011` bought two separate things and only one of them is being traded.

`0011` bought (a) a trigger that stops the OWNER too — migrations connect as the
owner, and `FORCE ROW LEVEL SECURITY` does not apply to DDL-level privilege — and
(b) a REVOKE that refuses the runtime role before the trigger is reached, so a
service bug surfaces as a permission error rather than a business rule.

**`UPDATE` keeps both halves.** It is still revoked and still refused
unconditionally by the trigger. Nothing in this change needs it: re-ordering a
stage in place is never a correction, it is a rewrite of a position other rows
were written against, and that is the exact attack `0011` measured.

**`DELETE` keeps only (a).** The runtime role gets the privilege back, so the
guarantee for deletes now rests entirely on the trigger. That is a real
reduction in defence depth and it is the price of the feature: a re-lock IS a
delete, performed by the runtime role, so no formulation of this fix can keep the
REVOKE. What the trigger still guarantees, for the owner and the runtime role
alike, is that anything holding the app credential can delete a stage row only
from a review that has never been activated, holds no assignment, no decision, no
conflict and no sealed export. On such a review there is nothing to falsify: no
decision was taken at that position, no export cites it, and no
`canonical_counts_hash` exists that depends on it. The rewrite `0011` measured —
two rows re-ordered under a sealed export, with `:verify` still answering
`verified` — remains impossible, because it requires an export to exist and the
trigger refuses every delete once one does.

The residual loss is the error MESSAGE for a service bug, which is now a
`restrict_violation` from the trigger rather than an `InsufficientPrivilege` from
the grant. That is a worse first line and the same outcome.

## The non-vacuity guard, and why it is not optional

Every table the trigger reads is `ENABLE` + `FORCE ROW LEVEL SECURITY` keyed on
`app.current_tenant` with `USING (tenant_id = current_setting(..., true))`. With
that GUC unbound the predicate is `tenant_id = NULL`, every `EXISTS` is false, and
a trigger that concluded "no work exists" from that would permit EVERY delete on
EVERY review. That is `0012`'s defect exactly — its duplicate pre-check returned
0 while two duplicates existed — and it fails in the safest-looking direction.

The trigger therefore refuses outright unless `app.current_tenant` is bound and
equals `OLD.tenant_id`. It is not enough that the row was visible: `review_stages`
own policy can be suspended by the owner (`ALTER TABLE ... NO FORCE`), which is
precisely how a migration reaches these rows, and the trigger must not treat
"I could see the row" as "I could see the review's work".

`screening_decisions` is a special case worth naming. Its `screening_decisions_blinded`
policy (`0008`) narrows further than tenant — a non-lead sees only their own rows
— so that one `EXISTS` can UNDER-report. It cannot mislead, for a structural
reason: `screening_decisions.assignment_id` is `NOT NULL` with a foreign key to
`screening_assignments`, so a decision cannot exist without an assignment, and the
assignments check reads through the tenant policy alone. The decisions clause is a
belt over braces and is kept because an under-reporting check can only ever add
refusals.

## The pre-existing-corruption report is a NOTICE, not a refusal

The count of reviews already holding two stages at one `stage_order` is taken and
reported, bracketed by `ALTER TABLE ... NO FORCE`/`FORCE` for the reason `0012`
documents at length: the owner is subject to FORCE, no migration binds
`app.current_tenant`, and an unbracketed `SELECT` returns 0 rows on every
database. A pre-check that reads through RLS is a pre-check of nothing.

It reports rather than raises, and that is a decision rather than an omission.
Such a review can only predate `710830e`; this migration neither creates nor
worsens it; `flow.py`'s `_review_stages` already refuses to derive it, so it is
fail-closed today. And raising would block the upgrade that installs the only
mechanism able to repair it — the DELETE this migration re-grants. A refusal that
withholds the remedy for the condition it refuses over is not a safety measure.

## The complete fix is a different change

Ordering `StagePosition` by the protocol's `version_number` first, then
`stage_order`, then `stage_key`, gives two stage sets a total order and lets the
lock guard be dropped entirely; `flow_derivation.py`'s own docstring names that as
"the change to make". That is the eventual answer and it is not this one: it
changes derivation semantics for every review in the system, including every
review already screened and every export already sealed against the current
ordering. This migration restores the correction window; that change would permit
amendment mid-review, which is a different question with a different blast radius.

Revision ID: 0013_er_m013_window
Revises: 0012_er_m012_replay_keys
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_er_m013_window"
down_revision = "0012_er_m012_replay_keys"
branch_labels = None
depends_on = None

APP_ROLE = "evidence_review_app"

#: `0011`'s wording, reused verbatim. It is the sentence that explains the
#: refusal, and two spellings of it would drift.
REASON = "a stage that ordered a recorded judgement cannot be re-ordered afterwards"

#: The conditional guard. `UPDATE` is refused unconditionally, as `0011` left it.
#: `DELETE` is refused unless the review is still inside the correction window.
CONDITIONAL_TRIGGER = f"""
CREATE OR REPLACE FUNCTION review_stages_append_only() RETURNS trigger AS $fn$
DECLARE
  bound_tenant text := current_setting('app.current_tenant', true);
BEGIN
  IF TG_OP <> 'DELETE' THEN
    RAISE EXCEPTION 'review_stages is append-only: {REASON}'
      USING ERRCODE = 'restrict_violation';
  END IF;

  -- NON-VACUITY. Every check below reads a FORCE ROW LEVEL SECURITY table keyed
  -- on app.current_tenant; unbound, they all answer "no work exists" and permit
  -- everything. Refuse rather than read nothing. See the module docstring.
  IF bound_tenant IS NULL OR bound_tenant IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION
      'review_stages is append-only: app.current_tenant is not bound to this row''s tenant, '
      'so whether this stage has been used cannot be determined'
      USING ERRCODE = 'restrict_violation';
  END IF;

  IF EXISTS (SELECT 1 FROM screening_assignments a WHERE a.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM screening_decisions d WHERE d.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM screening_conflicts c WHERE c.review_id = OLD.review_id)
     OR EXISTS (SELECT 1 FROM inclusion_exports e WHERE e.review_id = OLD.review_id)
     OR EXISTS (
          SELECT 1 FROM review_state_history h
           WHERE h.review_id = OLD.review_id AND h.to_state = 'active'
        )
  THEN
    RAISE EXCEPTION
      'review_stages is append-only once its stage set has been used: {REASON}. '
      'This review has been activated, or holds an assignment, decision, conflict or sealed export.'
      USING ERRCODE = 'restrict_violation';
  END IF;

  RETURN OLD;
END;
$fn$ LANGUAGE plpgsql;
"""

#: `0011`'s original, restored by `downgrade`. Written out rather than imported
#: from `0011`: a downgrade that reaches into another revision's module breaks
#: the moment that revision is squashed.
UNCONDITIONAL_TRIGGER = f"""
CREATE OR REPLACE FUNCTION review_stages_append_only() RETURNS trigger AS $fn$
BEGIN
  RAISE EXCEPTION 'review_stages is append-only: {REASON}'
    USING ERRCODE = 'restrict_violation';
END;
$fn$ LANGUAGE plpgsql;
"""


def _report_untotally_ordered_reviews() -> None:
    """Count reviews already holding two stages at one `stage_order`, and say so.

    Bracketed by `NO FORCE`/`FORCE` because the owner is subject to FORCE ROW
    LEVEL SECURITY and no migration binds `app.current_tenant` — unbracketed this
    returns 0 on every database, which is `0012`'s measured defect. Reports
    rather than raises; the docstring gives the argument.
    """
    bind = op.get_bind()
    bind.execute(sa.text("ALTER TABLE review_stages NO FORCE ROW LEVEL SECURITY"))
    try:
        tied = bind.execute(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT 1 FROM review_stages GROUP BY review_id, stage_order HAVING count(*) > 1"
                ") d"
            )
        ).scalar_one()
    finally:
        # Restored on BOTH paths, unlike `0012`'s — this statement cannot abort
        # the transaction (it raises nothing), so a `finally` here is reachable
        # rather than guaranteed to fail in a poisoned transaction.
        bind.execute(sa.text("ALTER TABLE review_stages FORCE ROW LEVEL SECURITY"))
    if tied:
        print(  # noqa: T201 - the migration log is where an operator will look
            f"0013: WARNING — {tied} (review_id, stage_order) group(s) hold more than one stage. "
            "These reviews predate the lock guard and cannot be totally ordered; flow.py already "
            "refuses to derive them. This migration does not create or worsen the condition, and the "
            "DELETE it re-grants is what a repair would use. See the docstring of "
            "0013_er_m013_the_correction_window."
        )


def upgrade() -> None:
    _report_untotally_ordered_reviews()
    op.execute(CONDITIONAL_TRIGGER)
    # The trigger itself is untouched — `CREATE OR REPLACE FUNCTION` re-points the
    # existing `trg_review_stages_append_only` at the new body, so there is no
    # window in which the table is unguarded.
    #
    # DELETE only. `UPDATE` stays revoked: see the docstring for the accounting.
    # Guarded on the role existing, the shape `0001`, `0002` and `0011` all use —
    # the role is provisioned by CI and by the deployment, not by the migration.
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT DELETE ON review_stages TO {APP_ROLE};
          ELSE
            RAISE NOTICE 'role {APP_ROLE} is absent; DELETE grant on review_stages skipped.';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            REVOKE DELETE ON review_stages FROM {APP_ROLE};
          END IF;
        END $$;
        """
    )
    op.execute(UNCONDITIONAL_TRIGGER)
