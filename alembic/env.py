"""Alembic configuration for Evidence Review 8810 (sync migrations).

Migrations run as the OWNER role, the application runs as a NON-owner. That
split is not cosmetic: reviewer blinding (ER-004.2) is enforced by row-level
security, `FORCE ROW LEVEL SECURITY` makes those policies bind the owner too, but
only a non-owner runtime role is unable to `ALTER TABLE ... DISABLE ROW LEVEL
SECURITY` at all. So the URL resolution here deliberately prefers
``MIGRATION_DATABASE_URL`` and falls back to ``DATABASE_URL`` only where no
separate owner URL is configured (local development and CI, where the ephemeral
`postgres:18` container hands out a superuser anyway).

⚠️ DO NOT APPLY `alembic revision --autogenerate` OUTPUT WITHOUT HAND-REVIEW.

The models in this service are typed accessors; the integrity rules — CHECK
constraints, partial unique indexes, `ON DELETE RESTRICT` foreign keys — are raw
DDL in `alembic/versions/` with no ORM expression. `target_metadata` therefore
does not describe them, and autogenerate correctly concludes they should be
removed. Measured against a correctly migrated database it emits 109
`op.drop_constraint`, 25 `op.drop_index` and 1 `op.drop_table`; applying that
file takes the schema from 43 CHECK constraints to 0 and from 59 foreign keys to
24, at which point reviewer blinding and append-only decision history are no
longer enforced by the database at all.

Autogenerate is a drafting aid here, never a source of truth: keep the `op.add_*`
and `op.alter_*` lines you actually wanted and delete every `op.drop_*` you did
not deliberately intend. `tests/migration/test_autogenerate_blast_radius.py` pins
the exact set of objects it would destroy, so that set changing is visible in CI.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import models  # noqa: F401, E402, I001
from database import Base  # noqa: E402

target_metadata = Base.metadata


def get_url() -> str:
    """Resolve the URL migrations run against.

    Order: the owner URL, then the archetype/runtime URL, then alembic.ini.
    The archetype sets ``DATABASE_URL`` to its per-job ephemeral `postgres:18`
    container, so CI never falls through to the alembic.ini placeholder.
    """
    url = (
        os.getenv("MIGRATION_DATABASE_URL")
        or os.getenv("EVIDENCE_REVIEW_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if url and url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url or "CHANGE_ME" in url:
        raise RuntimeError(
            "no database URL configured for alembic; set MIGRATION_DATABASE_URL (owner role) "
            "or DATABASE_URL. Refusing to run against the alembic.ini placeholder."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
