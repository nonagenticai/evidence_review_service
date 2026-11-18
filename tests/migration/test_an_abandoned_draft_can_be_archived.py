"""`0028`: the trigger function admits `('draft','archived')` and nothing else new.

Read back from `pg_proc`, not assumed from the migration's source: the pair list
is parsed out of the LIVE function body and compared, as a set, with
`models.reviews.LEGAL_TRANSITIONS` — the Python mirror the API pre-checks
against. A mirror that drifts from the trigger turns a typed 409 into a raw
database error (or the reverse: a 409 for a transition the database allows).

The round trip runs in a scratch database: `downgrade` to `0027` must restore
`0002`'s eight pairs exactly, and `upgrade head` must bring the ninth back.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.migration.test_migration_round_trip import _alembic, scratch_database  # noqa: F401 - fixture re-export

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not OWNER_URL.startswith("postgresql"),
    reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
)

PREVIOUS: frozenset[tuple[str, str]] = frozenset(
    {
        ("draft", "configured"),
        ("configured", "active"),
        ("active", "paused"),
        ("paused", "active"),
        ("active", "closed"),
        ("paused", "closed"),
        ("closed", "active"),
        ("closed", "archived"),
    }
)

_IN_LIST = re.compile(r"\(NEW\.from_state, NEW\.to_state\) IN \((?P<pairs>.*?)\)\s*THEN", re.S)
_PAIR = re.compile(r"\('(\w+)','(\w+)'\)")


def _live_pairs(url: str) -> frozenset[tuple[str, str]]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            body = conn.execute(
                text("SELECT pg_get_functiondef('review_state_history_legal_transition()'::regprocedure)")
            ).scalar_one()
    finally:
        engine.dispose()
    match = _IN_LIST.search(body)
    assert match, f"could not find the pair list in the live function:\n{body}"
    return frozenset(_PAIR.findall(match.group("pairs")))


def test_the_live_trigger_agrees_with_the_python_mirror() -> None:
    from models.reviews import LEGAL_TRANSITIONS

    live = _live_pairs(OWNER_URL)
    assert live == LEGAL_TRANSITIONS
    assert live - PREVIOUS == {("draft", "archived")}, "0028 adds exactly one edge"
    assert PREVIOUS <= live, "0028 must keep every pair 0002 allowed"
    assert ("draft", "active") not in live


@pytest.mark.slow
def test_downgrade_restores_the_previous_pairs_and_upgrade_brings_the_edge_back(
    scratch_database: str,  # noqa: F811
) -> None:
    _alembic("upgrade", "head", scratch_database)
    assert ("draft", "archived") in _live_pairs(scratch_database)

    _alembic("downgrade", "0027_er_m027_assertion_key_shape", scratch_database)
    assert _live_pairs(scratch_database) == PREVIOUS

    _alembic("upgrade", "head", scratch_database)
    assert _live_pairs(scratch_database) == PREVIOUS | {("draft", "archived")}
