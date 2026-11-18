"""The queue's progress counter had no index leading with the reviewer.

Revision ID: 0010_queue_progress
Revises: 0009_er_m009

`blinded_reads.own_progress_counts` — the pair of counts `GET /screening/queue`
renders as "N of M" — asks `screening_decisions` for

    review_id = ? AND stage_id = ? AND round_number = ? AND reviewer_subject_id = ?
    AND supersedes_decision_id IS NULL

and no index serves that shape. The two that exist both put the RECORD ahead of
the reviewer:

* `ix_decisions_record` — `(review_id, stage_id, review_record_id)`;
* `uq_decision_one_current_per_reviewer` — the partial unique index
  `(review_id, stage_id, round_number, review_record_id, reviewer_subject_id)
  WHERE supersedes_decision_id IS NULL`.

So the caller's own count is answered by touching the whole STAGE. Measured on
`2b29c7a` at 42,600 decisions in one table (Postgres 18.4, the non-owner app
role, RLS in force, fresh `ANALYZE`), counting one reviewer's 300 of them:

    Index Scan using uq_decision_one_current_per_reviewer  (rows=300 loops=1)
      Index Searches: 12
      Buffers: shared hit=664

664 buffers to return the number 300, on every load of the screen a reviewer
lives in. A reviewer holding 20 rows in a 42,600-row table paid 106 buffers and
21 index searches for the number 20.

Postgres 18's B-tree skip scan is the only reason that is not a sequential scan:
it seeks past `review_record_id` once per distinct value rather than reading the
heap. That is not a property to build on — an adversarial review of this branch
measured this same predicate as `Seq Scan … Rows Removed by Filter: 21334,
Buffers: shared hit=478` on its own fixture, where the leading columns covered
the whole table and the planner took the heap instead. Either shape does work
proportional to the stage's TOTAL decision volume across all reviewers, which is
not the quantity the answer is about.

## What this index is, and why it is partial

`(review_id, stage_id, round_number, reviewer_subject_id)
WHERE supersedes_decision_id IS NULL` — the counter's predicate, in its order,
with the counter's partial condition.

`WHERE supersedes_decision_id IS NULL` is not an optimisation detail: it is the
same partial condition `uq_decision_one_current_per_reviewer` already carries and
the same one `own_decisions_for` filters on, so the index covers exactly the rows
the queue calls decided, and corrections — which are the rows with a non-NULL
`supersedes_decision_id` — stay out of it entirely.

It is NOT unique. `uq_decision_one_current_per_reviewer` is the invariant ("one
current decision per reviewer per record"); this is an access path, and making it
unique would silently forbid a reviewer from deciding a second record.

Measured after, same database, same rows, same role, same queries:

    Index Scan using ix_decisions_reviewer_progress  (rows=300 loops=1)
      Index Searches: 1
      Buffers: shared hit=300 read=2

    Index Scan using ix_decisions_reviewer_progress  (rows=20 loops=1)
      Index Searches: 1
      Buffers: shared hit=3 read=1

One index search rather than 12 or 21, and 106 buffers become 4 for the reviewer
holding 20 rows. The 300-row reader still pays ~1 buffer per row it actually
counts, because the RLS policy is checked against `tenant_id` on the heap — but
that is now proportional to the CALLER'S OWN decisions, which is what the number
counts, instead of to everybody's.

Adding `INCLUDE (tenant_id)` to lift that last heap access was tried and
MEASURED: `hit=300 read=6`, i.e. no improvement, because the policy's third
clause also correlates on `review_record_id`, so the scan needs the heap tuple
whatever the index carries. It is not in the shipped definition.

## `ix_decisions_record` is not made redundant by this

It leads with the RECORD, which is what `own_decisions_for` asks for when it
resolves `my_decision` for a page. Both access paths are real; neither is a
prefix of the other. Nothing is dropped here.

## Not CONCURRENTLY, deliberately

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction, and alembic runs a
migration in one; taking it out would mean an autocommit block that cannot be
rolled back with the rest of the revision. It buys nothing here: production
`screening_decisions` is at zero rows — the same fact `0009` relied on for its
`NOT NULL` — so the write lock is taken over an empty table. If this ever has to
be applied to a loaded database, that is a decision to take then, with the row
count in hand.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_queue_progress"
down_revision = "0009_er_m009"
branch_labels = None
depends_on = None

#: Named once, and used by BOTH `upgrade` and `downgrade`. `0009` shipped a
#: `downgrade` that dropped a constraint name no `upgrade` had ever created, and
#: CI went red on #26 for exactly that; a shared constant is the form that cannot
#: diverge.
INDEX_NAME = "ix_decisions_reviewer_progress"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "screening_decisions",
        ["review_id", "stage_id", "round_number", "reviewer_subject_id"],
        postgresql_where=sa.text("supersedes_decision_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="screening_decisions")
