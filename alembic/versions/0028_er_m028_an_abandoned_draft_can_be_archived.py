"""ER-M028: an abandoned draft review can be archived.

## Why

The database is append-only by design: every table carries a DELETE trigger, so
a review that was created and never configured has no way out. `draft` had
exactly one outgoing edge (`draft -> configured`), and the only route to
`archived` was through `active -> closed`, which needs a locked protocol and an
activation the abandoned review never had. A test review created by mistake
would sit in every list forever.

This revision adds ONE edge, `('draft','archived')`, to the trigger that is the
lifecycle's real authority (`review_state_history_legal_transition`, created by
`0002`). Every other pair is kept, and in particular `draft -> active` stays
illegal — activation still walks `draft -> configured -> active`.

Only the FUNCTION is replaced. `trg_review_state_history_legal_transition`
binds to it by name, so `CREATE OR REPLACE` swaps the body under the existing
trigger without a window in which no trigger is attached.

The Python mirror (`models.reviews.LEGAL_TRANSITIONS`) gains the same edge;
`tests/migration/test_an_abandoned_draft_can_be_archived.py` pins the two equal
by reading the live function back out of `pg_proc`.

Downgrade restores `0002`'s eight pairs exactly. A review already archived from
`draft` keeps its history row — the history is append-only and the trigger only
gates INSERTs — so a downgrade forbids new draft archivals without rewriting
the ones that happened.
"""

from __future__ import annotations

from alembic import op

revision = "0028_er_m028_draft_archivable"
down_revision = "0027_er_m027_assertion_key_shape"
branch_labels = None
depends_on = None

#: `0002`'s list, verbatim. Downgrade restores exactly this.
PREVIOUS_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("draft", "configured"),
    ("configured", "active"),
    ("active", "paused"),
    ("paused", "active"),
    ("active", "closed"),
    ("paused", "closed"),
    ("closed", "active"),
    ("closed", "archived"),
)

LEGAL_TRANSITIONS: tuple[tuple[str, str], ...] = (*PREVIOUS_TRANSITIONS, ("draft", "archived"))


def _function_sql(transitions: tuple[tuple[str, str], ...]) -> str:
    """`0002`'s function body, with the pair list as the only variable."""
    transitions_sql = ", ".join(f"('{a}','{b}')" for a, b in transitions)
    return f"""
        CREATE OR REPLACE FUNCTION review_state_history_legal_transition() RETURNS trigger AS $$
        DECLARE
          prev_state text;
          prev_revision int;
        BEGIN
          SELECT h.to_state, h.revision_number INTO prev_state, prev_revision
            FROM review_state_history h
           WHERE h.review_id = NEW.review_id
           ORDER BY h.revision_number DESC
           LIMIT 1;

          IF prev_state IS NULL THEN
            -- The first row is the review coming into existence.
            IF NEW.from_state IS NOT NULL OR NEW.to_state <> 'draft' OR NEW.revision_number <> 1 THEN
              RAISE EXCEPTION 'the first state row must be revision 1, NULL -> draft (got % -> %, revision %)',
                NEW.from_state, NEW.to_state, NEW.revision_number
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
          END IF;

          IF NEW.from_state IS DISTINCT FROM prev_state THEN
            RAISE EXCEPTION 'stale transition: review is in state %, caller declared %',
              prev_state, NEW.from_state
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.revision_number <> prev_revision + 1 THEN
            RAISE EXCEPTION 'revision must be contiguous: expected %, got %',
              prev_revision + 1, NEW.revision_number
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NOT ((NEW.from_state, NEW.to_state) IN ({transitions_sql})) THEN
            RAISE EXCEPTION 'illegal lifecycle transition % -> %', NEW.from_state, NEW.to_state
              USING ERRCODE = 'restrict_violation';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """


def upgrade() -> None:
    op.execute(_function_sql(LEGAL_TRANSITIONS))


def downgrade() -> None:
    op.execute(_function_sql(PREVIOUS_TRANSITIONS))
