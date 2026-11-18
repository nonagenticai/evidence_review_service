"""ER-M006: record HOW each export was sealed.

Revision ID: 0006_er_m006
Revises: 0005_er_m005
Create Date: 2026-08-09

`inclusion_exports.manifest_hash` had two possible provenances and no way to tell
them apart. `_manifest_hash` preferred `compute_content_hash(instance, schema_ref)`
— structural validation against a declared shape, then the full ordered hash
algorithm — and fell back to a bare RFC 8785 `canonical_sha256` inside a blanket
`except Exception`. Its docstring said the difference "is visible in the returned
`sealed_with` field rather than being silent". There was no such field anywhere in
the tree, and because `EXPORT_SCHEMA_REF` is an `https://` URL that the contract
registry cannot key on at all, the fallback fired on **every export ever taken**.
`verify_export` then recomputed through the same fallback, agreed with itself, and
reported `manifest_matches: true` — a verifier structurally unable to notice that
nothing had ever been validated.

This column is the field that was promised.

## Why the backfill is `unknown` and not `canonical_sha256`

Every row that exists today was almost certainly sealed with the bare hash, for
the reason above. "Almost certainly" is an inference about which wheel was
installed when each row was written, and this column is a RECORD of what
happened, not a reconstruction of it. Writing `canonical_sha256` onto rows whose
mode was never observed would make a guess indistinguishable from a measurement
on exactly the rows where the distinction matters.

`unknown` is not a dead end either: `verify_export` resolves it per row by
recomputing both ways and reporting which one reproduces the stored hash. The
answer is derived when it is needed, from the row, instead of being asserted here
from the outside.

## Why the default is dropped immediately

The default exists only to fill the existing rows without an `UPDATE` — which
`trg_inclusion_exports_append_only` would refuse anyway, correctly. Leaving it in
place would mean a future writer that forgets the column silently produces an
export labelled `unknown`, which is the same silence this migration exists to
remove. `NOT NULL` with no default makes that insert fail loudly instead.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_er_m006"
down_revision = "0005_er_m005"
branch_labels = None
depends_on = None

SEAL_MODES = ("content_hash", "canonical_sha256", "unknown")


def upgrade() -> None:
    op.add_column(
        "inclusion_exports",
        sa.Column("sealed_with", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
    )
    # Dropped in the same migration, deliberately — see the module docstring.
    op.execute("ALTER TABLE inclusion_exports ALTER COLUMN sealed_with DROP DEFAULT")
    op.create_check_constraint(
        "ck_export_sealed_with_vocabulary",
        "inclusion_exports",
        sa.text("sealed_with IN ('content_hash', 'canonical_sha256', 'unknown')"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_export_sealed_with_vocabulary", "inclusion_exports", type_="check")
    op.drop_column("inclusion_exports", "sealed_with")
