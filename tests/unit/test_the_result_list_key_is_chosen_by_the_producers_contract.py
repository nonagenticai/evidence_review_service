"""Which key holds the records is a contract question, not a guessing game.

`parse_discovery_session_export` walked `("results", "records", "items")` and
took the first key **present**. That is a defensible rule only if at most one of
them can ever be present, and nothing enforced that.

Reproduced before this module existed:

* ``{"results": [one item], "records": [A, B]}`` imported ONE record and dropped
  two, silently, with a `201`;
* ``{"results": []}`` alongside a populated `records` imported zero.

The parser's own docstring defends the loop on the grounds that a renamed key
upstream lands loudly rather than importing an empty corpus. That is true only
for names OUTSIDE the three-element list. For a document carrying two of them,
the loop is exactly the silent-partial-import the docstring says it prevents.

## What the producer actually emits

Checked in `discovery-engine`, not assumed:
`review_export_service._document` builds the sealed document with the records
under **`records`**, and `REVIEW_EXPORT_DOCUMENT_VERSION` is 3. The version
history in that module records what each bump changed — 2 removed `taken_at`, 3
removed `session_name`. Neither touched the record list, so **every**
`document_version` discovery-engine has ever emitted puts the records under
`records`. `results` and `items` are not discovery-engine spellings at any
version.

So where the document names its own version, the key is not inferred: it is
required. Where it does not — the hand-built and non-discovery-engine bodies
this parser also serves — the three-key tolerance stays, but a body carrying
more than one of them is refused rather than resolved by list order.
"""

from __future__ import annotations

import json

import pytest

from infrastructure.imports.import_parser import (
    UnsupportedFormatError,
    parse_discovery_session_export,
)


def test_a_document_carrying_both_results_and_records_is_refused():
    """The silent-drop case: 1 imported, 2 discarded, `201`, nothing said.

    Refused rather than resolved. Picking `records` because it is "the real one"
    would be this parser deciding which half of a self-contradicting document to
    believe, and the reviewer would never learn the other half existed.
    """
    body = json.dumps(
        {
            "session_id": "s1",
            "results": [{"title": "One"}],
            "records": [{"title": "A"}, {"title": "B"}],
        }
    ).encode()

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(body)

    message = str(exc.value)
    # Both names, because the reader has to go and look at the producer and
    # "an ambiguous document" does not say where.
    assert "results" in message
    assert "records" in message


def test_an_empty_results_beside_a_populated_records_is_refused():
    """`{"results": [], "records": [...]}` — the same defect, worse to read.

    The first-present loop took the empty list and imported nothing, which is
    the shape that reaches a reviewer as a sealed, screened, internally
    consistent corpus that was never there.
    """
    body = json.dumps({"session_id": "s1", "results": [], "records": [{"title": "A"}]}).encode()

    with pytest.raises(UnsupportedFormatError):
        parse_discovery_session_export(body)


def test_a_versioned_document_must_carry_its_records_under_records():
    """A batch that names its version owes the key that version emits.

    Every `document_version` discovery-engine has released puts the record list
    under `records`. A document declaring a version and carrying `results`
    instead did not come from that producer, whatever else is true of it, and
    importing it would attach discovery-engine provenance to a document
    discovery-engine did not write.
    """
    body = json.dumps(
        {
            "document_version": 3,
            "session_id": "s1",
            "results": [{"title": "One"}],
            "record_count": 1,
        }
    ).encode()

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(body)

    message = str(exc.value)
    assert "records" in message
    assert "document_version" in message


def test_a_versioned_document_under_records_still_imports():
    """Non-vacuity: the real producer's real document must still pass.

    Guards against a fix that satisfies every refusal above by refusing every
    versioned document.
    """
    body = json.dumps(
        {
            "document_version": 3,
            "session_id": "s1",
            "records": [{"title": "A"}, {"title": "B"}],
            "record_count": 2,
            "prisma": {"raw_total": 9},
        }
    ).encode()

    records, provenance = parse_discovery_session_export(body)

    assert len(records) == 2
    assert provenance is not None
    assert provenance["raw_total"] == 9


def test_an_unversioned_body_with_one_key_is_still_accepted_under_any_of_the_three():
    """The tolerance that predates the frozen batch stays, for a single key.

    These bodies are not discovery-engine documents and never claimed to be.
    Nothing here is ambiguous, so there is nothing to refuse — narrowing to
    `records` would break working producers to fix a defect they do not have.
    """
    for key in ("results", "records", "items"):
        body = json.dumps({"session_id": "s1", key: [{"title": "A study"}]}).encode()
        records, _ = parse_discovery_session_export(body)
        assert len(records) == 1, f"{key} alone must still import"


def test_a_body_with_none_of_the_three_keys_is_still_refused_as_before():
    """The pre-existing refusal is unchanged — a renamed key still lands loudly.

    `GET /api/sessions/{id}/export` nests its records in a JSON string under
    `papers`, and that is still not importable. This test pins that the fix did
    not widen the accepted set on its way past.
    """
    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(b'{"session_id": "s1", "papers": [{"title": "A"}]}')

    assert "carries no result list" in str(exc.value)
