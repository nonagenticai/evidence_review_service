"""ORM models for Evidence Review 8810.

`Base` lives in `database` so alembic and the app share exactly one metadata
object. Sharing the object is not enough on its own, though: `Base.metadata` is
populated as a SIDE EFFECT of importing the modules that declare the mapped
classes, so a `models` package that imports only `Base` hands alembic an EMPTY
metadata.

That is not a cosmetic gap. `alembic/env.py` sets `target_metadata =
Base.metadata` after `import models`, and empty metadata means
`alembic revision --autogenerate` reads "the models declare no tables at all"
and emits a migration that DROPS every table in the database. The submodule
imports below are the only thing standing between the next person to run
autogenerate and a migration that deletes the review corpus.

They are re-exports, not decoration — `__all__` names them so ruff sees them as
used, and `tests/unit/test_model_metadata.py` fails if any of them is removed.

Keeping these imports does NOT make autogenerate safe, and nothing here should be
read as saying it does. These classes are typed accessors: the CHECK constraints,
partial unique indexes and `ON DELETE RESTRICT` foreign keys that enforce reviewer
blinding and append-only history are declared only in `alembic/versions/`, so
autogenerate still proposes dropping all of them — 109 `op.drop_constraint` and 25
`op.drop_index` as measured today. The imports below prevent `op.drop_table` for
every table; they do not prevent the integrity layer being stripped. See the
warning in `alembic/env.py` and the pinned set in
`tests/migration/test_autogenerate_blast_radius.py`.

`operation_idempotency` (migration 0001) is deliberately NOT mapped here: no
application code reads or writes it, and a mapped class nothing uses is a second
definition to keep in sync for no benefit. `tests/migration/` pins that
exception so it stays a decision rather than an oversight.
"""

from __future__ import annotations

from database import Base
from models import evidence, foundation, records, reviews, screening

__all__ = ["Base", "evidence", "foundation", "records", "reviews", "screening"]
