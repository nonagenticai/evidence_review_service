"""ER-M008: an unblinding discloses to a declared audience, not to the tenant.

Revision ID: 0008_er_m008
Revises: 0007_er_m007

`ER-M005`'s `screening_decisions_blinded` policy has a third clause that opens a
record's decisions once an unblinding event exists:

    OR EXISTS (SELECT 1 FROM screening_unblinding_events u
                 JOIN screening_conflicts c ON c.conflict_id = u.conflict_id
                WHERE c.review_id = screening_decisions.review_id
                  AND c.stage_id = ...  AND c.round_number = ...
                  AND c.review_record_id = ...)

It keys on the CONFLICT and never on the caller, so ONE unblinding opens both
dispositions to every reviewer in the tenant — including a third reviewer
assigned to the same record who has not decided it yet. Measured over HTTP: the
bystander reads `{"items": [], "count": 0}` before the unblinding and both
dispositions after it, having formed no opinion of their own in between. That is
a blinding break, not a governed disclosure, and it is the one thing a
demonstration of "blinded dual screening" cannot survive.

## Why the audience is declared rather than derived

`test_an_unblinding_opens_the_record_to_every_reviewer_in_the_tenant` recorded
this as unfixable, and its reasoning was right about the fix it considered: the
natural narrowing — "only a reviewer who has already DECIDED this record" —
cannot be written as a policy on `screening_decisions`, because a subquery over
the table its own policy guards raises `infinite recursion detected in policy`,
and `SECURITY DEFINER` is ruled out under `FORCE ROW LEVEL SECURITY`.

A declared audience does not have that problem. It is a column on
`screening_unblinding_events`, whose own policy is tenant-scoped and mentions
`screening_decisions` nowhere, so the EXISTS gains a predicate rather than a
cycle.

It is also the better answer on the merits, and that is the owner's call
(George, 2026-08-10): the disclosure becomes an attributable act by a named
person, which is what an HTA audit trail has to be able to show. A derived
audience would change meaning as people decide, so the same event would authorise
different readers on different days — impossible to state in a protocol.

## Why NOT NULL with no default

A nullable column, or one defaulting to `'{}'`, gives a future writer that forgets
it a silent fall-back to some audience. `NOT NULL` with no default means
forgetting is an `IntegrityError` at the moment of the write. Fail-closed on the
one table whose whole purpose is to record who was allowed to see something.

## The backfill NARROWS existing rows, deliberately

Existing rows were written under a rule whose audience was "everyone in the
tenant". There is no honest way to record that as a list, and recording it as one
would preserve a break this migration exists to close. So each existing row is
backfilled with the set that was defensible all along: the reviewers who have a
decision on that conflict's record, plus the actor who unblinded.

That is derived from data rather than guessed, and it moves access in the safe
direction — anyone dropped could only ever have been reading something the
requirement did not intend them to see. Recorded here rather than inferred later,
which is the same reasoning `0006` used for `sealed_with = 'unknown'`, applied to
a case where leaving it unknown would mean leaving it open.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_er_m008"
down_revision = "0007_er_m007"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
ACTOR_GUC = "app.actor_subject_id"
ROLE_GUC = "app.actor_role"


def upgrade() -> None:
    op.add_column(
        "screening_unblinding_events",
        sa.Column("disclosed_to_subject_ids", sa.ARRAY(sa.Text()), nullable=True),
    )

    # `screening_unblinding_events` is append-only, enforced by
    # `trg_screening_unblinding_events_append_only` — "the audit trail of who
    # looked is the whole point". It refused this backfill, which is the trigger
    # doing its job: nothing in the application may ever rewrite one of these
    # rows.
    #
    # A migration adding a column and populating it is the one legitimate
    # exception, and it is made explicit rather than routed around. The
    # DISABLE/ENABLE pair is inside the migration's transaction, so a failure
    # anywhere below rolls the trigger back on with everything else, and
    # `test_the_append_only_trigger_survived_the_backfill` asserts it is enabled
    # afterwards — a trigger silently left disabled would be a far worse outcome
    # than the leak this migration closes.
    op.execute(
        sa.text("ALTER TABLE screening_unblinding_events DISABLE TRIGGER trg_screening_unblinding_events_append_only")
    )

    # Reviewers with a decision on the conflict's record, plus the unblinder.
    #
    # A plain correlated SCALAR subquery, not a derived table: a subquery in
    # `FROM` cannot reference `u` without `LATERAL`, and the first spelling of
    # this backfill did exactly that and aborted the migration. The `<>` keeps
    # the actor from appearing twice; `array_agg` over an empty set is NULL,
    # hence the COALESCE — an event whose decisions were all superseded away must
    # still end up holding the actor and never NULL, on a column that is about to
    # become NOT NULL.
    op.execute(
        sa.text("""
        UPDATE screening_unblinding_events u
           SET disclosed_to_subject_ids = ARRAY[u.actor_subject_id] || COALESCE(
               (SELECT array_agg(DISTINCT d.reviewer_subject_id)
                  FROM screening_decisions d
                  JOIN screening_conflicts c ON c.conflict_id = u.conflict_id
                 WHERE d.review_id = c.review_id
                   AND d.stage_id = c.stage_id
                   AND d.round_number = c.round_number
                   AND d.review_record_id = c.review_record_id
                   AND d.reviewer_subject_id <> u.actor_subject_id),
               ARRAY[]::text[])
         WHERE disclosed_to_subject_ids IS NULL
        """)
    )
    op.execute(
        sa.text("ALTER TABLE screening_unblinding_events ENABLE TRIGGER trg_screening_unblinding_events_append_only")
    )
    op.alter_column("screening_unblinding_events", "disclosed_to_subject_ids", nullable=False)

    # An empty audience is a row that authorises nobody, which is a write that
    # meant nothing. The service always includes the unblinding actor.
    op.execute(
        sa.text(
            "ALTER TABLE screening_unblinding_events ADD CONSTRAINT ck_unblinding_audience_not_empty "
            "CHECK (cardinality(disclosed_to_subject_ids) > 0) NOT VALID"
        )
    )
    op.execute(sa.text("ALTER TABLE screening_unblinding_events VALIDATE CONSTRAINT ck_unblinding_audience_not_empty"))

    op.execute(
        f"""
        DROP POLICY IF EXISTS screening_decisions_blinded ON screening_decisions;

        CREATE POLICY screening_decisions_blinded ON screening_decisions
          USING (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND (
              reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
              OR current_setting('{ROLE_GUC}', true) = 'lead'
              OR EXISTS (
                SELECT 1
                  FROM screening_unblinding_events u
                  JOIN screening_conflicts c ON c.conflict_id = u.conflict_id
                 WHERE c.review_id = screening_decisions.review_id
                   AND c.stage_id = screening_decisions.stage_id
                   AND c.round_number = screening_decisions.round_number
                   AND c.review_record_id = screening_decisions.review_record_id
                   -- THE CHANGE. The door is still the recorded unblinding; it
                   -- now opens only for the people that event names.
                   AND current_setting('{ACTOR_GUC}', true) = ANY (u.disclosed_to_subject_ids)
              )
            )
          )
          WITH CHECK (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
          );
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP POLICY IF EXISTS screening_decisions_blinded ON screening_decisions;

        CREATE POLICY screening_decisions_blinded ON screening_decisions
          USING (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND (
              reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
              OR current_setting('{ROLE_GUC}', true) = 'lead'
              OR EXISTS (
                SELECT 1
                  FROM screening_unblinding_events u
                  JOIN screening_conflicts c ON c.conflict_id = u.conflict_id
                 WHERE c.review_id = screening_decisions.review_id
                   AND c.stage_id = screening_decisions.stage_id
                   AND c.round_number = screening_decisions.round_number
                   AND c.review_record_id = screening_decisions.review_record_id
              )
            )
          )
          WITH CHECK (
            tenant_id = current_setting('{TENANT_GUC}', true)
            AND reviewer_subject_id = current_setting('{ACTOR_GUC}', true)
          );
        """
    )
    op.execute(
        sa.text("ALTER TABLE screening_unblinding_events DROP CONSTRAINT IF EXISTS ck_unblinding_audience_not_empty")
    )
    op.drop_column("screening_unblinding_events", "disclosed_to_subject_ids")
