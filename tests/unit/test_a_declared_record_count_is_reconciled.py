"""`record_count` is a self-describing field, and nothing was reading it.

`discovery-engine` has written `record_count` into the sealed bytes since
`document_version` 1 (`review_export_service._document`: ``"record_count":
len(records)``). It travels INSIDE the digested document, so it is not a
transport header a proxy can rewrite — it is the producer's own statement of how
many records the document contains, frozen at the same instant as the records.

Before this module, `record_count` appeared nowhere in this service's `src/`
except one docstring. A document declaring 999 records against one record
imported as one record, `201`, and a document declaring 0 against one record did
the same. The number the producer froze specifically so that a consumer could
disagree with it was the one number the consumer never read.

## Why the digest does not already cover this

On `/imports/from-discovery` the bytes are hashed and checked against
`expected_digest`, so a document truncated *in transit* is already caught. This
check is not a second copy of that one. It catches the case where the document
arrives WHOLE and the parser still produces the wrong number of records —
because it read the wrong key. `{"record_count": 93, "results": [one item],
"records": [93 items]}` is a byte-perfect document with a valid digest, and the
first-key-present loop took `results`. `record_count` is what makes the two
halves of that disagreement visible to each other, which is why this test module
and `test_the_result_list_key_is_chosen_by_the_producers_contract` were written
together.

It also covers every route where no digest is checked at all: `POST
/imports?source_kind=...` hashes whatever arrived and compares it to nothing.
"""

from __future__ import annotations

import json

import pytest

from infrastructure.imports.import_parser import (
    UnsupportedFormatError,
    parse_discovery_session_export,
)


def _document(**overrides: object) -> bytes:
    """A `document_version` 3 batch in the shape discovery-engine emits."""
    body: dict[str, object] = {
        "document_version": 3,
        "session_id": "019fe51a-ccf7-72af-8367-347f5872ee19",
        "records": [{"title": "A study", "doi": "10.1000/a"}],
        "record_count": 1,
    }
    body.update(overrides)
    return json.dumps(body).encode()


def test_a_document_declaring_more_records_than_it_carries_is_refused():
    """The short-document case: 999 declared, one delivered.

    This is the shape a truncated or visibility-scoped export takes. It used to
    import as a one-record corpus with a `201`, and `identified_total` is
    derived from `import_rows`, so the reviewer went on to seal and screen a
    corpus 998 records short of the one the producer sealed — with nothing
    downstream able to disagree.
    """
    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(_document(record_count=999))

    # BOTH numbers, because "a count mismatch" does not tell the reader whether
    # to go looking at the producer or at the transport. 999-against-1 is a
    # truncation; 1-against-999 is a producer that miscounted.
    assert "999" in str(exc.value)
    assert "1" in str(exc.value)


def test_a_document_declaring_zero_records_against_a_record_is_refused():
    """`record_count: 0` is not "empty"; it is a document disagreeing with itself.

    Kept separate from the case above because zero is the value a falsy check
    swallows. `if declared:` would treat "the producer says this document is
    empty" as "the producer said nothing" and import the records anyway.
    """
    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(_document(record_count=0))

    assert "0" in str(exc.value)


def test_a_document_whose_count_agrees_imports():
    """The non-vacuity half: the gate must pass the document it is meant to pass.

    Without this, every assertion above is satisfied by a parser that refuses
    every document with a `record_count` at all.
    """
    records, _ = parse_discovery_session_export(_document())

    assert len(records) == 1
    assert records[0].quarantine_reason is None


def test_an_empty_document_that_says_so_still_parses():
    """`record_count: 0` against `records: []` agrees, and agreement is an import.

    Whether an empty corpus may be RECORDED is `import_payload`'s decision
    (`allow_empty`), not this parser's. The count check must not quietly become
    a second, differently-worded emptiness refusal.
    """
    records, _ = parse_discovery_session_export(_document(records=[], record_count=0))

    assert records == []


def test_a_legacy_document_with_no_count_and_no_version_still_imports():
    """Absence is not disagreement, for a producer that never claimed anything.

    `record_count` is absent from every non-discovery-engine producer this
    parser serves and from every hand-built `{"results": [...]}` body in this
    suite. There is nothing to reconcile against, so refusing here would break
    working producers to punish a field they never promised.
    """
    records, _ = parse_discovery_session_export(b'{"session_id": "s1", "results": [{"title": "A study"}]}')

    assert len(records) == 1


def test_a_versioned_document_that_omits_its_own_count_is_refused():
    """...but a document that says it is a discovery-engine batch owes the count.

    This is where "absent is fine" stops. `document_version` is the producer
    identifying itself, and EVERY version of that document — 1, 2 and 3 — emits
    `record_count` unconditionally. So a document carrying `document_version`
    and no `record_count` is not an old document; it is an incoherent one, or a
    field someone stripped. Accepting it would make the reconciliation above
    opt-out: any producer that wanted its count unchecked need only omit it.
    """
    body = json.dumps({"document_version": 3, "session_id": "s1", "records": [{"title": "A study"}]}).encode()

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(body)

    assert "record_count" in str(exc.value)


@pytest.mark.parametrize("bad", ["1", 1.5, True, None, [1]])
def test_a_count_that_is_not_a_whole_number_is_refused(bad: object):
    """`"1"` is not 1, and `True` is not 1 either.

    `record_count` reaches a comparison against `len(items)`. Python would
    happily evaluate `True == 1` as agreement and `"1" == 1` as disagreement,
    so a producer emitting a stringified count would be reported as a corpus
    mismatch — sending the reader to look for missing records that are all
    present. The shape is refused as a shape, with its own message.
    """
    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(_document(record_count=bad))

    assert "record_count" in str(exc.value)
