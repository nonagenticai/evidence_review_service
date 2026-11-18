"""The survivorship vocabulary is declared twice. They must agree.

`imports.SURVIVABLE_FIELDS` refuses a bad `field_name` in the service;
`ck_survivorship_field_name` (migration `0007`) refuses it in the database. Both
exist deliberately — `59bc339` is the case where a closed vocabulary lived only
in the migration and was therefore not enforced where it was used, and the mirror
case (Python only) is not a constraint at all, because any code path holding a
session can write the table.

Two declarations is the cost of that, and this file is what stops them drifting.
Without it, adding a field to `RECORD_CONTENT_FIELDS` and not to the migration
produces a `422` from neither layer and an `IntegrityError` → `409` at merge time,
on a request that should have succeeded. Dropping one from the migration and not
from Python produces the reverse: a value Python permits and Postgres refuses.

This is a pure test — it reads two literals and compares them. It needs no
database, which is the point: the drift it catches is a source-level fact, and a
test that needed a live Postgres would be skipped in exactly the environment
where someone edits one list in a hurry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
MIGRATION = ROOT / "alembic" / "versions" / "0007_er_m007_survivable_fields.py"


def _load_migration():  # noqa: ANN202
    """Import the revision file directly. `alembic/versions` is not a package."""
    spec = importlib.util.spec_from_file_location("er_m007_under_test", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_service_declare_the_same_vocabulary() -> None:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from application.services.imports import RECORD_CONTENT_FIELDS, SURVIVABLE_FIELDS

    migration = _load_migration()

    assert set(migration.SURVIVABLE_FIELDS) == set(SURVIVABLE_FIELDS), (
        "the CHECK constraint in 0007 and `imports.SURVIVABLE_FIELDS` disagree. "
        f"only in the migration: {sorted(set(migration.SURVIVABLE_FIELDS) - set(SURVIVABLE_FIELDS))}; "
        f"only in the service: {sorted(set(SURVIVABLE_FIELDS) - set(migration.SURVIVABLE_FIELDS))}"
    )
    assert set(SURVIVABLE_FIELDS) == set(RECORD_CONTENT_FIELDS), (
        "a survivor field must be one of the fields that make up the hashed content view. A field "
        "outside `RECORD_CONTENT_FIELDS` cannot change `content_hash`, so surviving it would produce "
        "a new version indistinguishable from its predecessor."
    )


def test_the_vocabulary_is_not_empty() -> None:
    """The non-vacuity floor.

    Two empty frozensets are equal, and every assertion above passes on them
    while `resolve_cluster` refuses every survivorship request ever made. The
    count is not pinned exactly — that would break on a deliberate addition for
    no benefit — but zero and one are both wrong by construction.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from application.services.imports import SURVIVABLE_FIELDS

    assert len(SURVIVABLE_FIELDS) >= 5, (
        f"the survivorship vocabulary has shrunk to {sorted(SURVIVABLE_FIELDS)}. Either that was "
        "intended and this floor moves with a note, or a refactor emptied the source it derives from "
        "and every equality above is now comparing two empty sets."
    )


def test_identity_and_lineage_are_not_survivable() -> None:
    """The specific names this vocabulary exists to keep out.

    `field_name` reaches `getattr(version, field_name)` and then a column on
    `review_record_versions`. RLS there is scoped to the TENANT, not the review,
    so a merge naming `review_id` would mint a version of one review's record
    carrying another review's id and the `WITH CHECK` predicate would not refuse
    it. These are asserted by name rather than by "everything not in the content
    view", so that the reason survives a refactor of how the set is built.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from application.services.imports import SURVIVABLE_FIELDS

    for forbidden in (
        "review_id",
        "tenant_id",
        "review_record_id",
        "review_record_version_id",
        "version_number",
        "supersedes_version_id",
        "content_hash",
        "schema_ref",
        "field_provenance",
        "source_import_row_id",
        "created_by_subject_id",
    ):
        assert forbidden not in SURVIVABLE_FIELDS, (
            f"{forbidden!r} became survivable. It is identity, tenancy, lineage or provenance — a "
            "caller could then choose it out of another record's version."
        )
