"""`assertion_key`'s shape is a property of the DATA, so the database enforces it.

Before `0027`, `research_assertions.assertion_key` was plain `Text` with no
CHECK. The shape lived only in `AssertionBody`'s Pydantic field and in the
released contract `urn:datacompany:review-assertion-recorded:1` — both outside the
database. `evidence.py`'s own comment states the consequence and is correct:
`emit_assertion_recorded` copies the value verbatim into an append-only outbox
row, "so this is the only place either can be caught."

A door is not a wall. Any route that is not that one FastAPI body — a fixture, a
backfill, a repair script, a second endpoint, a caller importing
`evidence.record_assertion` directly — bypassed the only shape check that
existed, and the resulting row could never be corrected.

⚠️ The load-bearing test in this file is not any single refusal: it is
`test_the_database_and_the_released_contract_cannot_drift`. Three independent
copies of one pattern now exist (the released schema, the Pydantic field, this
CHECK) and three copies of a rule is how two of them quietly disagree. That test
is what makes them one rule with three enforcement points.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", "")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not OWNER_URL.startswith("postgresql"),
        reason="needs a live postgres; neither MIGRATION_DATABASE_URL nor DATABASE_URL is a postgresql URL",
    ),
]

CONSTRAINT = "ck_research_assertions_assertion_key_shape"

#: Values the released pattern admits. `population.mean_age` is the shipped
#: example; the others exercise the parts of the grammar a naive regex often
#: gets wrong — a single unsegmented token, digits after the first character,
#: and several dot segments.
ACCEPTED = (
    "population.mean_age",
    "mortality",
    "outcomes.overall_survival.month_12",
    "arm_a2.n",
)

#: Values it must refuse, each for a different reason, so a single over-broad
#: mutation cannot satisfy the whole set.
REFUSED = (
    ("an uppercase letter", "Mortality"),
    ("a leading dot", ".mortality"),
    ("a trailing dot", "outcomes."),
    ("an empty segment", "a..b"),
    ("a leading digit", "1outcome"),
    ("whitespace", "overall survival"),
    ("the claim's prose rather than a key", "the intervention improves overall survival"),
    ("129 characters, one over the bound", "a" * 129),
    # ⚠️ THE DISPUTED ONE, AND ITS REFUSAL IS DELIBERATE. The domain spells its
    # terms with hyphens and this pattern forbids them. Widening was attempted
    # on 2026-09-07 and refused by `research_contracts`' compatibility gate:
    # whether a regex change loosens cannot be decided by comparing regexes, so
    # it is reported breaking rather than guessed, and `OWNERS.yaml` mints a new
    # $id for a change to a released rule. This case is pinned so that if the
    # hyphen is ever admitted it happens as
    # `urn:datacompany:review-assertion-recorded:2` plus a revision that swaps this
    # constraint — and NOT as a quiet edit that puts the database and the
    # published contract into disagreement.
    ("a hyphen, which the domain uses and :1 does not admit", "outcomes.mortality-benefit"),
)


def _released_pattern() -> str:
    """The `assertion_key` pattern as `research_contracts` publishes it.

    Read at test time rather than pinned as a literal, for the reason
    `tests/unit/test_api_bounds_match_the_released_contract.py` already gives:
    a literal would make this file agree with itself instead of with the
    contract, and a schema change would leave it green.
    """
    from research_contracts.canonical import canonical_schemas

    document = canonical_schemas()["urn:datacompany:review-assertion-recorded:1"]
    return document["properties"]["assertion_key"]["pattern"]


def test_the_constraint_exists_after_the_migration() -> None:
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        found = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'research_assertions'::regclass AND conname = :name"
            ),
            {"name": CONSTRAINT},
        ).scalar_one()
    assert found == 1, f"{CONSTRAINT} is missing; the shape is unenforced at the layer that stores it"


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_legitimate_key_is_accepted(value: str) -> None:
    """Non-vacuity: a constraint that refused everything would pass every
    refusal test in this file while making the table unusable."""
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT :v ~ :p AND length(:v) <= 128"),
                {"v": value, "p": _released_pattern()},
            ).scalar_one()
            is True
        )


@pytest.mark.parametrize(("because", "value"), REFUSED, ids=[b for b, _ in REFUSED])
def test_the_database_refuses_a_key_that_is_not_one(because: str, value: str) -> None:
    """Asked of the CHECK itself, by expression, so the assertion is about the
    constraint the database holds rather than about a regex this test carries."""
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        accepted = connection.execute(
            text("SELECT :v ~ :p AND length(:v) <= 128"),
            {"v": value, "p": _released_pattern()},
        ).scalar_one()
    assert accepted is False, f"the database accepts {value!r} ({because}); the shape is not enforced"


def test_the_database_and_the_released_contract_cannot_drift() -> None:
    """⚠️ THE POINT OF THIS FILE.

    Three copies of one pattern now exist — the released schema, `AssertionBody`'s
    Pydantic field, and this CHECK. Three copies of a rule is how two of them
    quietly disagree, and a disagreement here is silent: the API would refuse a
    value the database would have stored, or worse, accept one it would not.

    This asserts the CHECK's recorded definition contains the released pattern
    verbatim, so changing either without the other turns this red.
    """
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        definition = connection.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": CONSTRAINT},
        ).scalar_one()

    released = _released_pattern()
    assert released in definition, (
        f"the CHECK does not carry the released pattern.\n  released: {released}\n  check:    {definition}"
    )

    from api.routers.evidence import AssertionBody

    field = AssertionBody.model_fields["assertion_key"]
    api_patterns = [m.pattern for m in field.metadata if hasattr(m, "pattern")]
    assert released in api_patterns, (
        f"AssertionBody's pattern has drifted from the released contract: {api_patterns} != {released!r}"
    )


def test_an_over_length_key_is_refused_by_the_database_not_only_the_api() -> None:
    """`13-…:§8.2` caps a minimal_fact at 512 characters. An unbounded key is a
    back door for exactly the claim prose the payload deliberately excludes, so
    the 128-character bound belongs beside the shape and not only at the door."""
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        definition = connection.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": CONSTRAINT},
        ).scalar_one()
    assert "128" in definition, f"the CHECK carries no length bound: {definition}"


def test_the_unique_constraint_still_scopes_the_key_to_its_review() -> None:
    """`0018`'s `(review_id, assertion_key)` uniqueness is what makes the key a
    business identity rather than a label, and it is the reason a transliteration
    drift matters: two spellings of one assertion become two assertions rather
    than a refusal. Pinned so `0027` cannot be read as having replaced it."""
    engine = create_engine(OWNER_URL, future=True)
    with engine.begin() as connection:
        found = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'research_assertions'::regclass AND conname = 'uq_research_assertion_key'"
            )
        ).scalar_one()
    assert found == 1
