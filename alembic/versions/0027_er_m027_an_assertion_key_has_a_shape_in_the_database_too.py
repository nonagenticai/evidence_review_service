"""ER-M027: `assertion_key`'s shape is enforced where the data lives, not only at the door.

## What was true before this revision

`research_assertions.assertion_key` was created by `0018` as plain `Text` with
`nullable=False` and a `uq_research_assertion_key` unique constraint over
`(review_id, assertion_key)`. **No CHECK constrained its shape.** The shape lived
in exactly two places, both outside the database:

* `src/api/routers/evidence.py`'s `AssertionBody.assertion_key`, a Pydantic
  `Field(min_length=1, max_length=128, pattern=...)`; and
* the released contract `urn:datacompany:review-assertion-recorded:1`.

That file's own comment says why this matters, and it is correct:

    the outbox writes this value verbatim (`events.py::emit_assertion_recorded`)
    and is append-only, so this is the only place either can be caught.

A value that reaches `research_assertions` by any route other than that one
FastAPI body — a fixture, a backfill, a repair script, a future second endpoint,
a service that imports `evidence.record_assertion` directly — was unconstrained,
and `emit_assertion_recorded` would then copy it verbatim into an append-only
outbox row that no later correction can edit. The API is a door, not a wall, and
the value's shape is a property of the DATA, so the database is where it belongs.

## Why the pattern here is `:1`'s pattern and not a wider one

⚠️ This revision deliberately does NOT widen the character set, and the reason is
recorded here because the question WILL be asked again.

`assertion_key` is authored by the caller — `grep` over `src/` shows it is never
generated anywhere; it arrives verbatim on `AssertionBody` and is written
straight through. The domain spells its own terms with hyphens
(`mortality-benefit`, `quality-of-life`, `progression-free-survival`), while the
released pattern admits `_` and refuses `-`. That mismatch is real, and the
evidence it was fitted backwards is that this service's own fixtures used
`mortality-benefit`, failed validation, and **the fixtures were edited to fit the
pattern** rather than the pattern re-examined.

Widening was attempted on 2026-09-07 and **refused by `research_contracts`'
compatibility gate**, whose reasoning is better than the argument for widening:

    pattern changed; whether it loosened cannot be decided by comparing regexes,
    so it is reported breaking rather than guessed

Regex subsumption is undecidable in general, so "I know this is only a widening"
is a claim the tooling can never verify — from anyone. `OWNERS.yaml`'s
`schema_version_policy` is unambiguous that a change to what a released rule
means mints a new `$id` major. The gate has no exemption path by design, and
inventing one on its first real use would have made it decorative.

So this CHECK enforces the pattern **as released**. If the domain proves hyphens
are needed, the answer is `urn:datacompany:review-assertion-recorded:2` and a
matching revision that swaps this constraint — not a quiet edit here, which would
put the database and the published contract into disagreement in the one
direction nothing else in this service can detect.

## Why this is safe to apply now

Measured on worker4 on 2026-09-07: `research_assertions` held **0 rows**, and
`domain_outbox`, `research_domain_events` and `event_deliveries` each held **0**
rows for `review.assertion.%`. `ALTER TABLE ... ADD CONSTRAINT ... CHECK`
validates against existing rows, so an empty table makes this free — and every
value the API has ever admitted would satisfy it anyway, because it is the same
pattern the API already enforces.

⚠️ The constraint is written with a POSIX regex (`~`), which is what Postgres
offers; the released pattern is ECMA-262. The two agree for this expression —
anchors, character classes and `*` mean the same thing in both — but they do not
agree in general, so a future widening must re-derive the POSIX form rather than
paste the JSON Schema one.
"""

from __future__ import annotations

from alembic import op

revision = "0027_er_m027_assertion_key_shape"
down_revision = "0026_er_m026_artifact_ref_shape"
branch_labels = None
depends_on = None

TABLE = "research_assertions"
CONSTRAINT = "ck_research_assertions_assertion_key_shape"

#: Byte-for-byte the character class of
#: `urn:datacompany:review-assertion-recorded:1`'s `assertion_key` pattern, plus the
#: length bound the same document sets (`maxLength: 128`, `minLength: 1`). The
#: length is included here rather than left to the API for the same reason the
#: shape is: a row that arrives by another route is otherwise unbounded, and
#: `13-…:§8.2` caps a minimal_fact at 512 characters, so an unbounded key is a
#: back door for the claim prose the payload deliberately excludes.
KEY_SHAPE = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
KEY_MAX_LENGTH = 128


def upgrade() -> None:
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        f"assertion_key ~ '{KEY_SHAPE}' AND length(assertion_key) <= {KEY_MAX_LENGTH}",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
