"""The ledger ref is now a promise. This is the test that makes it one.

`LEDGER_SCHEMA_REF` used to be "a label, and only a label" — echoed on every flow
response and validated against by nothing, because no such document existed.
`research-contracts` 0.4.0 released `urn:datacompany:review-flow-ledger:1`, so the
constant switched. A resolvable ref that nothing checks would be the SAME defect
in a better disguise: a consumer reading it has every reason to believe the body
was validated, and until this file existed nobody had ever held one against it.

Deliberately a UNIT test with no database. `_compute_flow` assembles the response
from `derive`'s output plus a revision row and a cached projection, and `derive`
is pure — its own module docstring says so, and the property suite already builds
`FlowInputs` by hand. So the body can be constructed here exactly as the endpoint
constructs it, and the contract checked, with no fixture and no postgres. A test
that needed a database to answer "does our output match our declared shape?"
would be skipped in every environment that lacks one, which is where a contract
regression would then live.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from research_contracts.canonical import validate_instance

from application.services.flow import LEDGER_SCHEMA_REF, _utc_z
from domain.policies.flow_derivation import (
    FlowInputs,
    StageDescriptor,
    StagePosition,
    canonical_counts_hash,
    derive,
)

TA = StagePosition(stage_order=1, stage_key="title_abstract", round_number=1)
FT = StagePosition(stage_order=2, stage_key="full_text", round_number=1)

RECORDS = [uuid.UUID(f"0000000{i}-0000-4000-8000-00000000000{i}") for i in range(1, 8)]


def inputs() -> FlowInputs:
    """A review with every terminal bucket occupied.

    Eight import rows (one rejected), seven records (one merged away), two
    stages, one record settled by adjudication, two nobody has reached. Every
    bucket the ledger can report is non-zero somewhere, because a fixture whose
    interesting members are all `0` would validate against a schema that had
    them typed wrongly.
    """
    return FlowInputs(
        import_row_statuses=["imported"] * 7 + ["rejected"],
        record_ids=set(RECORDS),
        removed_by_merge={RECORDS[6]: uuid.UUID("00000000-0000-4000-8000-0000000000aa")},
        decisions={
            (TA, RECORDS[0]): [("include", None), ("include", None)],
            (TA, RECORDS[1]): [("exclude", "wrong_population"), ("exclude", "wrong_population")],
            (TA, RECORDS[2]): [("include", None), ("exclude", "wrong_intervention")],
            (TA, RECORDS[3]): [("include", None)],
            (FT, RECORDS[0]): [("include", None), ("include", None)],
            (FT, RECORDS[4]): [("exclude", "wrong_outcome"), ("exclude", "wrong_outcome")],
        },
        adjudications={(TA, RECORDS[2]): ("include", None)},
        stages=(
            StageDescriptor(stage_order=1, stage_key="title_abstract", name="Title and abstract"),
            StageDescriptor(stage_order=2, stage_key="full_text", name="Full text"),
        ),
        reviewers_required=2,
        worked_at={
            (TA, RECORDS[0]): datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
            (TA, RECORDS[1]): datetime(2026, 8, 30, 9, 5, tzinfo=UTC),
            (TA, RECORDS[2]): datetime(2026, 8, 30, 9, 10, tzinfo=UTC),
            (TA, RECORDS[3]): datetime(2026, 8, 30, 9, 15, tzinfo=UTC),
            (FT, RECORDS[0]): datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            (FT, RECORDS[4]): datetime(2026, 8, 31, 10, 5, tzinfo=UTC),
        },
    )


def body(*, projection_computed_at: datetime | None = None, stale: bool = False) -> dict[str, Any]:
    """The response `_compute_flow` builds, assembled member for member.

    Kept in step with that function BY HAND, which is the honest description of
    the coupling: there is no way to call it without a session. If a member is
    added there and not here, the new member is simply unvalidated — so the
    member-set assertion below is the guard, and it is why that test exists
    beside the validation one rather than being folded into it.
    """
    result = derive(inputs())
    return {
        "review_id": "7c67fcdf-c60e-45d8-9d46-a35189c0ea11",
        "flow_revision_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "flow_revision_number": 3,
        "counts": result["counts"],
        "counts_by_stage": result["counts_by_stage"],
        "canonical_counts_hash": canonical_counts_hash(result["counts"]),
        "schema_ref": LEDGER_SCHEMA_REF,
        "projection_stale": stale,
        "projection_computed_at": _utc_z(projection_computed_at),
    }


def test_a_derived_flow_body_validates_against_the_released_document() -> None:
    """The whole point. Our output, their schema, no exceptions taken."""
    assert validate_instance(body(), LEDGER_SCHEMA_REF) == []


def test_a_body_with_a_written_projection_validates() -> None:
    """The other arm of the nullable member — a review whose cache HAS been written.

    Both arms, because `projection_computed_at` is required-and-nullable and a
    suite that only ever exercised the null would not notice a timestamp
    grammar the document refuses.
    """
    written = body(projection_computed_at=datetime(2026, 8, 31, 10, 5, tzinfo=UTC), stale=True)
    assert written["projection_computed_at"] == "2026-08-31T10:05:00Z"
    assert validate_instance(written, LEDGER_SCHEMA_REF) == []


def test_the_offset_spelling_the_producer_used_to_emit_is_refused() -> None:
    """THE REGRESSION GUARD FOR THE ONE THING THAT HAD TO CHANGE HERE.

    `datetime.isoformat()` renders UTC as `+00:00`. The released `timestamp`
    primitive is trailing-`Z` only, so the previous emission would not have
    validated — which nobody could discover while the ref resolved to nothing.
    If somebody "simplifies" `_utc_z` back to `.isoformat()`, this fails.
    """
    offset = body()
    offset["projection_computed_at"] = datetime(2026, 8, 31, 10, 5, tzinfo=UTC).isoformat()
    assert offset["projection_computed_at"].endswith("+00:00")
    assert validate_instance(offset, LEDGER_SCHEMA_REF) != []


def test_the_body_carries_exactly_the_members_the_document_declares() -> None:
    """Both directions, and this is the guard on the hand-kept fixture above.

    A member added to `_compute_flow` and not to the document is unvalidated; a
    member in the document that the producer never sends is a contract
    describing something else. Neither is visible from a validation call alone —
    validation is silent about members it was never given.
    """
    from research_contracts.canonical.registry import canonical_schemas

    document = canonical_schemas()[LEDGER_SCHEMA_REF]
    assert set(body()) == set(document["properties"])
    assert set(body()) == set(document["required"])


@pytest.mark.parametrize("member", sorted(body()))
def test_dropping_any_member_is_refused(member: str) -> None:
    """Non-vacuity: proves the document actually constrains this body rather
    than accepting anything shaped vaguely like it."""
    partial = body()
    del partial[member]
    assert validate_instance(partial, LEDGER_SCHEMA_REF) != []
