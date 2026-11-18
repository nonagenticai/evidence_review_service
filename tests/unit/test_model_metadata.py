"""`Base.metadata` must know about every table, or autogenerate deletes them all.

`alembic/env.py` does `import models` and then `target_metadata = Base.metadata`.
Metadata is populated by the side effect of importing the modules that declare
the mapped classes, so when `src/models/__init__.py` imported only `Base` the
target metadata was EMPTY — and an empty target does not mean "autogenerate
finds nothing to do", it means "the models declare no tables", which
`alembic revision --autogenerate` renders as `op.drop_table(...)` for every
table in the database.

Nothing in the suite noticed, because the application never reads
`Base.metadata`; only alembic does, and only on a command no test runs.

The import is done in a CHILD INTERPRETER on purpose. In a full-suite run
`tests/integration` has already imported `app`, which imports the model modules
by other routes, so an in-process assertion would find the metadata populated
and pass even with `models/__init__.py` broken. Alembic imports `models` and
nothing else; that is the condition worth asserting, and a subprocess is the
only place it can be asserted honestly.

No database needed: this is a statement about imports. The expected set is read
out of the migration sources rather than typed out, so a table added in a future
migration without a model is caught too.
"""

from __future__ import annotations

import ast
import json
import subprocess  # nosec B404 - runs this interpreter on a constant script, no shell, no input
import sys
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "alembic" / "versions"
SRC = REPO / "src"

#: Exactly what `alembic/env.py` does, minus the migration run.
PROBE = "import models; from database import Base; import json; print(json.dumps(sorted(Base.metadata.tables)))"

#: Created by migration 0001 and deliberately unmapped — no application code
#: touches it. Listed here so "unmapped" stays a decision someone made, and so
#: the derived comparison below can be an exact equality instead of a subset
#: check that would pass with a model missing.
UNMAPPED_BY_DESIGN = {"operation_idempotency"}

#: The tables whose absence would be an outright disclosure or data-loss bug,
#: named in case the migration parser below ever silently returns nothing. Green
#: from a parser that found no tables is the failure mode this pins shut.
LOAD_BEARING = {
    "reviews",
    "review_records",
    "corpus_versions",
    "screening_decisions",
    "screening_conflicts",
    "screening_adjudications",
    "inclusion_exports",
    "flow_revisions",
    "duplicate_resolutions",
}


@lru_cache(maxsize=1)
def _metadata_tables_after_importing_models() -> tuple[str, ...]:
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", PROBE],
        cwd=str(REPO),
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", "ENABLE_AUTH": "false", "ENVIRONMENT": "test"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"`import models` failed the way alembic would:\n{result.stderr}"
    return tuple(json.loads(result.stdout.strip().splitlines()[-1]))


@lru_cache(maxsize=1)
def _tables_created_by_migrations() -> frozenset[str]:
    """Every `op.create_table("...")` in `alembic/versions`, by AST not by regex.

    Parsed rather than executed: importing a migration module runs no DDL, but
    it does need an alembic context, and this test must not need a database.
    """
    found: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "create_table" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return frozenset(found)


def test_the_migration_parser_actually_finds_the_migrations() -> None:
    """Guards the two tests below against passing on an empty parse."""
    created = _tables_created_by_migrations()
    assert len(created) >= 20, f"only found {sorted(created)} — the parser or the migration layout changed"
    assert LOAD_BEARING <= created


@pytest.mark.parametrize("table", sorted(LOAD_BEARING))
def test_the_load_bearing_tables_are_in_the_metadata(table: str) -> None:
    """One case per table so a failure names which model stopped being imported."""
    tables = _metadata_tables_after_importing_models()
    assert table in tables, (
        f"{table!r} is absent from Base.metadata after `import models`; "
        f"`alembic revision --autogenerate` would emit op.drop_table({table!r}). "
        f"Metadata held {len(tables)} table(s)."
    )


def test_every_migrated_table_is_mapped_except_the_documented_exception() -> None:
    """Exact equality, not a subset.

    A subset check passes when a model is missing AND when one is spurious. This
    fails in both directions, which is what makes `UNMAPPED_BY_DESIGN` a pin
    rather than a comment.
    """
    expected = set(_tables_created_by_migrations()) - UNMAPPED_BY_DESIGN
    assert set(_metadata_tables_after_importing_models()) == expected
