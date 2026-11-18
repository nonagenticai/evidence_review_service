"""ER-M007: a survivorship field name is a closed vocabulary.

Revision ID: 0007_er_m007
Revises: 0006_er_m006
Create Date: 2026-08-10

`duplicate_survivorship_fields.field_name` accepted any string. `resolve_cluster`
took it straight from the request body and did `getattr(version, field_name, None)`
with no validation, and the table had no CHECK.

That was inert only because nothing ever read the rows back. With `apply_survivorship`
the same `field_name` becomes a caller-chosen write target on `review_record_versions`:
a merge naming `"review_id"` would mint a version of one review's record carrying
another review's id. Row-level security on that table is scoped to the TENANT, not to
the review — the same gap `resolution_chain`'s docstring documents for reads — so the
`WITH CHECK` predicate would not refuse it.

The vocabulary is `imports.RECORD_CONTENT_FIELDS`: the eleven fields that make up a
record's hashed content view, which are exactly the values `canonical_presentation`
shows a reviewer to choose between. Identity, tenancy, lineage and provenance are not
choosable.

## Why the constraint exists as well as the Python check

`59bc339` — "The seal's closed vocabulary existed only in the migration" — is the
same defect in the other direction. A vocabulary declared only in the migration is
not enforced where it is used; one declared only in Python is not a constraint, and
this table is reachable by any code path holding a session. Both, or it is a
convention.

## Why NOT VALID then VALIDATE

The table is empty everywhere I can observe — it has never had a read path, so
nothing has ever depended on a value in it. But a single legacy row outside the
vocabulary would abort the whole `ALTER` with a message naming neither the row nor
the table's purpose. Adding the constraint `NOT VALID` and validating as a separate
statement makes a deployment with such a row fail at validation, on a statement that
says what it was validating.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_er_m007"
down_revision = "0006_er_m006"
branch_labels = None
depends_on = None

#: Must stay in step with `imports.RECORD_CONTENT_FIELDS`. Pinned from both ends by
#: `tests/migration/test_survivable_vocabulary_agrees.py`, so a field added to one
#: and not the other is a red test rather than a 500 at merge time.
SURVIVABLE_FIELDS = (
    "arxiv_id",
    "abstract",
    "authors",
    "container_title",
    "doi",
    "pmcid",
    "pmid",
    "publication_year",
    "record_type",
    "title",
    "url",
)

CONSTRAINT = "ck_survivorship_field_name"
TABLE = "duplicate_survivorship_fields"


def upgrade() -> None:
    values = ", ".join(f"'{name}'" for name in SURVIVABLE_FIELDS)
    op.execute(
        sa.text(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} "  # noqa: S608 - constant identifiers
            f"CHECK (field_name IN ({values})) NOT VALID"
        )
    )
    op.execute(sa.text(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CONSTRAINT}"))  # noqa: S608


def downgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))  # noqa: S608
