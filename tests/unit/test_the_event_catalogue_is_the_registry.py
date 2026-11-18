"""The catalogue, the migration's CHECK and the released envelope agree.

`13-…:§8.3` calls for an event catalogue. Three copies of that catalogue exist
in this service by design, and this file is the only thing holding them equal:

* `alembic/versions/0019_…REGISTERED_EVENTS` — the closed `CHECK` the database
  enforces, which is the copy a caller cannot route around;
* `domain/policies/event_catalog.REGISTERED_EVENTS` — the copy the application
  reads, and the one that records which events nothing can raise yet;
* `09-…:§3.7` — the specification both are transcriptions of.

They are deliberately NOT derived from one another. A shared constant would make
the CHECK and the caller agree by construction, including when both are wrong,
and the failure that costs is silent: the database would accept exactly the
mistake the application makes. Two independent spellings held equal by a test
fails loudly the moment either is edited alone.
"""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from domain.policies.event_catalog import CATALOGUE, EMITTABLE, NOT_YET, REGISTERED_EVENTS, SLO
from models.foundation import ENVELOPE_SCHEMA_REF, PRODUCER

MIGRATION = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "0019_er_m019_domain_outbox_commits_with_the_state.py"
)

#: The twenty-one names `09-…:§3.7` lists, retyped from the document rather than
#: imported from either copy. A third transcription is the point: if the test
#: read one of the two constants it would prove only that they match each other,
#: and both could have drifted from the specification together.
SPECIFIED = (
    "review.created.v1",
    "review.activated.v1",
    "review.paused.v1",
    "review.resumed.v1",
    "review.reopened.v1",
    "review.archived.v1",
    "review.import.completed.v1",
    "review.duplicate_cluster.proposed.v1",
    "review.duplicate_cluster.resolved.v1",
    "review.decision.recorded.v1",
    "review.conflict.opened.v1",
    "review.conflict.resolved.v1",
    "review.research_object.approved.v1",
    "review.proposal.decided.v1",
    "review.observation.accepted.v1",
    "review.observation.superseded.v1",
    "review.assertion.recorded.v1",
    "review.appraisal.recorded.v1",
    "review.evidence_snapshot.created.v1",
    "review.stage.completed.v1",
    "review.closed.v1",
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("er_m019", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_policy_name_the_same_events() -> None:
    assert _migration().REGISTERED_EVENTS == REGISTERED_EVENTS


def test_both_copies_are_the_specification() -> None:
    assert REGISTERED_EVENTS == SPECIFIED
    assert _migration().REGISTERED_EVENTS == SPECIFIED


def test_the_envelope_ref_is_the_same_string_in_all_three_places() -> None:
    """The model's default, the migration's CHECK and the released document.

    A row whose declared grammar names a document nobody released is the defect
    `research_contracts` released the envelope to repair; it must not be
    reachable from this side either.
    """
    migration = _migration()
    assert ENVELOPE_SCHEMA_REF == "urn:datacompany:research-event-envelope:1"
    assert migration.ENVELOPE_SCHEMA_REF == ENVELOPE_SCHEMA_REF
    assert migration.PRODUCER == PRODUCER


def test_the_released_envelope_actually_resolves() -> None:
    """Non-vacuity control for the string above.

    Pinning three copies of a URN equal to each other proves they agree, not
    that the document exists. This is the check that it does — and it is the
    reason the constant may be a URN at all rather than the URL
    `research_output_service` still carries.
    """
    from research_contracts.canonical import canonical_schemas

    schema = canonical_schemas().get(ENVELOPE_SCHEMA_REF)
    assert schema is not None, (
        f"{ENVELOPE_SCHEMA_REF} does not resolve in the installed research_contracts. "
        "Every outbox row this service writes declares it."
    )
    assert schema["properties"]["schema_ref"]["const"] == ENVELOPE_SCHEMA_REF


@pytest.mark.parametrize("name", sorted(NOT_YET))
def test_every_unbuilt_event_is_registered_and_explained(name: str) -> None:
    """The four events no operation can raise yet.

    Listing only the seventeen that work would have produced a catalogue that
    looked complete. Each of these has to be IN the registry — the database
    accepts it, so the register must name it — and each has to carry a reason,
    so the absence stays a statement somebody made.
    """
    assert name in REGISTERED_EVENTS
    assert len(NOT_YET[name]) > 40, f"{name} has no real reason recorded"


def test_the_emittable_set_is_exactly_the_registry_minus_the_unbuilt() -> None:
    """The counts are pinned, not floored, and they MOVE as packages land.

    17 -> 18 and 4 -> 3 on 2026-09-03, when `ER-007` built
    `evidence_snapshots` and `snapshots._build` began raising
    `review.evidence_snapshot.created.v1`. Both numbers move together, which is
    the signature of an event leaving `NOT_YET` rather than a new one being
    registered — a registered event with no writer moves neither.

    A floor (`>= 17`) could not catch the failure this exists for: an event
    quietly dropped from `EMITTABLE` while its writer still runs leaves the
    catalogue describing a service that no longer exists, and the writer keeps
    working, so nothing else would notice.
    """
    assert set(EMITTABLE) == set(REGISTERED_EVENTS) - set(NOT_YET)
    assert len(EMITTABLE) == 18
    assert len(NOT_YET) == 3


#: The English spellings the module narrates its counts in. Only the range the
#: registry can actually reach is listed: a count outside it is a `KeyError` here,
#: which is the right failure -- it means the registry changed shape, not that a
#: word is missing.
NUMBER_WORDS: dict[int, str] = {
    0: "no",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    12: "twelve",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
    21: "twenty-one",
}

#: Every sentence in `event_catalog.py` that states a COUNT, as a pattern with the
#: number word captured. Anchored on wording either side so a reworded sentence
#: fails to match and goes red too: this prose is load-bearing, and silently
#: losing the claim is the same defect as silently miscounting it.
#:
#: ⚠️ RE-PINNED 2026-09-04. The module was rewritten for `PRODUCER`,
#: `COMPATIBILITY_RULE`, `DATA_CLASSES`, `REDACTION`, `RETENTION`, `SLO` and the
#: `CATALOGUE`/`EventDescriptor` structure, and its docstring was rewritten with
#: it. The three previous patterns (the old "Why the ... unbuilt events are
#: listed here" section) no longer exist in the source at all; the sentences
#: below are the ones the new prose actually states its counts in.
COUNT_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"§3\.7` names (\S+) events this service produces", "registered"),
    (r"released \*\*(\S+)\*\* payload documents covering the\s+\*\*(\S+)\*\* wired events", "schema_refs+emittable"),
    (r"The (\S+) registered events no operation in this service can raise yet", "unbuilt"),
    (r"The (\S+) this service can raise today", "emittable"),
)


def test_the_prose_states_the_counts_the_code_computes() -> None:
    """The gate that was missing when this drifted.

    `test_the_emittable_set_is_exactly_the_registry_minus_the_unbuilt` pins the
    numbers in CODE. The module's PROSE narrates the same numbers separately, and
    nothing but this test checks that the narration agrees -- it went stale for a
    day in `44e6cd7` (claimed seventeen/four after the count had moved to
    eighteen/three) with nothing to catch it.

    That is not cosmetic here. `event_catalog` exists to state the gap, so a
    register that miscounts its own gap sends the next reader hunting an unbuilt
    event that does not exist, or worse, trusting that some event still has no
    writer when it has one.

    Reads the module SOURCE, not its `__doc__`: one of the four claims is a `#:`
    comment on `NOT_YET`, which never reaches a docstring.
    """
    source = (Path(__file__).parent.parent.parent / "src" / "domain" / "policies" / "event_catalog.py").read_text(
        encoding="utf-8"
    )

    expected = {
        "emittable": NUMBER_WORDS[len(EMITTABLE)],
        "unbuilt": NUMBER_WORDS[len(NOT_YET)],
        "registered": NUMBER_WORDS[len(REGISTERED_EVENTS)],
        "schema_refs": NUMBER_WORDS[len({d.schema_ref for d in CATALOGUE.values() if d.schema_ref is not None})],
    }

    for pattern, kind in COUNT_CLAIMS:
        match = re.search(pattern, source)
        assert match is not None, (
            f"no sentence in event_catalog.py matches {pattern!r}. The claim was reworded or "
            "deleted; this prose is load-bearing, so restore it or update this pattern."
        )
        words = [w.strip(".,") for w in match.groups()]
        kinds = kind.split("+")
        for word, one_kind in zip(words, kinds, strict=True):
            assert word.lower() == expected[one_kind], (
                f"event_catalog.py says {word!r} where the code computes "
                f"{expected[one_kind]!r} {one_kind} events "
                f"(EMITTABLE={len(EMITTABLE)}, NOT_YET={len(NOT_YET)})"
            )


def test_every_emittable_event_has_a_writer_that_names_it() -> None:
    """A registry entry with no caller is a promise nothing keeps.

    Reads the writer module's SOURCE rather than calling it: the point is that
    every emittable name appears at a callsite, and importing the functions
    would only prove they exist.
    """
    source = (Path(__file__).parent.parent.parent / "src" / "application" / "services" / "events.py").read_text(
        encoding="utf-8"
    )
    missing = [name for name in EMITTABLE if f'"{name}"' not in source]
    assert not missing, f"registered as emittable but no writer names them: {missing}"


def test_no_unbuilt_event_has_a_writer() -> None:
    """The other direction, and the one that would ship a lie.

    A writer for `review.evidence_snapshot.created.v1` would announce a snapshot
    this service cannot build (`ER-007` is not implemented), and an append-only
    log cannot take that back.
    """
    source = (Path(__file__).parent.parent.parent / "src" / "application" / "services" / "events.py").read_text(
        encoding="utf-8"
    )
    for name in NOT_YET:
        assert f'event_type="{name}"' not in source, f"{name} has a writer but no operation that can raise it"


# ============================================== the CATALOGUE cannot silently rot ===


def test_the_catalogue_has_exactly_one_row_per_registered_event() -> None:
    """Both directions, on purpose.

    `CATALOGUE` is keyed by hand, not derived from `REGISTERED_EVENTS` (the
    module docstring on `EventDescriptor` explains why consumers are not a
    second copy in this file; the same independence argument applies to the
    keys). A one-directional check would miss either a registered event with no
    row, or a row for an event that is not registered at all.
    """
    assert set(CATALOGUE) == set(REGISTERED_EVENTS)


def test_every_emittable_event_has_a_released_schema_ref() -> None:
    """A `None` here for an event this service can actually raise would be a
    catalogue that claims `§8.3`'s gate was crossed for an event with no
    document at all."""
    missing = [name for name in EMITTABLE if CATALOGUE[name].schema_ref is None]
    assert not missing, f"emittable but no schema_ref recorded: {missing}"


def test_every_unbuilt_event_has_no_schema_ref() -> None:
    """The other direction: a released document for an event nothing can raise
    is a contract with no writer, which reads as progress through the `§8.3`
    gate and is not — the module docstring on `CATALOGUE` says this explicitly."""
    leaked = [name for name in NOT_YET if CATALOGUE[name].schema_ref is not None]
    assert not leaked, f"not-yet-built but a schema_ref is recorded anyway: {leaked}"


def test_every_owner_is_a_team_or_work_package() -> None:
    """`13-…:§8.2` requires a poisoned delivery to page an owning team, and the
    `EventDescriptor.owner` docstring says the same: "A team or package, never
    a person". `owner` used to be DERIVED for the three `NOT_YET` events by
    string-splitting the human-readable reason sentence on its first "." — the
    identical mistake the module's own docstring warns against for
    `schema_ref` ("Why the schema URN is recorded and not derived"). That
    derivation produced `"Stage completeness is only READ, as an export
    precondition in src/application/services/flow"` as a paging target for
    `review.stage.completed.v1`, because the first "." in that reason sits
    inside the filename `flow.py`. A shape check on every owner in the
    catalogue — not just the three that broke — is the gate that would have
    caught it and catches any future regression to a derived value.
    """
    pattern = re.compile(r"^(ER|FND)-[0-9]")
    bad = {name: descriptor.owner for name, descriptor in CATALOGUE.items() if not pattern.match(descriptor.owner)}
    assert not bad, f"owner does not look like a team/work-package id: {bad}"


def test_the_slo_stays_unset() -> None:
    """`13-…:§14.2` forbids inventing an operational target before Phase 0 runs.

    This is a deliberate tripwire, not a normal assertion: `SLO` is typed
    `Final[None]`, so the only way this goes red is a future session replacing
    `None` with a number that LOOKS plausible. That number would be exactly the
    thing `§14.2` prohibits — an SLO nobody measured, which is worse than the gap
    it would appear to close, because an operator could page against it. If this
    test ever needs to change, the change is "Phase 0 ran", not "a number looked
    reasonable."
    """
    assert SLO is None


def test_the_shared_schema_documents_cover_six_and_two_event_types() -> None:
    """`research_contracts` 0.9.0 released twelve payload documents for the
    eighteen wired events, not eighteen — `emit_review_lifecycle` writes six
    event types from one dict dispatch and `emit_acceptance` writes two, so
    those two documents are each pointed at by more than one `CATALOGUE` row.
    The module docstring narrates this in prose; this test pins the shape in
    data so a future edit that quietly gives one of the six its own document
    (or drops one of the two) is caught here rather than only in the prose gate.
    """
    schema_ref_counts = Counter(
        descriptor.schema_ref for descriptor in CATALOGUE.values() if descriptor.schema_ref is not None
    )
    assert len(schema_ref_counts) == 12, (
        f"expected exactly 12 distinct released schema_ref values, found {len(schema_ref_counts)}"
    )
    shared = sorted((count for count in schema_ref_counts.values() if count > 1), reverse=True)
    assert shared == [6, 2], f"expected exactly two shared documents, covering 6 and 2 event types: got {shared}"


# ======================================================= the blinding guarantee ===


def _writer_source() -> str:
    return (Path(__file__).parent.parent.parent / "src" / "application" / "services" / "events.py").read_text(
        encoding="utf-8"
    )


def test_a_decision_event_carries_no_verdict_and_no_reviewer() -> None:
    """`14-…` forbids an event carrying a blinded decision, and this is the one
    place in the service where the payload rule and the blinding rule point at
    the same field.

    The outbox has no blinding layer — it is the only table here with no
    row-level security at all — and it is append-only. A `disposition` written
    into `review.decision.recorded.v1` is a peer's verdict disclosed permanently
    over a channel that cannot take it back, which is exactly what the 403 on an
    unblinded read exists to prevent.

    Asserted on the writer's own payload block rather than on a committed row:
    a row-level check would only catch the leak for the decisions a test happens
    to create.
    """
    source = _writer_source()
    start = source.index("def emit_decision_recorded")
    end = source.index("def emit_conflict_opened")
    block = source[start:end]
    for forbidden in ("disposition", "exclusion_reason", "reviewer_subject_id", "review_record_id"):
        assert f'"{forbidden}"' not in block, (
            f"emit_decision_recorded puts {forbidden!r} in the payload. The outbox has no blinding "
            "layer and is append-only."
        )


def test_a_conflict_event_carries_no_dispositions() -> None:
    """A conflict exists because two reviewers disagreed. Publishing the two
    dispositions beside it unblinds both at once — the disclosure
    `unblind_conflict` exists to make a recorded, authorized act."""
    source = _writer_source()
    start = source.index("def emit_conflict_opened")
    end = source.index("def emit_conflict_resolved")
    block = source[start:end]
    assert '"disposition"' not in block


def test_the_adjudicated_disposition_is_deliberately_present() -> None:
    """The other direction, so the two rules above cannot be satisfied by a
    writer that simply says nothing.

    An adjudication ENDS the blinding for its conflict: it is recorded by a named
    adjudicator who was authorized to unblind, and the resulting disposition is
    the review's answer rather than a reviewer's private verdict. If this ever
    goes red, the events stopped carrying the one decision consumers may see.
    """
    source = _writer_source()
    start = source.index("def emit_conflict_resolved")
    end = source.index("def emit_observation_superseded")
    assert '"disposition": adjudication.disposition' in source[start:end]
