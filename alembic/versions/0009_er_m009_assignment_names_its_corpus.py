"""ER-M009: an assignment records the corpus it was minted from.

Revision ID: 0009_er_m009
Revises: 0008_er_m008

`create_assignments` resolves a `corpus_version_id`, reads that corpus's members
to decide WHICH records to assign, returns the id in its response — and then
never stores it. So the assignment knows which records it covers and has
forgotten which sealed set that was.

`screening_queue` then resolves each record's content with
`current_record_versions`, the review's TAIL. While a record has one version the
two agree; they stop agreeing the moment `apply_survivorship` mints a v2. A
reviewer whose assignment came from corpus v1 is then shown the merged v2 text
and decides on it, and the decision is recorded against a record whose sealed
corpus contains different content.

`create_export`'s G3 refusal already catches the CONSEQUENCE — it refuses to seal
an export citing a version the corpus does not contain. But it catches it after
the screening has happened, which is the expensive place to find out. The claim
this whole service exists to support is "these reviewers screened exactly the
corpus that was sealed", and today nothing makes that true.

## Why NOT NULL, and why now

Same reasoning as `0008`'s audience column: nullable, or defaulted, means a
future writer that forgets it falls back silently to *some* corpus. `NOT NULL`
makes forgetting an `IntegrityError` at the write.

The timing is the load-bearing part. Production `screening_assignments` is at
**zero rows**, so `NOT NULL` costs nothing today. One real run puts thousands of
rows in that table, none of which can ever be repaired — the corpus each came
from is not recoverable from any other column once several corpus versions
exist. This is the last moment the column can be born required.

## The backfill reconstructs rather than guesses

Non-production databases (developer machines, e2e lanes) do have rows. Their
corpus is not recorded, but it is *derivable*: `create_assignments` with no
explicit corpus calls `latest_corpus`, which is the highest `version_number` for
the review. So the corpus an existing row came from is the newest corpus version
of its review that already existed when the row was written —
`created_at <= assigned_at`.

That is a reconstruction from data, in the same spirit as `0008`'s narrowing
backfill, and it is exact for every row written by the only code path that
writes them. It is NOT a fallback to "the latest corpus now", which would be the
very defect this migration closes, quietly applied to history.

A row for which no such corpus exists is genuinely unrepairable, and the
migration fails on it rather than inventing one. That cannot happen through the
service — an assignment cannot be created before a corpus is sealed — so hitting
it means the data was made some other way, and a loud stop is the right outcome.

`screening_assignments` is append-only by trigger ("assignment inputs are
immutable"), so the backfill disables and re-enables it inside this migration's
transaction, exactly as `0008` did, rather than routing around it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_er_m009"
down_revision = "0008_er_m008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "screening_assignments",
        sa.Column("corpus_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # TWO things stand between this migration and its own rows, and only one of
    # them is obvious.
    #
    # The append-only trigger is the obvious one, and `0008` handled its
    # equivalent the same way.
    #
    # `FORCE ROW LEVEL SECURITY` is the other, and it is worth stating plainly
    # because it fails SILENTLY. The migration connects as the table OWNER, and
    # FORCE means the tenant policy applies to the owner too. A migration session
    # sets no `app.current_tenant`, so `tenant_id = current_setting(...)` is NULL
    # for every row: the UPDATE below matches nothing, reports success, and the
    # `SET NOT NULL` that follows is the first thing to notice. Worse, a guard
    # that counts remaining NULLs from inside the same session counts zero — it
    # cannot see the rows either — so the check reads green while doing nothing.
    #
    # Measured here: 22 assignment rows, backfill applied 0, guard reported 0
    # orphans, `SET NOT NULL` then failed with `contains null values`.
    #
    # ⚠️ `0008`'s backfill has the same shape and the same hole. It has never
    # been observed because production `screening_unblinding_events` was empty
    # when it ran, so its UPDATE was correctly a no-op. On any database with rows
    # it silently narrows nothing. Recorded here rather than silently fixed
    # there: rewriting an applied migration would change history for every
    # database that already ran it.
    # BOTH tables, and the second one is the trap inside the trap. Un-FORCEing
    # only `screening_assignments` makes the UPDATE's target rows visible while
    # its correlated subquery still reads `corpus_versions` through a policy that
    # matches nothing — so every row resolves to NULL and the backfill looks like
    # it ran. Measured: 22 of 22 rows still unresolved, with the target table
    # already un-FORCEd. A backfill that reads a second table has to account for
    # that table's RLS as well.
    op.execute(sa.text("ALTER TABLE screening_assignments NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE corpus_versions NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE screening_assignments DISABLE TRIGGER trg_screening_assignments_append_only"))

    # The newest corpus version of this assignment's review that already existed
    # when the assignment was written. A correlated scalar subquery for the same
    # reason `0008` used one: a derived table in FROM cannot reference `a`
    # without LATERAL.
    op.execute(
        sa.text("""
        UPDATE screening_assignments a
           SET corpus_version_id = (
               SELECT cv.corpus_version_id
                 FROM corpus_versions cv
                WHERE cv.review_id = a.review_id
                  AND cv.created_at <= a.assigned_at
                ORDER BY cv.version_number DESC
                LIMIT 1)
         WHERE corpus_version_id IS NULL
        """)
    )

    # Fail loudly and name the count, rather than letting the NOT NULL below
    # report a bare constraint violation an operator has to reverse-engineer.
    #
    # Runs while RLS is still un-FORCEd, which is the whole reason it means
    # anything, and reports `total` alongside `orphans` so "0 orphans because 0
    # rows were visible" cannot present as "0 orphans because the backfill
    # worked". That distinction is exactly what the first version of this
    # migration got wrong.
    op.execute(
        sa.text("""
        DO $$
        DECLARE orphans bigint; total bigint;
        BEGIN
          SELECT count(*), count(*) FILTER (WHERE corpus_version_id IS NULL)
            INTO total, orphans FROM screening_assignments;
          RAISE NOTICE 'ER-M009 backfill: % assignment row(s) visible, % still unresolved', total, orphans;
          IF orphans > 0 THEN
            RAISE EXCEPTION
              '% of % screening_assignments row(s) predate every corpus version of their review; '
              'the corpus they were minted from is not recoverable and cannot be invented', orphans, total
              USING ERRCODE = 'restrict_violation';
          END IF;
        END $$;
        """)
    )

    op.execute(sa.text("ALTER TABLE screening_assignments ENABLE TRIGGER trg_screening_assignments_append_only"))
    op.execute(sa.text("ALTER TABLE screening_assignments FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE corpus_versions FORCE ROW LEVEL SECURITY"))

    op.alter_column("screening_assignments", "corpus_version_id", nullable=False)
    op.create_foreign_key(
        # Postgres' own default name for this constraint, not a hand-picked one:
        # every other FK in this schema carries `<table>_<column>_fkey`, and the
        # blast-radius pin lists them under exactly that spelling.
        "screening_assignments_corpus_version_id_fkey",
        "screening_assignments",
        "corpus_versions",
        ["corpus_version_id"],
        ["corpus_version_id"],
        ondelete="RESTRICT",
    )

    # The queue's per-page read joins `corpus_version_members` on
    # `(corpus_version_id, review_record_id)`, which is that table's primary key.
    # This index is for the other direction — "which assignments came from this
    # corpus" — which the flow ledger and any future re-assignment path ask.
    op.create_index(
        "ix_assignment_corpus_version",
        "screening_assignments",
        ["corpus_version_id"],
    )


def downgrade() -> None:
    # REFUSE TO DESTROY WHAT THE UPGRADE CANNOT RECONSTRUCT.
    #
    # `upgrade` backfills `corpus_version_id` as "the newest corpus version of
    # this review that already existed when the assignment was written". That is
    # a sound RECONSTRUCTION for rows written before the column existed, and it
    # is not the same fact as the column: `create_assignments` takes an explicit
    # `corpus_version_id` from the caller, so a lead may deliberately assign
    # against an OLDER corpus while a newer one exists.
    #
    # Measured, `downgrade` then `upgrade` on a review holding both v1 and v2
    # with the assignment minted explicitly against v1:
    #
    #     BEFORE  c9a5cf0f-...   (v1, as the lead chose)
    #     AFTER   1a0b19e8-...   (v2, as the backfill guessed)
    #
    # Since `ER-M009` that column decides which version the screening queue
    # serves, and since the decision-version fix it decides which version a
    # recorded judgement cites. So the round trip does not lose an audit detail:
    # it changes what a reviewer is shown and what their decision is attributed
    # to, silently, with the column reporting a value that looks deliberate.
    #
    # The column is dropped below, so the fact is unrecoverable afterwards and
    # there is nothing to repair. A refusal is the only honest option: an
    # operator who really wants the downgrade can re-point the offending rows
    # onto the reconstructable value first, deliberately and in the open.
    #
    # ⚠️ The guard un-FORCEs RLS for exactly the reason `upgrade` does, and it
    # would otherwise fall into the hole this file documents at length: the
    # migration session sets no `app.current_tenant`, so a count run under FORCE
    # matches nothing, reports zero mismatches, and the downgrade proceeds having
    # checked nothing. BOTH tables, because the subquery reads `corpus_versions`
    # too. `total` is reported beside `mismatched` so "0 because nothing was
    # visible" cannot present as "0 because everything agreed".
    op.execute(sa.text("ALTER TABLE screening_assignments NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE corpus_versions NO FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text("""
        DO $$
        DECLARE mismatched bigint; total bigint;
        BEGIN
          SELECT count(*),
                 count(*) FILTER (
                   WHERE a.corpus_version_id IS DISTINCT FROM (
                     SELECT cv.corpus_version_id
                       FROM corpus_versions cv
                      WHERE cv.review_id = a.review_id
                        AND cv.created_at <= a.assigned_at
                      ORDER BY cv.version_number DESC
                      LIMIT 1))
            INTO total, mismatched
            FROM screening_assignments a;
          IF mismatched > 0 THEN
            RAISE EXCEPTION
              'refusing to drop screening_assignments.corpus_version_id: % of % assignments name a '
              'corpus that this migration''s backfill would NOT reconstruct, so upgrading again would '
              'silently re-point them and change what those reviewers are served. Re-point them '
              'deliberately first, or accept the loss by clearing the column yourself.',
              mismatched, total
              USING ERRCODE = 'restrict_violation';
          END IF;
          RAISE NOTICE 'downgrade check: % assignments, all reconstructable', total;
        END $$;
        """)
    )
    op.execute(sa.text("ALTER TABLE corpus_versions FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE screening_assignments FORCE ROW LEVEL SECURITY"))

    op.drop_index("ix_assignment_corpus_version", table_name="screening_assignments")
    # The same name `upgrade` created. It has to be spelled out again because
    # `create_foreign_key` above passes Postgres' default name explicitly rather
    # than letting it be derived, so there is no shared constant to diverge from
    # — and the first version of this file diverged, dropping a hand-picked
    # `fk_assignment_corpus_version` that no upgrade has ever created.
    op.drop_constraint("screening_assignments_corpus_version_id_fkey", "screening_assignments", type_="foreignkey")
    op.drop_column("screening_assignments", "corpus_version_id")
