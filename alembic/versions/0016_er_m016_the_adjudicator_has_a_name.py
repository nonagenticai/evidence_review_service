"""A lead's ruling was attributed to a UUID, and to nothing a person recognises.

`_adjudication_summary` sent `adjudicator_subject_id` and the studio rendered it
verbatim, so the permanent, attributable act of settling a disagreement between
two reviewers was signed with a 36-character opaque string. That is legible to
the database and to nobody else.

## Why a stored column and not a lookup

The obvious alternative is to resolve the subject id to a name at read time,
against Keycloak. Measured on `0015`, this service cannot:

    $ grep -rE "client_secret|realm-management|view-users" src/ .env.example
    (0 rows)

`config.py` carries `KEYCLOAK_URL` and `KEYCLOAK_REALM` and no credential of any
kind; the single outbound client in the tree is `clients/discovery_engine.py`;
and `auth/verifier.py` records that in-cluster Keycloak is not reachable from
this pod at all, which is why JWKS egresses via Cloudflare. Read-time resolution
therefore means minting a confidential client with realm-wide `view-users` and
teaching a service whose only tenant boundary is RLS to read a user directory
across every tenant in the realm. That is a large new blast radius bought for a
label.

Capturing the name the credential already carried is also the more truthful
record. A lookup answers *"what is this person called today"*; an append-only
ledger of who ruled on what wants *"what were they called when they ruled"*.

## Nullable, and permanently so

No backfill is possible and none is intended. `0005` installed
`screening_adjudications_append_only`, which refuses every UPDATE on this table,
so the rows written before this column existed keep `NULL` for good. Readers
fall back to `adjudicator_subject_id`, which remains the evidentiary
attribution — the name is a label on top of it, never a substitute for it.

That fallback is the intended rendering for those rows, not a gap to repair.

## What is deliberately NOT here

No column on `screening_decisions`. A reviewer's display name is a
blinding-relevant asset — `ER-004.2` — and there is no consumer that needs one,
so storing it would create the asset and the risk while buying nothing.

No `server_default` and no CHECK. Both would move the pinned sets in
`tests/migration/test_autogenerate_blast_radius.py`, and a constraint on a label
that is legitimately absent on every historical row is a constraint that exists
to be violated.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_er_m016_adjudicator_name"
down_revision = "0015_er_m015_ledger_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "screening_adjudications",
        sa.Column("adjudicator_display_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("screening_adjudications", "adjudicator_display_name")
