"""ER-M026: `artifact_id` and `artifact_version` get the shape the PORT declares.

Revision ID: 0026_er_m026_artifact_ref_shape
Revises: 0025_er_m025_artifact_records
Create Date: 2026-09-06

## The defect this closes

`0025` wrote `CHECK (artifact_id ~ '^[^/]+$')` and the same on
`artifact_version`, named both `…_not_storage_key`, and both its own module
docstring and `src/models/foundation.py`'s `ArtifactRecord` docstring claimed
the CHECK refuses "`s3://…`, `bucket/path`, an opaque blob".

Only the first two of those three are true, and only because they happen to
contain the one character the regex bans. Measured against `0025` at head on a
scratch database, every one of these INSERTed successfully:

| value written to `artifact_id`            | `^[^/]+$` |
| ----------------------------------------- | --------- |
| `s3:bucket:key`                           | ACCEPTED  |
| `artifact-store` (a bare bucket name)     | ACCEPTED  |
| `bucket\\path\\file` (a Windows-ish key)   | ACCEPTED  |
| `s3%3A%2F%2Fartifact-store%2Fx`           | ACCEPTED  |
| a 168-character opaque base64 blob        | ACCEPTED  |

"Contains no slash" is not "is not a storage key". A NEGATIVE constraint can
only ever ban the shapes someone thought to enumerate, and a storage key has no
shape — `13-…:739-741` puts the Storage Service behind the Artifact Service
precisely because this service is not supposed to know what one looks like. The
docstring promised a property the DDL never had, which is the mechanism by which
this went unnoticed: a reader who trusted the prose had no reason to test it.

## Why the constraint is inverted rather than widened

The refusal is re-expressed POSITIVELY, from the only released statement of what
these two columns actually hold. `src/adapters/artifact_store.py`'s
`ArtifactRef` — the port that receives what Artifact Service returns from its
`UploadResponse` (`artifact_service/src/models/schemas.py:264-276`) — declares:

    artifact_id: uuid.UUID
    version: int

So `artifact_records.artifact_id` is the text spelling of a `uuid.UUID` and
`artifact_records.artifact_version` is the text spelling of a positive `int`.
Both columns stay `TEXT` — `13-…`'s rule that foreign identifiers are values,
not typed foreign keys, has not changed — but a value that cannot be one of
those two things is now refused for a stated reason rather than tolerated for
an unstated one.

Every row in the table above fails the new CHECK, and it fails for the right
reason: not "it looked like a locator" but "it is not a UUID".

## Exactly what is enforced, and deliberately no more

`ck_artifact_records_artifact_id_is_a_uuid` accepts the canonical LOWERCASE
8-4-4-4-12 hex text form and nothing else. It does NOT assert an RFC-4122
version or variant nibble, because `uuid.UUID` does not: `uuid.UUID(s)` happily
constructs the nil UUID and any non-RFC-4122 variant, and a CHECK that refused
those would refuse a value the port would have accepted — the same
over-promising this migration exists to undo, pointed the other way. Case is
pinned because two spellings of one identifier defeat the
`ix_artifact_records_artifact` lookup and `0025` has no normalising writer;
`13-…`'s "one instant has one spelling" argument applied to identity.

`ck_artifact_records_artifact_version_is_a_positive_integer` accepts a decimal
integer with no leading zeros, no sign and no separators, starting at `1`.
Leading zeros are refused for the same one-spelling reason: `'01'` and `'1'` are
one `int` and two `TEXT` values. `0` and negatives are refused because Artifact
Service numbers versions from 1 and a `version` a caller could not have received
is a caller bug, not a row.

Neither CHECK says anything about `artifact_content_hash`; `0025`'s
`HASH_SHAPE` already pins that to `^sha256:[0-9a-f]{64}$`.

## Why this is a new migration and why it is safe

`0025` is merged and APPLIED IN PRODUCTION — worker4's `alembic_version` reads
`0025_er_m025_artifact_records` — so editing it would change nothing that runs.
The correction has to be its own revision.

`ALTER TABLE … ADD CONSTRAINT … CHECK` validates against existing rows, so a
tightening migration can fail on data. It cannot here: production
`artifact_records` holds **0 rows**. That is not incidental, it is the reason
this can be tightened at all rather than shipped as a nullable advisory column
— and it is true because `0025`'s own docstring records that no writer exists
yet (no rights authority to check against, no Artifact Service client path that
writes this table). `grep` over `src/` finds no INSERT, no ORM `add()` and no
`ArtifactRecord(...)` construction anywhere: the only reader of the class is
`Base.metadata`. There is therefore no caller in this repository that these
constraints could break, and no backfill to write.

## What the old constraints' names cost

`…_not_storage_key` named a threat, not a property, and a name that describes
what a rule is *against* cannot be checked against the rule's text — nobody
reading `ck_artifact_records_artifact_id_not_storage_key` beside `^[^/]+$` is
prompted to ask whether the two agree. The new names state the assertion
(`_is_a_uuid`, `_is_a_positive_integer`), so the name and the expression are
falsifiable against each other by eye. `tests/migration/`'s
`test_the_artifact_ref_is_never_a_storage_key.py` carries the five values above
as cases, so the claim is measured and not merely restated here.
"""

from __future__ import annotations

from alembic import op

revision = "0026_er_m026_artifact_ref_shape"
down_revision = "0025_er_m025_artifact_records"
branch_labels = None
depends_on = None

TABLE = "artifact_records"

#: The canonical lowercase text form of a `uuid.UUID` — 8-4-4-4-12 hex, and
#: nothing about version or variant, because `ArtifactRef.artifact_id`'s own
#: type constrains nothing about version or variant either.
UUID_TEXT_SHAPE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

#: A positive decimal integer with no leading zeros — the text form of
#: `ArtifactRef.version: int` as Artifact Service issues it, from 1 upward.
POSITIVE_INTEGER_SHAPE = r"^[1-9][0-9]*$"

#: `0025`'s two constraints, restored verbatim by `downgrade()`.
_SUPERSEDED: dict[str, str] = {
    "ck_artifact_records_artifact_id_not_storage_key": "artifact_id ~ '^[^/]+$'",
    "ck_artifact_records_artifact_version_not_storage_key": "artifact_version ~ '^[^/]+$'",
}

#: What replaces them. Named for the property asserted, not the threat feared.
_ENFORCED: dict[str, str] = {
    "ck_artifact_records_artifact_id_is_a_uuid": "artifact_id ~ '" + UUID_TEXT_SHAPE + "'",
    "ck_artifact_records_artifact_version_is_a_positive_integer": (
        "artifact_version ~ '" + POSITIVE_INTEGER_SHAPE + "'"
    ),
}


def upgrade() -> None:
    for name in _SUPERSEDED:
        op.drop_constraint(name, TABLE, type_="check")
    for name, expression in _ENFORCED.items():
        op.create_check_constraint(name, TABLE, expression)


def downgrade() -> None:
    for name in _ENFORCED:
        op.drop_constraint(name, TABLE, type_="check")
    for name, expression in _SUPERSEDED.items():
        op.create_check_constraint(name, TABLE, expression)
