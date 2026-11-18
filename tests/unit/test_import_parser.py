"""Bibliographic import parsing — the adapters in `infrastructure.imports.import_parser`.

Most of these tests exist because of what the parsers do to a *corpus*, not
because of what they do to a string:

* an identifier read out of the wrong RIS tag becomes `normalized_pmid`, which
  `domain.policies.deduplication_engine` treats as the second-strongest merge
  rule — so a mis-read tag merges two unrelated papers, and a tag read too
  timidly leaves the same paper in the corpus twice. Both directions are
  asserted through the real engine, at cluster level, because "the parsed pmid
  is `None`" is not the harm — "the reviewer is asked to choose a survivor
  between two unrelated studies" is;
* a payload that parses to zero records used to be indistinguishable from a
  legitimately empty source, which is the "empty success defeats evidence gates"
  failure: an import reporting `201, accepted=0` is a corpus that silently is not
  there;
* a malformed payload that reaches the caller as a `500` — or worse, as a `409`
  carrying a raw PostgreSQL string — is an import failure reported as something
  the caller cannot act on;
* a mangled title is what a reviewer, an export and a PRISMA appendix all show.

Fixtures are inline byte strings deliberately: every one is a shape a real
exporter emits, and a golden file would hide which byte is the point.

The one exception is `tests/fixtures/discovery_engine_session_export.json`,
where the *document* is the point rather than a byte in it: it is what
`discovery-engine`'s `ExportService.export_json` actually emits, nested
`external_ids` and all. Every abbreviated inline version of that payload written
here before was written flat — which is precisely how the nesting went unnoticed
until every DOI in an imported corpus was null.
"""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path
from unittest.mock import ANY

import pytest

from actor import Actor, ActorRole
from api.problem_details import EmptySourceNotDeclaredError, ProblemError, UnrecognizedPayloadError
from application.services import imports as import_service
from domain.policies.deduplication_engine import CandidateRecord, build_clusters, find_pairs
from domain.policies.denylist import find_forbidden_keys
from infrastructure.imports import import_parser
from infrastructure.imports.import_parser import (
    ImportTooLargeError,
    NoRecordsParsedError,
    ParsedRecord,
    UnreadablePayloadError,
    UnsupportedFormatError,
    parse_bibtex,
    parse_canonical_json,
    parse_csv,
    parse_discovery_session_export,
    parse_ris,
)
from models.records import ImportBatch, ReviewRecordVersion
from models.reviews import Review
from tests.tenants import tenant

pytestmark = pytest.mark.unit


def _only(records: list[ParsedRecord]) -> ParsedRecord:
    assert len(records) == 1, f"expected exactly one record, got {len(records)}"
    return records[0]


def _candidates(records: list[ParsedRecord]) -> list[CandidateRecord]:
    out = []
    for record in records:
        normalized = record.normalized()
        out.append(
            CandidateRecord(
                review_record_id=uuid.uuid4(),
                review_record_version_id=uuid.uuid4(),
                normalized_doi=normalized["normalized_doi"],
                normalized_pmid=normalized["normalized_pmid"],
                normalized_title=normalized["normalized_title"],
                publication_year=record.publication_year,
            )
        )
    return out


def _clusters(records: list[ParsedRecord]) -> int:
    """What the reviewer is actually shown: merge proposals, through the real engine."""
    return len(build_clusters(find_pairs(_candidates(records))))


# ---------------------------------------------------------------------------
# P0-1 — a RIS accession/reference number is not a PMID
# ---------------------------------------------------------------------------


def test_web_of_science_accession_does_not_become_a_pmid():
    """Web of Science emits `AN  - WOS:000123456789`.

    Read as a PMID it normalizes to `000123456789`, and two Web of Science
    records that happen to share nothing else would still be compared on it.
    """
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A study\nAN  - WOS:000123456789\nER  - \n"))
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None
    # The accession is evidence and must survive, just not as an identifier we
    # claim to understand.
    assert record.raw["AN"] == "WOS:000123456789"


def test_scopus_accession_does_not_become_a_pmid():
    """Scopus emits `AN  - 2-s2.0-85012345678` — digits, but not PubMed's."""
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A study\nAN  - 2-s2.0-85012345678\nER  - \n"))
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None


def test_embase_accession_does_not_become_a_pmid():
    """Embase emits `AN  - L629345678` and `AN  - 2019123456`.

    The second is bare digits, so digit-plausibility alone would accept it. It is
    rejected because nothing in the record says the source was PubMed.
    """
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - L629345678\nER  - \n")).pmid is None
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - 2019123456\nER  - \n")).pmid is None


def test_the_ris_reference_id_is_never_read_as_a_pmid():
    """`ID  - 47` is the library-local Reference ID every manager numbers 1..N."""
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A study\nID  - 47\nER  - \n"))
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None


def test_an_ovid_medline_accession_is_a_pmid():
    """The case the AN read exists for: Ovid MEDLINE's accession IS the PMID."""
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A study\nDB  - Ovid MEDLINE(R)\nAN  - 33933206\nER  - \n"))
    assert record.pmid == "33933206"
    assert record.normalized()["normalized_pmid"] == "33933206"


def test_a_self_labelled_pmid_is_read_without_a_database_tag():
    """EndNote writes `PMID: 12345678` into the accession, custom or note field."""
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - PMID: 33933206\nER  - \n")).pmid == "33933206"
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nN1  - PMID: 33933206\nER  - \n")).pmid == "33933206"
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nC2  - PMID:33933206\nER  - \n")).pmid == "33933206"
    # Even a labelled value must be plausibly a PMID: nine digits is outside the
    # namespace, so it is refused rather than truncated to eight.
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - PMID: 339332061\nER  - \n")).pmid is None


# ---------------------------------------------------------------------------
# P0-1a — a labelled PMID inside a MEDLINE relational note belongs to ANOTHER paper
# ---------------------------------------------------------------------------

#: Two entirely unrelated papers. Each carries a MEDLINE relational note, which
#: every RIS exporter writes into `N1`, and each note cites the SAME commentary —
#: which is ordinary: one editorial can comment on several papers.
_COMMENT_IN_NOTE = b"""TY  - JOUR
TI  - Statin therapy in acute stroke
PY  - 2020
AN  - WOS:000123456789
N1  - Comment in: Lancet. 2021 Jan 30;397(10272):341. PMID: 33357499
ER  -
"""

_ERRATUM_IN_NOTE = b"""TY  - JOUR
TI  - Anticoagulation in atrial fibrillation
PY  - 2019
AN  - L629345678
N1  - Erratum in: N Engl J Med. 2021 Jan 14;384(2):199. PMID: 33357499
ER  -
"""


def test_a_pmid_inside_a_medline_comment_note_is_not_this_records_pmid():
    """`Comment in:` cites the commentary. Its PMID is the commentary's, not ours."""
    record = _only(parse_ris(_COMMENT_IN_NOTE))
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None
    # The note is evidence and survives verbatim; it is simply not an identifier.
    assert "33357499" in record.raw["N1"]


def test_a_pmid_inside_a_medline_erratum_note_is_not_this_records_pmid():
    assert _only(parse_ris(_ERRATUM_IN_NOTE)).pmid is None


def test_two_papers_sharing_a_commented_on_pmid_are_not_proposed_as_duplicates():
    """The corpus-level consequence: a Web of Science paper and an Embase paper.

    They share nothing — not a DOI, not a title, not a year — except that one
    editorial commented on both. Reading the note's PMID as theirs put them in
    one cluster and asked a reviewer to choose a survivor between two unrelated
    studies, discarding one of them from the review.
    """
    records = parse_ris(_COMMENT_IN_NOTE) + parse_ris(_ERRATUM_IN_NOTE)
    assert len(records) == 2
    assert [r.pmid for r in records] == [None, None]
    assert _clusters(records) == 0


def test_a_note_that_begins_with_its_own_pmid_is_still_read():
    """The Zotero/EndNote shape the `N1` scan exists for, and the rule that separates them.

    A note that STARTS with the label is the record identifying itself; a note
    where prose comes first is describing some other article. That is the whole
    distinction, and it survives the exporter dropping the `Comment in:` prefix
    and leaving a bare citation, which no keyword blocklist would.
    """
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nN1  - PMID: 33933206\nER  - \n")).pmid == "33933206"
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nN1  - PMID: 33933206 PMCID: PMC8084355\nER  - \n")).pmid == "33933206"
    bare_citation = b"TY  - JOUR\nTI  - A\nN1  - Lancet. 2021 Jan 30;397(10272):341. PMID: 33357499\nER  - \n"
    assert _only(parse_ris(bare_citation)).pmid is None


def test_an_ovid_export_and_a_zotero_export_of_one_paper_still_merge():
    """The two shapes the PMID read exists to serve, merging across each other.

    Ovid puts the PMID in `AN` under `DB  - Ovid MEDLINE(R)`; Zotero puts it in
    `N1` behind a `PMID:` label. The titles differ (Ovid keeps the trial
    acronym), so `normalized_title` cannot merge them and neither export carries
    a DOI — the PMID is the only thing that can. Tightening the note rule must
    not cost this.
    """
    ovid = parse_ris(
        b"TY  - JOUR\nTI  - Dexamethasone in hospitalised patients with COVID-19 (RECOVERY)\n"
        b"PY  - 2021\nDB  - Ovid MEDLINE(R)\nAN  - 32678530\nER  - \n"
    )
    zotero = parse_ris(
        b"TY  - JOUR\nTI  - Dexamethasone in hospitalised patients with Covid-19\n"
        b"PY  - 2021\nID  - 4\nN1  - PMID: 32678530\nER  - \n"
    )
    assert [r.pmid for r in ovid + zotero] == ["32678530", "32678530"]
    assert _clusters(ovid + zotero) == 1


def test_a_self_identifying_note_wins_over_a_relational_one_in_the_same_record():
    """Real MEDLINE records carry both. The record's own PMID must still be found."""
    payload = (
        b"TY  - JOUR\nTI  - A\n"
        b"N1  - Comment in: Lancet. 2021;397(10272):341. PMID: 33357499\n"
        b"N1  - PMID: 33933206\n"
        b"ER  - \n"
    )
    assert _only(parse_ris(payload)).pmid == "33933206"


def test_a_bare_custom_field_is_not_read_as_a_pmid():
    """`C2` is RIS "Custom 2" — some styles put a PMID there, others a PMCID.

    Deliberately refused: this is the conservative half of the trade. A missing
    PMID costs a dedup rule; a wrong one merges two papers.
    """
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nC2  - 33933206\nER  - \n")).pmid is None


def test_a_pubmed_accession_that_is_not_plausibly_a_pmid_is_refused():
    """`DB  - PubMed` is not a licence to accept any string in `AN`."""
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A\nDB  - PubMed\nAN  - PMC7654321\nER  - \n"))
    assert record.pmid is None


# ---------------------------------------------------------------------------
# P0-1b — a bare `AN` PMID in an export that omits `DB`, which most of them do
# ---------------------------------------------------------------------------

#: The same paper twice, from two databases that title it differently — one
#: carries the full RECOVERY subtitle, the other does not, so `normalized_title`
#: cannot merge them. Both carry the PMID in `AN` with no `DB` tag, which is what
#: EndNote and a plain PubMed RIS export both emit.
_COPY_A = b"""TY  - JOUR
TI  - Tocilizumab in hospitalised patients with severe COVID-19 (RECOVERY): a randomised, controlled trial
PY  - 2021
AN  - 33933206
ER  -
"""

_COPY_B = b"""TY  - JOUR
TI  - Tocilizumab in hospitalised patients with COVID-19
PY  - 2021
AN  - 33933206
ER  -
"""


def test_a_bare_accession_is_a_pmid_when_the_export_declares_no_database_at_all():
    """Requiring `DB` cost real dedup: most exporters do not write one."""
    assert _only(parse_ris(_COPY_A)).pmid == "33933206"
    assert _only(parse_ris(_COPY_B)).pmid == "33933206"


def test_two_copies_of_one_paper_still_merge_without_a_database_tag():
    """The corpus-level consequence, and the reason a blanket `DB` requirement is wrong.

    Nothing else can merge these: the two databases titled the paper
    differently, so the title+year rule cannot see them as one, and neither
    export carries a DOI. Dropping the bare `AN` read left the same study in the
    corpus twice, counted twice in `identified_total`, and screened twice —
    possibly to opposite decisions.
    """
    records = parse_ris(_COPY_A) + parse_ris(_COPY_B)
    assert len(records) == 2
    assert records[0].normalized()["normalized_title"] != records[1].normalized()["normalized_title"]
    assert _clusters(records) == 1


def test_an_uncorroborated_accession_must_still_look_like_a_modern_pmid():
    """With no `DB` to vouch for it, the value's own shape is the only evidence.

    Seven digits and no leading zero is every PMID assigned since 1976. A short
    or zero-padded number is an ordinal or a padded accession, and admitting it
    would re-open the `ID  - 1..N` false-merge from the other direction.
    """
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - 47\nER  - \n")).pmid is None
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - 00123456\nER  - \n")).pmid is None
    assert _only(parse_ris(b"TY  - JOUR\nTI  - A\nAN  - 1234567\nER  - \n")).pmid == "1234567"


def test_a_declared_non_pubmed_database_refuses_a_bare_accession_whatever_its_shape():
    """The `DB` tag is still authoritative when it is present — it just is not required.

    Embase's ten-digit accessions are already out on length, but an eight-digit
    one from a database that names itself must not be read as a PMID merely
    because it is the right size.
    """
    payload = b"TY  - JOUR\nTI  - A\nDB  - Embase\nAN  - 33933206\nER  - \n"
    assert _only(parse_ris(payload)).pmid is None


# ---------------------------------------------------------------------------
# P0-1c — `PubMed Central` contains the word "PubMed" and is a different namespace
# ---------------------------------------------------------------------------


def test_a_pubmed_central_accession_does_not_become_a_pmid():
    """`DB  - PubMed Central` matched a naive `pubmed|medline`, so a PMC accession
    was published into PubMed's number space, where it collides with a real PMID."""
    record = _only(parse_ris(b"TY  - JOUR\nTI  - A study\nDB  - PubMed Central\nAN  - 8123456\nER  - \n"))
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None


def test_a_pmc_accession_and_the_pmid_it_collides_with_are_not_merged():
    """A PMC accession and a PMID can be the same digits and mean different papers."""
    pmc = parse_ris(b"TY  - JOUR\nTI  - A study of statins\nPY  - 2020\nDB  - PMC\nAN  - 8123456\nER  - \n")
    pmid = parse_ris(b"TY  - JOUR\nTI  - A trial of anticoagulants\nPY  - 2019\nDB  - MEDLINE\nAN  - 8123456\nER  - \n")
    assert [r.pmid for r in pmc + pmid] == [None, "8123456"]
    assert _clusters(pmc + pmid) == 0


#: Two RIS exports of two different database searches, each numbered from 1 by
#: the exporting reference manager. This is the ordinary way a reviewer builds a
#: corpus, and every record in one file shares an `ID` with a record in the other.
_SEARCH_A = b"""TY  - JOUR
TI  - Tocilizumab in hospitalised patients with COVID-19
PY  - 2021
ID  - 1
ER  -

TY  - JOUR
TI  - Dexamethasone in hospitalised patients with COVID-19
PY  - 2021
ID  - 2
ER  -
"""

_SEARCH_B = b"""TY  - JOUR
TI  - Baricitinib plus remdesivir for hospitalised adults
PY  - 2020
ID  - 1
ER  -

TY  - JOUR
TI  - Convalescent plasma in critically ill patients
PY  - 2021
ID  - 2
ER  -
"""


def test_two_ris_searches_with_overlapping_reference_ids_produce_no_duplicate_pairs():
    """The corpus-level consequence of P0-1, asserted through the real engine.

    Four distinct papers. Before the fix every one of them false-matched its
    same-ordinal partner in the other file, `matched_on="normalized_pmid"`.
    """
    candidates = _candidates(parse_ris(_SEARCH_A) + parse_ris(_SEARCH_B))
    assert len(candidates) == 4
    assert find_pairs(candidates) == []


# ---------------------------------------------------------------------------
# P0-2 — zero records out of a non-empty payload is a failure, not an empty corpus
# ---------------------------------------------------------------------------


def test_a_utf16_ris_export_is_refused_rather_than_reported_as_empty():
    """EndNote on Windows exports UTF-16. Every byte is there; none is readable."""
    text = "TY  - JOUR\nTI  - A study\nER  - \n"
    with pytest.raises(UnreadablePayloadError, match="UTF-16"):
        parse_ris(text.encode("utf-16"))


def test_a_payload_that_is_not_ris_at_all_is_refused():
    with pytest.raises(NoRecordsParsedError):
        parse_ris(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n")


def test_ris_content_declared_as_bibtex_is_refused():
    with pytest.raises(NoRecordsParsedError):
        parse_bibtex(b"TY  - JOUR\nTI  - A study\nER  - \n")


def test_a_csv_with_no_header_row_at_all_is_refused():
    with pytest.raises(NoRecordsParsedError):
        parse_csv(b"")


def test_a_csv_whose_header_names_nothing_we_recognise_is_refused():
    """A header naming no column we understand is not a CSV of records."""
    with pytest.raises(NoRecordsParsedError):
        parse_csv(b"this is not a csv at all\n")


def test_a_pdf_declared_as_csv_is_refused_rather_than_quarantined():
    """P1-9. `%PDF-1.7` parses as a header row with two data rows under it.

    Gating the header check on "zero rows AND nothing recognised" let this
    through as `201` with two quarantined rows, telling the caller their PDF had
    imported and blaming a missing title column. The row count says nothing
    about whether the file is a CSV of records; only the header does.
    """
    with pytest.raises(NoRecordsParsedError, match="header row"):
        parse_csv(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n")


def test_a_header_only_csv_parses_to_no_records():
    """A search that returned nothing still exports its header row.

    The parser's answer is `[]`, not an exception: this IS a well-formed CSV of
    records that happens to have none. Whether an empty corpus may be *recorded*
    is the import service's decision — see
    `test_a_parseable_but_empty_payload_is_refused_unless_the_caller_declares_it`.
    """
    assert parse_csv(b"Title,DOI,Authors\n") == []
    assert parse_csv(b"Title") == []


def test_canonical_json_can_declare_itself_empty():
    assert parse_canonical_json(b'{"records": []}') == []
    assert parse_canonical_json(b"[]") == []


def test_a_discovery_export_with_null_results_is_refused():
    """`{"results": null}` is upstream saying something went wrong, not "none"."""
    with pytest.raises(UnsupportedFormatError):
        parse_discovery_session_export(b'{"session_id": "s1", "results": null}')


def test_a_discovery_export_with_no_result_key_at_all_is_refused():
    """The upstream-key-rename case: a renamed key must not read as an empty corpus."""
    with pytest.raises(UnsupportedFormatError):
        parse_discovery_session_export(b'{"session_id": "s1", "hits": [{"title": "A study"}]}')


def test_a_discovery_export_can_declare_itself_empty():
    records, provenance = parse_discovery_session_export(b'{"session_id": "s1", "results": [], "raw_total": 0}')
    assert records == []
    assert provenance == {"session_id": "s1", "raw_total": 0}


def test_the_mutable_export_body_is_still_refused_and_that_refusal_is_load_bearing():
    """The `/export` shape stays unimportable ON PURPOSE, now that a fix exists.

    This is the byte shape `GET /api/sessions/{id}/export?format=json` returns —
    `SessionExport` in `discovery-engine/src/schemas/session.py` is
    `{session_id, format, data, paper_count}`, and `data` is a JSON **string**
    whose object nests the records under **`papers`**.

    Its predecessor said *"when this test goes red, the seam has been
    connected"*. The seam WAS connected, on 2026-08-13 — and this test did not
    go red, which is the outcome the design wanted. The fix landed upstream as
    `discovery-engine`'s frozen `review-export` batch, not as a second accepted
    key here, so this body is still refused and must stay refused: a session
    export is mutated by every later search, so a shape merely tolerated here
    would look reproducible without being so.

    Accepting `papers` would now be strictly worse than before, because a
    reproducible alternative exists — a caller could import the mutable body and
    believe they had the citable one. See
    :func:`test_the_frozen_review_export_batch_is_the_shape_that_imports`.
    """
    body = json.dumps(
        {
            "session_id": "s1",
            "format": "json",
            "paper_count": 1,
            "data": json.dumps({"session_id": "s1", "papers": [{"id": "p1", "title": "A study"}]}),
        }
    ).encode()

    with pytest.raises(UnsupportedFormatError) as exc:
        parse_discovery_session_export(body)
    assert "no result list" in str(exc.value)


def test_the_provenance_arm_harvests_a_block_and_absence_stays_absent():
    """The arm that "had never run" now runs against the live producer.

    Its predecessor asserted the opposite in its name and docstring: *"no live
    export carries prisma provenance"*. That was true when written and is false
    now — `discovery-engine`'s frozen `review-export` batch emits the block, and
    it was measured through THIS function inside the running `evidence-review`
    pod on 2026-08-13 against the real 93-paper `tocilizumab covid-19
    randomised` session:

        records    : 93, quarantined 0
        provenance : per_source_counts {'europe_pmc': 623, 'pubmed': 623,
                     'semantic_scholar': 75}, raw_total 1321,
                     deduped_total None

    `deduped_total` arrives as `None` **by the producer's deliberate choice**,
    not for want of a value — summing its per-query figures double-counts a
    paper two queries both found — so the `None` below is the real shape, not a
    placeholder waiting to be filled in.

    Asserted both ways so neither half can rot: a body with the block yields it,
    and a body without still imports with `None`.
    """
    with_block = b'{"results": [{"title": "A study"}], "raw_total": 7, "deduped_total": 5}'
    records, provenance = parse_discovery_session_export(with_block)
    assert len(records) == 1
    assert provenance is not None
    assert provenance["raw_total"] == 7

    without_block = b'{"results": [{"title": "A study"}]}'
    records, provenance = parse_discovery_session_export(without_block)
    assert len(records) == 1
    assert provenance is None, (
        "an export with no provenance block must yield None, not an empty dict: the two say "
        "different things and the ledger reports one of them as 'not stated'"
    )


def test_the_frozen_review_export_batch_is_the_shape_that_imports():
    """The replacement the old seam test asked for: an assertion on the new shape.

    This is `discovery-engine`'s `GET /api/sessions/{id}/review-export/{batch_id}`
    body, reduced to two records but structurally identical to the 463,808-byte
    document that was parsed here against the live 93-paper session. What makes
    it importable where `/export` is not:

    * the records are at the TOP level under `records`, not inside a JSON string;
    * the PRISMA block is nested under `prisma`, which the harvest loop reads;
    * identifiers arrive BOTH flat and under `external_ids`, so the primary key
      is populated and the adapter's own spelling survives beside it.

    The `search_strategy` block is deliberately unread by this parser and
    deliberately present in the payload: it is the trace from corpus back to
    search, and it lives inside the digested bytes so it cannot be edited
    afterwards without breaking the seal.
    """
    body = json.dumps(
        {
            "document_version": 1,
            "session_id": "019fe51a-ccf7-72af-8367-347f5872ee19",
            "session_name": "tocilizumab covid-19 randomised",
            "taken_at": "2026-08-13T06:03:08+00:00",
            "record_count": 2,
            "records": [
                {
                    "id": "p1",
                    "title": "Tocilizumab in hospitalised patients with COVID-19",
                    "publication_year": 2021,
                    "doi": "10.1056/NEJMoa2028836",
                    "pmid": "33332779",
                    "external_ids": {"doi": "10.1056/NEJMoa2028836", "pubmed_id": "33332779"},
                    "authors": ["Stone JH", "Frigault MJ"],
                },
                {
                    "id": "p2",
                    "title": "Interleukin-6 receptor antagonists in critically ill patients",
                    "publication_year": 2021,
                    "doi": None,
                    "pmid": None,
                    # Nested-only, and lowercase — the semantic_scholar spelling.
                    "external_ids": {"pubmed_id": "33631065"},
                    "authors": [],
                },
            ],
            "search_strategy": [
                {"id": "q1", "query_text": "tocilizumab covid-19 randomised", "raw_total": 1321},
            ],
            "prisma": {
                "session_id": "019fe51a-ccf7-72af-8367-347f5872ee19",
                "per_source_counts": {"pubmed": 623, "europe_pmc": 623, "semantic_scholar": 75},
                "raw_total": 1321,
                "deduped_total": None,
                "deduped_total_basis": "not recorded: summing per-query figures double-counts",
            },
        }
    ).encode()

    records, provenance = parse_discovery_session_export(body)

    assert len(records) == 2
    assert [r.quarantine_reason for r in records] == [None, None]

    assert provenance is not None, "the block is nested under `prisma`; the harvest must read it"
    assert provenance["raw_total"] == 1321
    assert provenance["per_source_counts"]["europe_pmc"] == 623
    assert provenance["deduped_total"] is None

    # The identifier seam: flat where the producer has one, nested-only otherwise.
    assert records[0].doi == "10.1056/NEJMoa2028836"
    assert records[0].pmid == "33332779"
    assert records[1].doi is None
    assert records[1].pmid == "33631065", (
        "the nested `pubmed_id` fallback did not fire; a dropped identifier does not fail an "
        "import, it silently reduces two of the three dedup rules to no-ops on that record"
    )


#: The subject every stubbed import here acts as. `_NoExistingBatch.get` hands
#: back a review created by this same subject, so the lead gate resolves through
#: the creator-governs-their-own-review branch of `require_review_lead`.
_LEAD_SUBJECT = "lead-carol"


class _NoExistingBatch:
    """Just enough `Session` for `import_payload`, and nothing that touches a database.

    The lookup that runs before parsing (answering "no prior batch with these
    bytes"), plus `add`/`flush`, so the *successful* paths can be asserted here
    too. Every rule under test is a decision this service makes before it
    persists anything, so a real session would only add setup cost and a
    Postgres dependency to a unit test.

    `get` answers the lead-authority gate that `import_payload` acquired when
    `require_review_lead_for` was pushed down from the router into the service.
    These tests were written against the ungated version and every one of them
    died on `AttributeError: '_NoExistingBatch' object has no attribute 'get'`
    once the two branches met — the gate is real and it runs before any parsing.

    It returns a review whose `created_by_subject_id` IS the acting subject
    rather than a permissive object that says yes to everything: the stub has to
    satisfy the gate the way a real caller does, so that a future change which
    tightens the gate fails here instead of being quietly absorbed.

    `first` answers the SECOND gate, `require_review_open`, added when import was
    found to accept records into a closed review. It reports `active`, which is
    the only state under which these parsing rules are reachable at all — and
    reporting it rather than a permissive stub is the same choice as above:
    twenty-five tests here failed the moment that gate landed, which is the stub
    doing its job.

    `_insert_pk` and the `ImportBatch` branch of `get` answer the THIRD change of
    this kind. `0012` gave `import_batches` the unique constraint
    `uq_import_batch_payload_digest` that the module docstring's "the digest plus
    the transaction IS the guarantee" had always assumed and never had — measured
    false with two threads, two batches for one payload — and `import_payload`
    now writes the batch with `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`
    instead of `session.add`, so that a losing writer replays the winner's batch
    rather than importing twice.

    That is a change in the statement the stub has to answer, so the stub answers
    it: an `Insert` returns the primary key it was handed (this stub is always the
    WINNER — the loser's path needs two real sessions and is proved in
    `tests/integration/test_a_second_writer_cannot_mint_a_twin.py`), everything
    else keeps returning `None`. Distinguishing on the statement TYPE rather than
    returning a fixed value keeps `scalar_one_or_none() is None` meaning "no prior
    batch" for the pre-check, which is the answer these tests are built on.
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self._insert_pk: object = None

    def execute(self, statement=None, *_args, **_kwargs):  # noqa: ANN001
        from sqlalchemy.sql import Insert

        self._insert_pk = None
        if isinstance(statement, Insert):
            # The value `import_payload` generated for this insert, read back off
            # the compiled statement rather than invented here, so a stub that
            # drifts from the caller shows up as a mismatch instead of passing.
            values = getattr(statement, "_values", None) or {}
            for column, value in values.items():
                if getattr(column, "name", None) == "import_batch_id":
                    self._insert_pk = getattr(value, "value", value)
        return self

    def scalar_one_or_none(self):
        return self._insert_pk

    def first(self):
        return ("active", 1)

    def get(self, model: object, key: uuid.UUID):
        if model is ImportBatch:
            # The row the insert above just wrote. `import_payload` mutates its
            # counts and returns a summary of it, so it has to be a real
            # `ImportBatch`, not the review this stub answers every other `get`
            # with.
            return ImportBatch(
                import_batch_id=key,
                review_id=uuid.uuid4(),
                tenant_id=tenant("unit"),
                source_kind="canonical_json",
                raw_payload_digest="stub",
                submitted_by_subject_id=_LEAD_SUBJECT,
            )
        return Review(
            review_id=key,
            tenant_id=tenant("unit"),
            project_id="p",
            external_key=f"k-{key}",
            title="stub",
            created_by_subject_id=_LEAD_SUBJECT,
        )

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None


def test_an_unparseable_import_reaches_the_caller_as_4xx_not_201():
    """The whole point of P0-2: this used to be `201, accepted=0, quarantined=0`."""
    actor = Actor(tenant_id=tenant("unit"), subject_id=_LEAD_SUBJECT, role=ActorRole.LEAD)
    with pytest.raises(UnrecognizedPayloadError) as raised:
        import_service.import_payload(
            _NoExistingBatch(),  # type: ignore[arg-type]
            actor,
            uuid.uuid4(),
            source_kind="ris",
            payload="TY  - JOUR\nTI  - A study\nER  - \n".encode("utf-16"),
        )
    assert isinstance(raised.value, ProblemError)
    assert 400 <= raised.value.status < 500
    assert raised.value.slug == "unrecognized_payload"


def _import(source_kind: str, payload: bytes, *, allow_empty: bool = False):
    return import_service.import_payload(
        _NoExistingBatch(),  # type: ignore[arg-type]
        Actor(tenant_id=tenant("unit"), subject_id=_LEAD_SUBJECT, role=ActorRole.LEAD),
        uuid.uuid4(),
        source_kind=source_kind,
        payload=payload,
        allow_empty=allow_empty,
    )


#: Every payload the adversary demonstrated importing as `201 accepted=0
#: quarantined=0`. The five-byte `Title` is the one that shows why a
#: format-level gate is not enough: any file whose first line happens to contain
#: one recognised alias word was a successful import of nothing.
_PARSEABLE_BUT_EMPTY = [
    ("canonical_json", b'{"records": []}'),
    ("canonical_json", b"[]"),
    ("discovery_session", b'{"results": []}'),
    ("discovery_session", b'{"items": []}'),
    ("csv", b"title,doi,authors\n"),
    ("csv", b"Title"),
    ("csv", b"pmid"),
]


@pytest.mark.parametrize(("source_kind", "payload"), _PARSEABLE_BUT_EMPTY)
def test_a_parseable_but_empty_payload_is_refused_unless_the_caller_declares_it(source_kind: str, payload: bytes):
    """P1-7. A `201` with `accepted=0` is the shape of a successful import.

    `identified_total` is derived from `import_rows`, so a batch of nothing
    seals a corpus, screens it and exports a PRISMA count that is internally
    consistent and describes a corpus that was never there. Nothing downstream
    can disagree with it, which is exactly why nothing downstream notices.
    """
    with pytest.raises(EmptySourceNotDeclaredError) as raised:
        _import(source_kind, payload)
    assert raised.value.status == 422
    assert raised.value.slug == "empty_source_not_declared"


@pytest.mark.parametrize(("source_kind", "payload"), _PARSEABLE_BUT_EMPTY)
def test_a_genuinely_empty_search_stays_expressible(source_kind: str, payload: bytes):
    """A database that returned nothing is a real PRISMA row and must be recordable.

    The gate turns it from an accident nobody sees into an assertion by a named
    human, which is the only difference that matters.
    """
    batch = _import(source_kind, payload, allow_empty=True)
    assert batch["accepted_count"] == 0
    assert batch["quarantined_count"] == 0


def test_declaring_allow_empty_does_not_excuse_a_file_that_is_not_the_declared_format():
    """`allow_empty` asserts "the search found nothing", not "accept anything"."""
    with pytest.raises(UnrecognizedPayloadError):
        _import("csv", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n", allow_empty=True)
    with pytest.raises(UnrecognizedPayloadError):
        _import("ris", "TY  - JOUR\nTI  - A\nER  - \n".encode("utf-16"), allow_empty=True)


# ---------------------------------------------------------------------------
# P1-4/5/6 — malformed bytes must reach the caller as a typed 4xx, never a 500
# and never somebody else's error string
# ---------------------------------------------------------------------------


def test_deeply_nested_json_is_a_typed_refusal_not_a_recursion_error():
    """A 40 KB body of nothing but brackets, which used to be a plain-text `500`.

    `RecursionError` derives from `RuntimeError`, so it slipped past every
    `ValueError` handler in the parser and out of the route — no problem+json
    body at all for the gateway to forward. Cheap, repeatable, needs no valid
    content and no privileged state.
    """
    payload = ('{"records":' + "[" * 20_000 + "]" * 20_000 + "}").encode()
    assert len(payload) < 41_000, "the point is that it is small"
    with pytest.raises(UnreadablePayloadError):
        parse_canonical_json(payload)
    with pytest.raises(UnreadablePayloadError):
        parse_discovery_session_export(('{"results":' + "[" * 80_000 + "]" * 80_000 + "}").encode())


def test_the_denylist_walk_no_longer_throws_at_any_depth():
    """The mechanism, and then the fix. **This test used to pin the defect.**

    It was written as `pytest.raises(RecursionError)` and it was right to be: the
    intuitive attribution is wrong, and the assertion is what proved it. On
    CPython 3.14 `json.loads` absorbs the 20,000-deep body without complaint —
    its C scanner has its own budget, measured at roughly 114,000 levels. What
    actually threw was `find_forbidden_keys`, the recursive denylist walk the
    import service runs over every parsed row, which gave out just past the
    1,000-frame interpreter limit. A fix aimed only at wrapping `json.loads`
    would have left the real thrower untouched and looked green.

    That attribution still stands and is still the point. What changed is that
    the thrower is no longer a thrower: `find_forbidden_keys` is iterative on an
    explicit stack, so the assertion is inverted rather than deleted, and the
    walk is exercised an order of magnitude past the limit that used to end it.

    Bounding this walk instead would have been the wrong instrument, which is why
    the fix is a rewrite and not a `MAX_DEPTH`. `imports.guard_denylist` calls it
    on the parsed row LIST — one or two wrappers below the payload root
    `MAX_JSON_DEPTH` measures — so a bound here equal to the parser's would start
    refusing records the parser had just accepted. `MAX_JSON_DEPTH` (below) and
    `api.body_guard.MAX_BODY_DEPTH` remain the depth authorities, each over the
    input whose destination it knows.

    The 500 this closes was not hypothetical for imports either: measured on
    `393ae61`, `POST /imports` with a JSON *envelope* nested 1,200 deep returned
    a bare `500 text/plain`, because `routers/imports.py:73` guards the raw
    envelope before any parser — and therefore before `_audit_json` — ever runs.
    """
    depth = 20_000
    data = json.loads("[" * depth + "]" * depth)
    assert find_forbidden_keys(data) == []

    # A guarded key at the bottom of a structure far past the old limit is still
    # FOUND, not merely survived — an iterative walk that quietly stopped
    # descending would also pass an "it does not raise" assertion.
    nested: dict = {"work_type": "regulatory_instrument"}
    for _ in range(5_000):
        nested = {"wrapper": nested}
    hits = find_forbidden_keys(nested)
    assert hits == ["$" + ".wrapper" * 5_000 + ".work_type"]


def test_json_nested_past_the_bound_but_decodable_is_refused_by_the_bound():
    """`MAX_JSON_DEPTH` is the load-bearing half — the `RecursionError` catch is the backstop.

    This payload decodes fine and is nowhere near any interpreter limit; it is
    refused because the bound says so, which is what protects `find_forbidden_keys`,
    RFC 8785 hashing and the JSONB write alike.
    """
    depth = import_parser.MAX_JSON_DEPTH + 5
    payload = ('{"records":' + "[" * depth + "]" * depth + "}").encode()
    with pytest.raises(UnreadablePayloadError, match="nests more than"):
        parse_canonical_json(payload)


def test_json_too_deep_for_the_decoder_itself_is_also_typed():
    """The backstop arm, past the point where the C scanner gives up (~114k levels).

    Without it this is a `RecursionError` raised inside `json.loads`, before
    `_audit_json` has any structure to measure.
    """
    depth = 130_000
    payload = ('{"records":' + "[" * depth + "]" * depth + "}").encode()
    with pytest.raises(UnreadablePayloadError, match="too deeply to parse"):
        parse_canonical_json(payload)


def test_a_record_nested_to_a_realistic_depth_still_imports():
    """The bound must not refuse anything an exporter emits."""
    payload = b'{"records": [{"title": "A study", "authors": [{"name": "Smith, J", "affiliation": {"a": "b"}}]}]}'
    assert _only(parse_canonical_json(payload)).title == "A study"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"records": [{"title": "a\\ud800b"}]}',
        b'{"records": [{"title": "t", "doi": "10.1\\udfff/x"}]}',
        b'{"records": [{"title": "t", "a\\ud800b": "v"}]}',
    ],
)
def test_a_lone_surrogate_is_a_typed_refusal_not_a_500(payload: bytes):
    """Valid JSON that `json.loads` accepts and nothing downstream can hold.

    RFC 8785 canonicalization refused it first — `UnsupportedTypeError: lone
    surrogate U+D800 is not valid Unicode and cannot be canonicalized` — with no
    handler anywhere, so it left as a plain-text `500`.
    """
    with pytest.raises(UnreadablePayloadError, match="surrogate"):
        parse_canonical_json(payload)


def test_a_nul_escape_in_json_is_a_typed_refusal():
    """`\\u0000` is valid JSON and the one code point PostgreSQL stores nowhere."""
    with pytest.raises(UnreadablePayloadError, match="NUL"):
        parse_canonical_json(b'{"records": [{"title": "a\\u0000b"}]}')


def test_a_utf16_csv_is_refused_by_encoding_rather_than_written_to_the_database():
    """P1-6. Excel's default "Unicode Text (*.txt)" is UTF-16.

    Decoded with `errors="replace"` the header row survived as recognisable
    words with NUL bytes between the letters, so the file passed every gate and
    died at the INSERT with PostgreSQL's `unsupported Unicode escape sequence` —
    served as `409 state_conflict`, a raw driver string in a status that means
    "your request conflicts with the review's state".
    """
    payload = "title,doi\nA study,10.1000/x\n".encode("utf-16")
    with pytest.raises(UnreadablePayloadError, match="UTF-16"):
        parse_csv(payload)


def test_a_nul_byte_in_a_csv_is_refused_as_a_payload_problem():
    """A NUL is valid UTF-8 and survives `errors="replace"` intact into the JSONB write."""
    with pytest.raises(UnreadablePayloadError, match="NUL"):
        parse_csv(b"title\nA\x00study\n")


def test_a_utf16_bibtex_export_is_named_as_utf16_rather_than_called_empty():
    with pytest.raises(UnreadablePayloadError, match="UTF-16"):
        parse_bibtex("@article{k, title = {A study}}".encode("utf-16"))


def test_a_latin1_csv_still_imports_with_replacement_characters():
    """The trade-off held deliberately: a lossy import is not a wrong one.

    Latin-1 is readable ASCII with a handful of damaged accents. Refusing it
    would cost a reviewer a corpus over three broken characters, which is not
    the same class of harm as a corpus of NUL-interleaved titles.
    """
    record = _only(parse_csv("title,journal\nCafé study,Journal\n".encode("latin-1")))
    assert record.title is not None and record.title.startswith("Caf")
    assert record.quarantine_reason is None


@pytest.mark.parametrize("source_kind", ["canonical_json", "discovery_session", "csv", "ris", "bibtex"])
def test_no_malformed_payload_reaches_the_caller_as_anything_but_a_typed_4xx(source_kind: str):
    """Every one of these was a `500` or a `409` carrying a PostgreSQL string."""
    hostile = [
        ('{"records":' + "[" * 20_000 + "]" * 20_000 + "}").encode(),
        b'{"records": [{"title": "a\\ud800b"}]}',
        b'{"results": [{"title": "a\\ud800b"}]}',
        "title,doi\nA study,10.1000/x\n".encode("utf-16"),
        b"title\nA\x00study\n",
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n",
    ]
    for payload in hostile:
        with pytest.raises(ProblemError) as raised:
            _import(source_kind, payload)
        assert 400 <= raised.value.status < 500, f"{source_kind} / {payload[:24]!r} -> {raised.value.status}"


# ---------------------------------------------------------------------------
# P0-3 — BibTeX brace protection
# ---------------------------------------------------------------------------

_BRACED = b"""@article{smith2019,
  title = {The {DNA} Helix and {RNA}},
  journal = {The {BMJ}},
  author = {Smith, J. and Doe, A.},
  year = {2019},
}
"""


def test_brace_protected_acronyms_survive_a_bibtex_title():
    """Google Scholar, Zotero and publisher exports all brace-protect acronyms."""
    record = _only(parse_bibtex(_BRACED))
    assert record.title == "The DNA Helix and RNA"


def test_brace_protection_is_stripped_from_the_container_title():
    assert _only(parse_bibtex(_BRACED)).container_title == "The BMJ"


def test_a_doubly_braced_title_is_not_left_with_a_stray_brace():
    """`{{Nature}} at the end of a title is where the old `strip('{} ')` broke."""
    record = _only(parse_bibtex(b"@article{k,\n  title = {A review in {{Nature}}},\n}\n"))
    assert record.title == "A review in Nature"


def test_an_unbalanced_closing_brace_is_kept_rather_than_silently_eaten():
    """Malformed input stays visible; the parser does not invent a repair.

    Asserted against the helper rather than through `parse_bibtex`, because
    bibtexparser 1.4.4 terminates a `{...}` field at the balance point and never
    hands an unbalanced string down — so the property is unreachable from the
    outside and would otherwise go untested until some future parser change made
    it reachable.
    """
    assert import_parser._strip_brace_protection("A study of C} elegans") == "A study of C} elegans"


# ---------------------------------------------------------------------------
# P1 — malformed input must never reach the caller as a 500
# ---------------------------------------------------------------------------


def test_non_utf8_json_is_a_typed_refusal_not_a_unicode_error():
    with pytest.raises(UnreadablePayloadError):
        parse_canonical_json('{"records": [{"title": "café"}]}'.encode("latin-1"))


def test_non_utf8_discovery_export_is_a_typed_refusal():
    with pytest.raises(UnreadablePayloadError):
        parse_discovery_session_export('{"results": [{"title": "café"}]}'.encode("latin-1"))


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (b'{"records": [{"title": "t", "abstract": 42}]}', "abstract"),
        (b'{"records": [{"title": "t", "authors": 42}]}', "authors"),
        (b'{"records": [{"title": "t", "container_title": {"name": "Lancet"}}]}', "container_title"),
        (b'{"records": [{"title": "t", "doi": {"value": "10.1000/x"}}]}', "doi"),
        (b'{"records": [{"title": "t", "url": {"href": "http://x"}}]}', "url"),
        (b'{"records": [{"title": "t", "pmcid": {"id": "PMC1"}}]}', "pmcid"),
        (b'{"records": [{"title": {"text": "t"}}]}', "title"),
    ],
)
def test_a_json_field_of_the_wrong_shape_quarantines_the_row(body: bytes, field: str):
    """A dict where a string belongs used to reach a `String` column and 500 at flush."""
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is not None
    assert field in record.quarantine_reason
    assert record.raw, "a quarantined row still carries its original representation"


def test_an_object_pmid_never_becomes_a_dedup_key():
    """`str({"a": 1})` used to yield `"{'a': 1}"`, which normalizes to the PMID `1`."""
    record = _only(parse_canonical_json(b'{"records": [{"title": "t", "pmid": {"a": 1}}]}'))
    assert record.quarantine_reason is not None
    assert record.pmid is None
    assert record.normalized()["normalized_pmid"] is None


def test_a_numeric_json_year_or_pmid_is_still_accepted():
    """Coercion is fine for scalars — only containers are refused."""
    record = _only(parse_canonical_json(b'{"records": [{"title": "t", "pmid": 33933206, "year": 2021}]}'))
    assert record.quarantine_reason is None
    assert record.pmid == "33933206"
    assert record.publication_year == 2021


def test_a_list_where_a_year_belongs_is_quarantined_not_unwrapped():
    """P1-8. `normalize_year` searches `str(value)`, so `[1999]` yielded `1999`.

    A list silently coerced to the scalar it happened to contain, which is what
    `_require_text` refuses everywhere else. `[1999, 2001]` would have produced
    `1999` with exactly the same confidence, and year is not cosmetic:
    `normalized_title + publication_year` is the third dedup rule.
    """
    record = _only(parse_canonical_json(b'{"records": [{"title": "t", "publication_year": [1999]}]}'))
    assert record.quarantine_reason is not None
    assert "publication_year" in record.quarantine_reason
    assert record.publication_year is None
    assert record.raw["publication_year"] == [1999], "the quarantined row keeps what was sent"


def test_a_list_where_a_record_type_belongs_is_quarantined_not_downgraded():
    """The same discipline. Silently becoming `other` hides a producer bug."""
    record = _only(parse_canonical_json(b'{"records": [{"title": "t", "record_type": ["report"]}]}'))
    assert record.quarantine_reason is not None
    assert "record_type" in record.quarantine_reason


# ---------------------------------------------------------------------------
# The identifier seam — `discovery-engine` NESTS what this parser read flat
# ---------------------------------------------------------------------------
#
# `discovery-engine`'s `ExportService._paper_to_dict` emits every identifier
# inside an `external_ids` object and the link as `pdf_url`. This parser read
# `doi`, `pmid`, `pmcid`, `arxiv_id` and `url` at the top level only, so a
# discovery-engine corpus imported with EVERY identifier null. Nothing failed:
# the rows were accepted, the counts conserved, and `duplicate_resolutions`
# stayed empty because two of the three dedup rules had no key to run on. The
# PRISMA block then described a deduplication that never had evidence to run.

_DISCOVERY_EXPORT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "discovery_engine_session_export.json"

#: The inner document of a real `GET /api/sessions/{id}/export?format=json`
#: body — i.e. `json.loads(SessionExport.data)`, which is the `canonical_json`
#: a user copies out of the studio.
_DISCOVERY_PAPERS: list[dict] = json.loads(_DISCOVERY_EXPORT_PATH.read_text("utf-8"))["papers"]


def _discovery_records() -> list[ParsedRecord]:
    return parse_canonical_json(json.dumps(_DISCOVERY_PAPERS).encode("utf-8"))


def _titled(records: list[ParsedRecord], needle: str) -> ParsedRecord:
    """One record, by exact title — or by prefix when that is unambiguous.

    Two of the fixture's papers are the same work under titles that differ only
    by a trailing period or a subtitle, which is the whole point of them, so an
    exact match has to win over a prefix.
    """
    exact = [r for r in records if r.title == needle]
    matches = exact or [r for r in records if (r.title or "").startswith(needle)]
    assert len(matches) == 1, f"expected one record titled {needle!r}, got {len(matches)}"
    return matches[0]


#: The two works the fixture carries twice, each from a different source.
_TOCILIZUMAB = (
    "Tocilizumab in patients admitted to hospital with COVID-19 (RECOVERY): "
    "a randomised, controlled, open-label, platform trial"
)
_DEXAMETHASONE = "Dexamethasone in Hospitalized Patients with Covid-19"


def test_a_discovery_engine_export_keeps_its_identifiers():
    """The seam itself, over one payload per adapter that writes `external_ids`.

    Read flat-only, every one of these came out `None` — including the DOI,
    which is the highest-confidence merge rule the engine has.
    """
    records = _discovery_records()
    assert [r.quarantine_reason for r in records] == [None] * len(_DISCOVERY_PAPERS)

    # semantic_scholar: `s2_id` / `doi` / `pubmed_id`, and `pdf_url` for the link.
    s2 = _titled(records, _TOCILIZUMAB)
    assert s2.doi == "10.1016/S0140-6736(21)00676-0"
    assert s2.pmid == "33933206", "semantic_scholar spells the PMID `pubmed_id`"
    assert s2.url == "https://www.thelancet.com/article/S0140-6736(21)00676-0/pdf"

    # pubmed: `pmid` / `doi`, and no link at all.
    pubmed = _titled(records, _TOCILIZUMAB + ".")
    assert (pubmed.pmid, pubmed.doi) == ("33933206", "10.1016/S0140-6736(21)00676-0")
    assert pubmed.url is None

    # arxiv: `arxiv_id` / `doi`.
    arxiv = _titled(records, "Attention Is All You Need")
    assert arxiv.arxiv_id == "1706.03762"
    assert arxiv.doi == "10.48550/arXiv.1706.03762"

    # europe_pmc: `pmid` / `doi` / `europe_pmc_id`.
    epmc = _titled(records, "Remdesivir")
    assert (epmc.pmid, epmc.doi) == ("32445440", "10.1056/NEJMoa2007764")

    # consensus (OpenAlex): `openalex_id` / `doi` / `pubmed_id`.
    openalex = _titled(records, "Colchicine")
    assert (openalex.pmid, openalex.doi) == ("34672950", "10.1016/S2213-2600(21)00435-5")

    # A paper the adapter had no identifiers for is still a record, not a quarantine.
    bare = _titled(records, "Report of the WHO-China")
    assert (bare.doi, bare.pmid, bare.arxiv_id, bare.url) == (None, None, None, None)

    normalized = [r.normalized()["normalized_doi"] for r in records]
    assert normalized.count(None) == 1, "exactly one fixture paper has no DOI; the rest must all normalize"


def test_the_same_paper_from_two_discovery_sources_is_proposed_as_a_duplicate():
    """The corpus-level consequence, through the real engine.

    One work, returned by two of the session's sources. The titles differ (one
    carries the `- Preliminary Report` subtitle), the years differ (2020 vs 2021)
    and only one of them carries a PMID — so `normalized_title_year` cannot
    merge them and neither can `normalized_pmid`. The shared DOI is the only
    thing that can, which is exactly the rule the flat-only read silenced.
    """
    records = _discovery_records()
    crossref = _titled(records, _DEXAMETHASONE)
    semantic = _titled(records, _DEXAMETHASONE + " - Preliminary Report")

    pairs = find_pairs(_candidates([crossref, semantic]))
    assert [p.matched_on for p in pairs] == ["normalized_doi"], (
        "one work is in the corpus twice and the engine proposed nothing: with the DOI unread, "
        "`duplicates_removed` is 0 and the PRISMA block is internally consistent about it"
    )
    assert _clusters([crossref, semantic]) == 1

    # And the supporting facts — neither remaining rule could have found it.
    assert crossref.title != semantic.title
    assert crossref.publication_year != semantic.publication_year
    assert (crossref.pmid, semantic.pmid) == (None, "32678530")


def test_a_discovery_session_export_body_keeps_its_identifiers_too():
    """The other entry point onto the same records. Both must read the nesting."""
    body = json.dumps({"session_id": "s1", "results": _DISCOVERY_PAPERS}).encode("utf-8")
    records, _ = parse_discovery_session_export(body)
    assert _titled(records, "Attention Is All You Need").arxiv_id == "1706.03762"
    assert _titled(records, "Remdesivir").pmid == "32445440"


def test_a_flat_json_record_is_unchanged_by_the_nested_fallback():
    """The negative control. Every existing exporter writes these keys flat."""
    body = (
        b'{"records": [{"title": "t", "doi": "10.1000/182", "pmid": "33933206", "pmcid": "PMC8084355", '
        b'"arxiv_id": "1706.03762", "url": "https://example.org/a"}]}'
    )
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert (record.doi, record.pmid, record.pmcid, record.arxiv_id, record.url) == (
        "10.1000/182",
        "33933206",
        "PMC8084355",
        "1706.03762",
        "https://example.org/a",
    )


def test_a_flat_identifier_outranks_the_nested_one():
    """Ordering is a decision: the flat key is the record's own claim.

    `external_ids` is a fallback for producers that only nest. Where both are
    present the flat key wins, so no existing export changes meaning.
    """
    body = (
        b'{"records": [{"title": "t", "doi": "10.1000/flat", "url": "https://example.org/flat", '
        b'"external_ids": {"doi": "10.1000/nested"}, "pdf_url": "https://example.org/nested"}]}'
    )
    record = _only(parse_canonical_json(body))
    assert record.doi == "10.1000/flat"
    assert record.url == "https://example.org/flat"


def test_a_nested_identifier_still_goes_through_the_normalizers():
    """The fallback must not become a second, laxer path into the same columns."""
    body = b'{"records": [{"title": "t", "external_ids": {"pmcid": "8084355", "pmid": 33933206}}]}'
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert record.pmcid == "PMC8084355", "normalize_pmcid must run on the nested value too"
    assert record.pmid == "33933206", "an integer PMID is a faithful encoding, nested or not"


@pytest.mark.parametrize(
    "body",
    [
        b'{"records": [{"title": "t", "doi": "10.1000/182"}]}',
        b'{"records": [{"title": "t", "doi": "10.1000/182", "external_ids": null}]}',
        b'{"records": [{"title": "t", "doi": "10.1000/182", "external_ids": {}}]}',
    ],
    ids=["absent", "null", "empty"],
)
def test_an_absent_or_empty_external_ids_is_not_an_error(body: bytes):
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert record.doi == "10.1000/182"


def test_an_external_ids_that_is_not_an_object_does_not_discard_the_record():
    """`[]` is how PHP, Perl and Ruby serialise an empty map, and it is not fatal.

    Every field this record needs is flat and readable. `external_ids` is a key
    the parser IGNORED before it learned to read nested identifiers, so a record
    that imported on the commit before that must still import: opting in to
    reading a key must never turn it into a way to lose the row. The malformed
    source is annotated instead — and `raw` keeps it verbatim either way.
    """
    body = json.dumps(
        {
            "records": [
                {
                    "title": "Tocilizumab in patients admitted to hospital with COVID-19",
                    "doi": "10.1016/S0140-6736(21)00676-0",
                    "pmid": "33933206",
                    "external_ids": [],
                }
            ]
        }
    ).encode("utf-8")
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert (record.doi, record.pmid) == ("10.1016/S0140-6736(21)00676-0", "33933206")
    assert record.unusable_sources == ("external_ids",)
    assert record.raw["external_ids"] == []


# ---------------------------------------------------------------------------
# Flat-payload parity — a payload with no `external_ids` must not have moved
# ---------------------------------------------------------------------------
#
# MEASURED against the commit before the nested read (`97c441d`) over an
# exhaustive grid of flat payloads. Each case below is one that diverged when
# strictness was applied to the fallback CHAIN rather than to the VALUE: a
# falsy-but-wrong-typed value used to fall through an `or` harmlessly and
# started being handed to the type check instead, so 127 payloads that imported
# were suddenly quarantined. The fix is that the primary expression is the one
# this parser has always evaluated, and only what is left over is a fallback.


@pytest.mark.parametrize(
    ("body", "expected", "unusable"),
    [
        # `url: []` fell through `[] or item.get("link")` to None and imported.
        (b'{"records": [{"title": "t", "doi": "10.1000/182", "url": []}]}', {"doi": "10.1000/182", "url": None}, ()),
        # `link` still catches it, exactly as it did.
        (
            b'{"records": [{"title": "t", "url": [], "link": "https://example.org/a"}]}',
            {"url": "https://example.org/a"},
            (),
        ),
        # `pdf_url` is a key the base commit never read AT ALL, so no value in it
        # may refuse a record: it is a fallback, so a malformed one is skipped
        # and named rather than discarding a row that used to import.
        (
            b'{"records": [{"title": "t", "doi": "10.1000/182", "pdf_url": []}]}',
            {"doi": "10.1000/182", "url": None},
            ("pdf_url",),
        ),
        (b'{"records": [{"title": "t", "pdf_url": {"href": "x"}}]}', {"url": None}, ("pdf_url",)),
        # `arxiv_id: 0` is falsy: the base expression moves on to `arxiv`, and
        # what it finds there is what gets validated. Stopping on the `0` both
        # quarantined the first of these and silently changed the second.
        (b'{"records": [{"title": "t", "arxiv_id": 0, "arxiv": "1706.03762"}]}', {"arxiv_id": "1706.03762"}, ()),
        (b'{"records": [{"title": "t", "arxiv_id": 0}]}', {"arxiv_id": None}, ()),
        (b'{"records": [{"title": "t", "pmid": 0}]}', {"pmid": "0"}, ()),
    ],
)
def test_a_flat_payload_parses_exactly_as_it_did_before_the_nested_read(body: bytes, expected: dict, unusable: tuple):
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None, "this payload imported on 97c441d and must still import"
    for name, value in expected.items():
        assert getattr(record, name) == value, name
    assert record.unusable_sources == unusable


def test_a_flat_payload_that_quarantined_before_still_quarantines():
    """The other direction of parity. `[]` reached the type check and refused."""
    record = _only(parse_canonical_json(b'{"records": [{"title": "t", "arxiv_id": 0, "arxiv": []}]}'))
    assert record.quarantine_reason is not None
    assert "arxiv_id" in record.quarantine_reason


def test_a_wrong_shaped_flat_identifier_is_not_skipped_in_favour_of_the_nested_one():
    """The fallback chain must not become a way to look past a producer bug.

    A truthiness test for "is this key present" would step over `"doi": 0` and
    quietly take the nested one, and the record would import clean while the
    export that produced a numeric DOI stayed broken.
    """
    body = b'{"records": [{"title": "t", "doi": 0, "external_ids": {"doi": "10.1000/182"}}]}'
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is not None
    assert "doi" in record.quarantine_reason


def test_an_empty_flat_identifier_does_fall_through_to_the_nested_one():
    """`""` is the producer saying it has nothing there, not a value to prefer."""
    body = b'{"records": [{"title": "t", "doi": "", "external_ids": {"doi": "10.1000/182"}}]}'
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert record.doi == "10.1000/182"


def test_a_nested_identifier_of_the_wrong_shape_is_skipped_and_named_not_fatal():
    """Same rule one level down: a member we opted in to reading is best-effort.

    The record imported before `external_ids` was read at all, so a malformed
    member cannot discard it. It is skipped and named — `external_ids.doi`, not
    `doi`, because they are different producer bugs to go and fix.
    """
    body = b'{"records": [{"title": "t", "external_ids": {"doi": {"value": "10.1/x"}, "pubmed_id": "33933206"}}]}'
    record = _only(parse_canonical_json(body))
    assert record.quarantine_reason is None
    assert record.doi is None
    assert record.unusable_sources == ("external_ids.doi",)
    # And a usable sibling is still read: one bad key does not abandon the rest.
    assert record.pmid == "33933206"


def test_a_nested_source_that_was_unusable_is_visible_in_the_audit_trail():
    """ "No DOI" and "a DOI we could not read" must not look the same afterwards."""
    body = b'{"records": [{"title": "t", "external_ids": {"doi": {"value": "10.1/x"}}}]}'
    version = _imported_version(body)
    assert version.field_provenance["unusable_sources"] == ["external_ids.doi"]


def test_a_clean_record_carries_no_unusable_sources_key():
    """Scoping: the annotation appears only when something was actually skipped."""
    version = _imported_version(b'{"records": [{"title": "t", "doi": "10.1000/182"}]}')
    assert "unusable_sources" not in version.field_provenance


# ---------------------------------------------------------------------------
# `field_provenance` — the column that says WHERE a dedup key came from
# ---------------------------------------------------------------------------


def _imported_version(payload: bytes, source_kind: str = "canonical_json"):
    """One `ReviewRecordVersion` as the import service would write it."""
    session = _NoExistingBatch()
    import_service.import_payload(
        session,  # type: ignore[arg-type]
        Actor(tenant_id=tenant("unit"), subject_id=_LEAD_SUBJECT, role=ActorRole.LEAD),
        uuid.uuid4(),
        source_kind=source_kind,
        payload=payload,
    )
    versions = [obj for obj in session.added if isinstance(obj, ReviewRecordVersion)]
    assert len(versions) == 1, f"expected one record version, got {len(versions)}"
    return versions[0]


def test_field_provenance_names_the_key_that_actually_produced_the_identifier():
    """`ER-002.4`: "so a surprising dedup match is traceable to the field that
    produced it". A hardcoded `"pmid"` is a FALSE statement in an append-only
    audit column the moment a payload spells it `external_ids.pubmed_id` — which
    is how discovery-engine's semantic_scholar and consensus adapters spell it,
    i.e. the shape of the corpus this parser was just taught to read.
    """
    body = json.dumps(
        {"records": [{"title": "t", "external_ids": {"doi": "10.1000/182", "pubmed_id": "33933206"}}]}
    ).encode("utf-8")
    version = _imported_version(body)
    assert version.normalized_pmid == "33933206"
    assert version.field_provenance["normalized_pmid"] == "external_ids.pubmed_id"
    assert version.field_provenance["normalized_doi"] == "external_ids.doi"


def test_field_provenance_still_names_the_flat_field_when_that_is_where_it_came_from():
    """The negative control, and what every RIS/BibTeX/CSV row keeps reporting."""
    body = b'{"records": [{"title": "t", "doi": "10.1000/182", "pmid": "33933206"}]}'
    version = _imported_version(body)
    assert version.field_provenance["normalized_doi"] == "doi"
    assert version.field_provenance["normalized_pmid"] == "pmid"
    assert _imported_version(b"TY  - JOUR\nTI  - A\nDO  - 10.1000/182\nER  - \n", "ris").field_provenance == {
        "normalized_title": "title",
        "normalized_doi": "doi",
        "normalized_pmid": "pmid",
        "source_row": ANY,
    }


def test_a_csv_field_larger_than_the_stdlib_limit_is_read_and_truncated():
    """A 140k-character abstract is an ordinary Embase export, not an attack."""
    payload = b"title,abstract\nA study," + b"a" * 140_000 + b"\n"
    record = _only(parse_csv(payload))
    assert record.quarantine_reason is None
    assert record.abstract is not None
    assert len(record.abstract) == import_parser.MAX_FIELD_CHARS


def test_the_global_csv_field_limit_is_restored_after_a_parse():
    """`csv.field_size_limit` is process-global; leaking it changes other callers."""
    before = csv.field_size_limit()
    parse_csv(b"title\nA study\n")
    assert csv.field_size_limit() == before


def test_the_csv_field_limit_is_restored_when_the_parse_fails(monkeypatch: pytest.MonkeyPatch):
    """The `finally`, not the happy path — an abandoned parse must not leak the limit."""
    before = csv.field_size_limit()
    monkeypatch.setattr(import_parser, "MAX_ROWS", 1)
    with pytest.raises(ImportTooLargeError):
        parse_csv(b"title\nA study\nAnother study\n")
    assert csv.field_size_limit() == before


# ---------------------------------------------------------------------------
# MAX_ROWS — the same answer from every parser
# ---------------------------------------------------------------------------


def _n_records(kind: str, n: int) -> bytes:
    if kind == "ris":
        return b"".join(b"TY  - JOUR\nTI  - Study %d\nER  - \n" % i for i in range(n))
    if kind == "bibtex":
        return b"".join(b"@article{k%d,\n  title = {Study %d},\n}\n" % (i, i) for i in range(n))
    if kind == "csv":
        return b"title\n" + b"".join(b"Study %d\n" % i for i in range(n))
    if kind == "canonical_json":
        return b'{"records": [' + b", ".join(b'{"title": "Study %d"}' % i for i in range(n)) + b"]}"
    return b'{"results": [' + b", ".join(b'{"title": "Study %d"}' % i for i in range(n)) + b"]}"


_PARSERS = {
    "ris": parse_ris,
    "bibtex": parse_bibtex,
    "csv": parse_csv,
    "canonical_json": parse_canonical_json,
    "discovery": parse_discovery_session_export,
}


@pytest.mark.parametrize("kind", sorted(_PARSERS))
def test_every_parser_refuses_a_payload_over_max_rows(kind: str, monkeypatch: pytest.MonkeyPatch):
    """A silent `[:MAX_ROWS]` truncation gives a PRISMA count that is wrong and consistent.

    Wrong-and-consistent is the worst shape a count can have: nothing downstream
    disagrees with it, so nothing downstream can notice.
    """
    monkeypatch.setattr(import_parser, "MAX_ROWS", 3)
    parser = _PARSERS[kind]
    assert len(_as_records(parser(_n_records(kind, 3)))) == 3
    with pytest.raises(ImportTooLargeError):
        parser(_n_records(kind, 4))


def _as_records(result) -> list[ParsedRecord]:
    return result[0] if isinstance(result, tuple) else result


# --------------------------------------------------------------------------
# AHTA-DISC-003 / 004 — the blocker that no HTTP request could reach
# --------------------------------------------------------------------------


def test_every_blocked_source_kind_resolves_to_a_named_blocker() -> None:
    """`BLOCKED_SOURCE_KINDS` and `unsupported_format_blocker` must agree.

    The router refuses these before the generic `SOURCE_KINDS` check so a
    recognised-but-blocked format gets its own diagnosis. A kind listed there
    that falls through to the generic branch would produce a 422 with
    `blocker: None` — the same shape as a typo, which is the defect this closes,
    reintroduced one string at a time.
    """
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from api.routers.imports import BLOCKED_SOURCE_KINDS, SOURCE_KINDS
    from infrastructure.imports.import_parser import unsupported_format_blocker

    assert BLOCKED_SOURCE_KINDS, "the blocked-kind list is empty; the blocker is unreachable again"
    for kind in BLOCKED_SOURCE_KINDS:
        assert kind not in SOURCE_KINDS, f"{kind!r} is both supported and blocked"
        blocker = unsupported_format_blocker(kind)
        assert getattr(blocker, "blocker", None) in {"AHTA-DISC-003", "AHTA-DISC-004"}, (
            f"{kind!r} is refused before the generic check but carries no AHTA-DISC blocker, so the "
            f"caller gets a bare 422 with no diagnosis: {blocker!r}"
        )
        assert str(blocker), f"{kind!r} produced an empty blocker message"


def test_an_unrecognised_kind_still_gets_the_generic_refusal() -> None:
    """Scoping. `unsupported_format_blocker` must NOT claim a blocker it has none for."""
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from infrastructure.imports.import_parser import unsupported_format_blocker

    generic = unsupported_format_blocker("banana")
    assert getattr(generic, "blocker", None) is None, (
        "an unrecognised format was given an AHTA-DISC blocker, which asserts a documented decision "
        "that was never taken about it"
    )
