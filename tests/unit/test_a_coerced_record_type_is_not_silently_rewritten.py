"""A declared `record_type` outside the vocabulary was rewritten to `"other"` in silence.

`_json_to_record` coerced anything outside
``("journal_article", "preprint", "report", "dataset", "other")`` to `"other"`.
Reproduced before this module: five canonical-JSON records declaring `article`,
`journal-article`, `Journal Article`, `conference_paper` and `systematic_review`
all returned `201`, were stored as `"other"`, and nothing in the batch response
or the record said a value had been dropped.

`record_type` is `RECORD_CONTENT_FIELDS[0]`, so it flows into `content_hash` →
`member_manifest_hash` → `items_hash` → `manifest_hash` on append-only tables.
The rewrite was therefore sealed, and the declared value survived only inside
`import_rows.raw`, which no reader of the record reaches.

The sharpest case is `journal-article` **with a hyphen**: that is Crossref's
actual `type` string, so every Crossref-derived producer lost its type.

## Two halves, deliberately separate

**The coercion is recorded.** The vocabulary is NOT widened — the database
enforces it (`ck_record_version_record_type`) — and the record is NOT quarantined
or refused: an unknown type is still a real study, and dropping records is the
failure this domain exists to prevent. `conference_paper` and
`systematic_review` are genuinely outside the vocabulary and must still store as
`"other"`. What changes is that the coercion becomes visible, through the
mechanism already there: `ParsedRecord.unusable_sources` → `_field_provenance`
→ `field_provenance` on `GET /records/{id}`. `field_provenance` is not in
`RECORD_CONTENT_FIELDS`, so this half changes no hash and breaks no seal.

**The spellings producers actually emit are aliased.** `journal-article` and
`Journal Article` are not unknown types — they are the same type under
Crossref's and RIS/Zotero's punctuation. Mapping them is a correction, not a
widening: the stored value stays inside the existing five. This half DOES change
`content_hash` for newly imported records; the tables are append-only, so
existing rows and seals are untouched.
"""

from __future__ import annotations

import json
import uuid

import pytest

from actor import Actor, ActorRole
from application.services import imports as import_service
from infrastructure.imports.import_parser import (
    MAX_RECORD_TYPE_ANNOTATION_CHARS,
    ParsedRecord,
    _canonical_record_type,
    parse_canonical_json,
)
from models.records import ReviewRecordVersion

#: Matches the stub review's `created_by_subject_id`, so the lead gate resolves
#: through the creator-governs-their-own-review branch.
_LEAD_SUBJECT = "lead-carol"


def _only(records: list[ParsedRecord]) -> ParsedRecord:
    assert len(records) == 1, f"expected exactly one record, got {len(records)}"
    return records[0]


def _parsed(declared: object) -> ParsedRecord:
    body = json.dumps({"records": [{"title": "A study", "record_type": declared}]}).encode()
    return _only(parse_canonical_json(body))


# ---------------------------------------------------------------------------
# The alias half — a spelling is not an unknown type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        # Crossref's `type` vocabulary, verbatim. `journal-article` is the one
        # that made every Crossref-derived producer lose its type.
        ("journal-article", "journal_article"),
        ("posted-content", "preprint"),
        ("proceedings-article", "other"),
        # RIS / Zotero / EndNote display forms.
        ("Journal Article", "journal_article"),
        ("JOUR", "journal_article"),
        # BibTeX entry types.
        ("article", "journal_article"),
        ("techreport", "report"),
        # Already canonical, and case/space noise around it.
        ("journal_article", "journal_article"),
        ("  Preprint  ", "preprint"),
        ("DATASET", "dataset"),
    ],
)
def test_a_producers_spelling_of_a_known_type_is_read_as_that_type(declared: str, expected: str):
    """These are not unknown types; they are known types under other punctuation.

    Reading them as `"other"` was a lossy answer to a question that had a right
    one. The stored value stays inside the five the database allows — this
    corrects the mapping, it does not widen the vocabulary.
    """
    assert _parsed(declared).record_type == expected


def test_a_recognised_spelling_is_not_reported_as_a_dropped_value():
    """Scoping: an alias resolved cleanly, so there is nothing to annotate.

    Without this, a fix could satisfy every assertion below by annotating every
    record, which would make the annotation meaningless exactly where it matters.
    """
    assert _parsed("journal-article").unusable_sources == ()


# ---------------------------------------------------------------------------
# The visibility half — a genuinely unknown type is kept, stored as `other`,
# and SAID SO
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", ["conference_paper", "systematic_review", "proceedings-article"])
def test_a_type_outside_the_vocabulary_is_still_stored_as_other(declared: str):
    """The database enforces the vocabulary, so the stored value cannot widen.

    Pinned deliberately: the tempting fix is to widen the five, and
    `ck_record_version_record_type` would reject the write at flush — an import
    failure reported as a database error.
    """
    assert _parsed(declared).record_type == "other"


@pytest.mark.parametrize("declared", ["conference_paper", "systematic_review"])
def test_a_type_outside_the_vocabulary_is_not_quarantined_or_refused(declared: str):
    """An unknown type is still a real study. Losing it is the worse failure.

    Quarantining the record, or answering `422`, would discard a paper over a
    metadata field — the exact loss the coercion was there to avoid. The
    coercion stays; only its silence goes.
    """
    record = _parsed(declared)
    assert record.quarantine_reason is None
    assert record.title == "A study"


@pytest.mark.parametrize("declared", ["conference_paper", "systematic_review"])
def test_the_coerced_value_is_recorded_on_the_parsed_record(declared: str):
    """What was declared has to survive somewhere a reader of the record reaches.

    `import_rows.raw` keeps it verbatim, but nothing that reads a record reads
    that. `unusable_sources` is the channel this parser already uses for "a
    source carried something we could not use and we kept going".
    """
    assert _parsed(declared).unusable_sources == (f"record_type={declared}",)


def test_a_declared_type_inside_the_vocabulary_is_recorded_nowhere():
    """The other half of the scoping: no coercion happened, so nothing is said."""
    assert _parsed("preprint").unusable_sources == ()


def test_a_record_declaring_no_type_at_all_is_not_reported_as_coerced():
    """An absent `record_type` defaults to `"other"` and was never a claim.

    Annotating this would put an `unusable_sources` entry on the majority of
    records in a real corpus — RIS, BibTeX and CSV rows mostly declare nothing —
    and drown the cases that mean something.
    """
    record = _only(parse_canonical_json(b'{"records": [{"title": "A study"}]}'))
    assert record.record_type == "other"
    assert record.unusable_sources == ()


@pytest.mark.parametrize("declared", [12, [], {}])
def test_a_type_of_the_wrong_shape_still_quarantines_and_that_is_unchanged(declared: object):
    """The SHAPE case was already loud. Only the VOCABULARY case was silent.

    `test_a_list_where_a_record_type_belongs_is_quarantined_not_downgraded`
    pins this, on the stated grounds that "silently becoming `other` hides a
    producer bug" — and a quarantined row is not a lost one: it keeps its `raw`
    in `import_rows` and is counted in `quarantined_count`, which the batch
    response returns.

    That is the asymmetry this module is about. `record_type: ["report"]` — a
    producer emitting the wrong JSON type — was already reported. `record_type:
    "journal-article"` — a producer emitting the right JSON type with a
    different vocabulary — was not, and 201'd as `"other"`. This test exists so
    the fix for the second does not quietly relax the first.
    """
    record = _parsed(declared)
    assert record.quarantine_reason is not None
    assert "record_type" in record.quarantine_reason


# ---------------------------------------------------------------------------
# End to end: the annotation reaches the column a reader actually reads
# ---------------------------------------------------------------------------


# The session stub is IMPORTED, not copied.
#
# It used to be a copy, whose docstring said it was "the same stub as
# `tests/unit/test_import_parser.py`, and deliberately the same shape". Two
# copies of a subtle stub is how they drift, and they did: `0012` gave
# `import_batches` a unique constraint, `import_payload` moved to
# `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`, the sibling's stub was
# taught to answer an `Insert` — and this copy was not. It passed locally
# against the branch point and failed in CI against merged main with
# `UnprocessableStateError: the import batch disappeared while being written`,
# which is the re-select finding nothing because the stub answered `None` to a
# statement it did not recognise.
#
# Nothing about the coercion under test needs its OWN session stub, so it does
# not get one. A future statement change now has one place to be answered.
from tests.tenants import tenant  # noqa: E402
from tests.unit.test_import_parser import _NoExistingBatch  # noqa: E402


def _imported_version(payload: bytes) -> ReviewRecordVersion:
    session = _NoExistingBatch()
    import_service.import_payload(
        session,  # type: ignore[arg-type]
        Actor(tenant_id=tenant("unit"), subject_id=_LEAD_SUBJECT, role=ActorRole.LEAD),
        uuid.uuid4(),
        source_kind="canonical_json",
        payload=payload,
    )
    versions = [obj for obj in session.added if isinstance(obj, ReviewRecordVersion)]
    assert len(versions) == 1, f"expected one record version, got {len(versions)}"
    return versions[0]


def test_the_coercion_is_visible_in_the_field_provenance_a_reader_can_fetch():
    """`GET /records/{id}` returns `field_provenance`; this is what it must show.

    The whole point of the corrected fix: the reviewer looking at a record typed
    `"other"` can see that the producer said `systematic_review` and that this
    service could not store it, rather than believing the producer said nothing.
    """
    body = json.dumps({"records": [{"title": "A study", "record_type": "systematic_review"}]}).encode()
    version = _imported_version(body)

    assert version.record_type == "other"
    assert version.field_provenance["unusable_sources"] == ["record_type=systematic_review"]


def test_the_annotation_does_not_reach_the_hashed_content_view():
    """`field_provenance` is not in `RECORD_CONTENT_FIELDS`, so no seal moves.

    This is what makes the visibility half free to apply to a populated
    database. Asserted rather than assumed, because if it were ever added to
    that tuple this whole approach would start rewriting `manifest_hash` on
    append-only tables.
    """
    coerced = _parsed("systematic_review")
    silent = _only(parse_canonical_json(b'{"records": [{"title": "A study"}]}'))

    assert "field_provenance" not in import_service.RECORD_CONTENT_FIELDS
    assert "unusable_sources" not in import_service.RECORD_CONTENT_FIELDS
    # Same hashed content on both sides: the annotation is the only difference.
    assert import_service.record_content(coerced) == import_service.record_content(silent)


def test_the_annotation_is_bounded_because_it_lands_in_an_append_only_column() -> None:
    """A rejected `record_type` is producer-controlled text of no fixed length.

    The annotation is written into `field_provenance`, JSONB on an APPEND-ONLY
    table, so an unbounded copy lets any caller put arbitrarily large text into
    permanent audit storage — once per record, through a route that answers 201
    and can never be cleaned up. Every other producer-controlled string on this
    path already goes through `_truncate`; this one arrived by a different door
    and did not.

    The bound is far tighter than `MAX_FIELD_CHARS` on purpose: a record TYPE is
    drawn from closed vocabularies whose longest genuine member is under thirty
    characters, so anything past it is not a type a producer meant.
    """
    huge = "x" * 100_000
    record_type, provenance = _canonical_record_type(huge)

    assert record_type == "other"
    assert len(provenance) == 1
    annotation = provenance[0]

    # `record_type=` + the bound + the ellipsis, and nothing more.
    assert len(annotation) <= len("record_type=") + MAX_RECORD_TYPE_ANNOTATION_CHARS + 1, (
        f"the annotation is {len(annotation)} chars; unbounded producer text reaches an append-only audit column"
    )
    assert annotation.startswith("record_type=xxx")
    assert annotation.endswith("…"), (
        "a truncated value must SAY it was truncated, or a reader is shown a "
        "shortened type as though it were the whole one"
    )


def test_a_type_at_the_bound_is_kept_whole_and_carries_no_ellipsis() -> None:
    """The negative control: the bound must not fire on a value that fits.

    Without this, a fix that simply truncated everything to zero, or appended an
    ellipsis unconditionally, would satisfy the test above.
    """
    exact = "y" * MAX_RECORD_TYPE_ANNOTATION_CHARS
    _, provenance = _canonical_record_type(exact)
    assert provenance == (f"record_type={exact}",)
    assert "…" not in provenance[0]

    # And the realistic case this exists to keep readable.
    _, real = _canonical_record_type("systematic_review")
    assert real == ("record_type=systematic_review",)
