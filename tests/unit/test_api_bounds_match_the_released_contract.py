"""Two API fields the released contract bounds tighter than the door did.

`research_contracts` 0.9.0 released payload schemas that bind `external_key`
(`urn:datacompany:review-created:1`) and `assertion_key`
(`urn:datacompany:review-assertion-recorded:1`) to `maxLength: 256` and
`maxLength: 128` + `pattern: ^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$`
respectively. `CreateReviewBody.external_key` and `AssertionBody.assertion_key`
accepted more than that — the first had no `max_length` at all, the second had
a looser `max_length=200` and no pattern. Both are demonstrated defects: the
outbox producers (`events.py::emit_review_created`,
`events.py::emit_assertion_recorded`) write the caller's value into the payload
VERBATIM, so a caller could create a row today that the schema governing that
row's own event type would refuse, and `DomainOutbox` is append-only — there is
no correcting it afterwards. `test_every_producer_matches_its_released_schema.py`
proves the producers match the schema for the payloads it happens to build;
this file proves the two bounds the API enforces at the door are the SAME
bounds, so nothing a caller sends can ever reach that gap in the first place.

## Why this reads the bound OUT of the schema rather than pinning the numbers

Pinning `256` and `128` and the pattern as bare literals here would make this
file agree with itself, not with the contract: a schema bump that widens or
narrows either bound would leave both the field and this test unchanged, and
green. Reading the constraint off `canonical_schemas()[...]` at test time means
a schema change and a field-declaration change either move together in the
same PR or this fails — which is the whole reason the two demonstrated
defects above were possible to ship silently in the first place.
"""

from __future__ import annotations

from research_contracts.canonical import canonical_schemas

from api.routers.evidence import AssertionBody
from api.routers.reviews import CreateReviewBody

REVIEW_CREATED_SCHEMA_REF = "urn:datacompany:review-created:1"
ASSERTION_RECORDED_SCHEMA_REF = "urn:datacompany:review-assertion-recorded:1"


def _field_constraint(model: type, field: str) -> dict[str, object]:
    """The subset of a Pydantic field's rendered JSON Schema this file cares
    about, keyed the same way `research_contracts`' documents are."""
    schema = model.model_json_schema()["properties"][field]
    return {k: schema[k] for k in ("maxLength", "pattern") if k in schema}


def test_the_external_key_bound_matches_the_released_contract() -> None:
    """`CreateReviewBody.external_key` must refuse at the door exactly what
    `urn:datacompany:review-created:1` would refuse in the outbox.

    Measured before this test existed: production holds 3 reviews, longest
    `external_key` 24 characters — nothing in production is within reach of
    the 256-character bound, so tightening it here breaks no existing row.
    """
    document = canonical_schemas()[REVIEW_CREATED_SCHEMA_REF]
    released = document["properties"]["external_key"]
    assert released["maxLength"] == 256, "the released schema's bound moved; this test target is stale, not the field"

    field = _field_constraint(CreateReviewBody, "external_key")
    assert field.get("maxLength") == released["maxLength"], (
        f"CreateReviewBody.external_key allows more than the contract does: "
        f"field={field.get('maxLength')!r} contract={released['maxLength']!r}"
    )


def test_the_assertion_key_bound_and_pattern_match_the_released_contract() -> None:
    """`AssertionBody.assertion_key` must match `urn:datacompany:review-assertion-recorded:1`
    on BOTH axes independently — length and character set are separate failure
    modes, and a caller could violate either one without violating the other
    (a 200-character all-lowercase key; a 10-character key with a capital
    letter). The old field only bounded length, and looser than the contract
    at that.

    Measured before this test existed: production holds zero assertions, so
    tightening either bound breaks no existing row.
    """
    document = canonical_schemas()[ASSERTION_RECORDED_SCHEMA_REF]
    released = document["properties"]["assertion_key"]
    assert released["maxLength"] == 128, "the released schema's bound moved; this test target is stale, not the field"
    assert released["pattern"] == r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", (
        "the released schema's pattern moved; this test target is stale, not the field"
    )

    field = _field_constraint(AssertionBody, "assertion_key")
    assert field.get("maxLength") == released["maxLength"], (
        f"AssertionBody.assertion_key allows more than the contract does: "
        f"field={field.get('maxLength')!r} contract={released['maxLength']!r}"
    )
    assert field.get("pattern") == released["pattern"], (
        f"AssertionBody.assertion_key's pattern does not match the contract's: "
        f"field={field.get('pattern')!r} contract={released['pattern']!r}"
    )
