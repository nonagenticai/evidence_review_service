r"""Bounded bibliographic import adapters — `ER-002.2`.

RIS, BibTeX, CSV, canonical JSON, and a `discovery-engine` session export. XLSX
is deliberately absent: `ER-002.2` asks for it, but its only named consumers are
the Catchii/Cachii workbooks, which are `AHTA-DISC-003/004`-blocked, so it would
add an office-file parsing surface (`01-…:181` names "parser/office-file escape"
as a threat) for zero first-run evidence. `01-…:427` requires those workbooks to
report their blocker rather than a guessed mapping, which is what
:func:`unsupported_format_blocker` does.

Every adapter obeys the same two rules, both from `ER-002.4` / `01-…:421`:

* **A malformed row never discards a valid one.** Parsing is per-record, and a
  failure produces a quarantined row carrying its original bytes and a reason —
  not an aborted import. An import that fails wholesale on one bad entry makes
  the caller edit a 3000-record file by hand.
* **The original representation is always retained.** The normalized record is a
  derivative; the raw row is the evidence. Quarantined rows keep theirs too, so
  the quarantine is reviewable rather than a count.

Bounds are enforced before parsing, not during: a 200 MB upload should be refused
by size, not discovered by running out of memory partway through.

**Known and deliberately not addressed here**, so that the next reader does not
mistake silence for coverage. None of these can merge two different papers or
report an empty corpus as a successful import, which is why they were left:

* `_CSV_ALIASES` covers a narrow set of headers. Covidence (`Study`), Zotero
  (`Publication Title`, `Item Type`) and Scopus (`Document Title`, `Source
  title` is present but `Cited by` etc. are not) all emit columns that fall
  through into `raw` unmapped. Unmapped is safe — the row quarantines with its
  bytes intact if no title column is found — but it is a real import gap.
* LaTeX escapes in BibTeX (`\'{e}`, `\&`, `$x_{i}$`) are stored verbatim. See
  :func:`_strip_brace_protection` for why a half-decoder is worse than none.
* PMCID and arXiv identifiers are read from canonical JSON and CSV but not from
  RIS or BibTeX, where they do appear (`C7`, `eprint`).
* A `pubmed.ncbi.nlm.nih.gov/<pmid>/` URL in a RIS `UR` tag is an unambiguous
  PMID and is not read. It would be a safe addition; it is simply not in scope.
* :func:`parse_discovery_session_export` accepts `results`, `records` or
  `items`, and `discovery-engine`'s `GET /api/sessions/{id}/export?format=json`
  emits neither: its response body is `SessionExport` — `{session_id, format,
  data, paper_count}` — whose `data` is a JSON *string* containing
  `{"session_id": …, "papers": [...]}`. So that response cannot be handed to
  this parser as-is; the working path is the inner `papers` list, pasted as
  `canonical_json`. Left alone deliberately: the mismatch is a loud
  `UnsupportedFormatError` naming the keys it looked for, not a silent empty
  import, and which side should move is a cross-repo decision.
* Only UTF-8 is decoded, and only UTF-8 is accepted. A UTF-16 or UTF-32 export
  is refused by :func:`_decode_text` rather than decoded with replacement
  characters — see that function for why "imports with `�` in every field"
  was the wrong answer. Latin-1 still decodes with replacement, because a
  Latin-1 file is mostly readable ASCII with a handful of damaged accented
  characters, which is a lossy import rather than a wrong one.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from domain.policies.normalization import (
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    normalize_text,
    normalize_year,
)

# `RECORD_TYPES` is imported, not restated. `models.records` calls itself "the
# one place its values are written down", and the database enforces the same
# five through `ck_record_version_record_type` (migration `0003`). A local copy
# here would be a third declaration that drifts silently: a value this parser
# permitted and Postgres refused would surface as an IntegrityError on an import
# that looked fine.
from models.records import RECORD_TYPES

#: `01-…` "resource limits" / `ER-002` freeze gate. Deliberately conservative and
#: named, so raising one is a decision rather than an accident.
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_ROWS = 50_000
MAX_FIELD_CHARS = 100_000

#: How much of a rejected `record_type` is kept in `field_provenance`.
#:
#: Much tighter than `MAX_FIELD_CHARS` on purpose. That bound sizes free text —
#: a title, an abstract — where a reader loses something real to truncation. A
#: record TYPE is drawn from closed vocabularies whose longest genuine member
#: (`proceedings-article`, `systematic_review`) is under thirty characters, so
#: anything past this is not a type any producer meant and keeping it buys the
#: reader nothing.
#:
#: It matters because the annotation lands in `field_provenance`, JSONB on an
#: APPEND-ONLY table: unbounded here means any caller can write arbitrarily
#: large producer-controlled text into permanent audit storage, once per record,
#: through a route that answers 201 and can never be cleaned up.
MAX_RECORD_TYPE_ANNOTATION_CHARS = 64

#: How deeply a JSON payload may nest. A bibliographic record is a flat object
#: with, at worst, a list of author objects inside it — four levels.
#:
#: MEASURED on this interpreter (CPython 3.14, `sys.getrecursionlimit() == 1000`),
#: because the intuitive story is wrong: `json.loads` does NOT fall over first.
#: Its C scanner has its own much larger budget and survives to a nesting depth
#: of about 114,000. The pure-Python recursive walkers that run on the *result*
#: give out at about 1,046 — `domain.policies.denylist.find_forbidden_keys`, which
#: the import service runs over every parsed row, and RFC 8785 canonical hashing.
#: So the `RecursionError` that a 40 KB body of nothing but `[` produced came out
#: of the DENYLIST GUARD, not the decoder. Sixty-four is two orders of magnitude
#: clear of that floor and far past anything an exporter emits.
MAX_JSON_DEPTH = 64

#: RIS type codes -> our `record_type`. Anything unmapped becomes `other`
#: rather than being guessed into `journal_article`: a wrong type is a claim,
#: and `other` is an honest one.
_RIS_TYPE_MAP = {
    "JOUR": "journal_article",
    "EJOUR": "journal_article",
    "ABST": "journal_article",
    "INPR": "preprint",
    "UNPB": "preprint",
    "RPRT": "report",
    "GOVDOC": "report",
    "STAND": "report",
    "DATA": "dataset",
}

_BIBTEX_TYPE_MAP = {
    "article": "journal_article",
    "misc": "other",
    "unpublished": "preprint",
    "techreport": "report",
    "manual": "report",
    "dataset": "dataset",
    "inproceedings": "journal_article",
    "conference": "journal_article",
}


class ImportTooLargeError(ValueError):
    """The payload exceeded a declared bound. Refused before parsing."""


class UnreadablePayloadError(ValueError):
    """These bytes are not the format the caller declared.

    Distinct from :class:`UnsupportedFormatError`, which is "we deliberately do
    not parse this format at all" and carries a blocker id. This one is "you
    said RIS and sent a PDF", "you said CSV and sent UTF-16", "you sent a lone
    surrogate that Postgres cannot store". The caller's remedy is always the
    same shape: re-export the file, or correct `source_kind`. It reaches the
    caller as `422 unrecognized_payload`.
    """


class NoRecordsParsedError(UnreadablePayloadError):
    """Bytes went in and no record came out.

    The specific case that used to succeed: a payload the declared parser cannot
    read produced an empty list, which the import service wrote as a batch with
    ``accepted=0, quarantined=0`` and returned `201`. A UTF-16 EndNote export, a
    PDF uploaded as RIS and a BibTeX file declared as CSV were all reported as
    successful imports of nothing.

    NOT raised for a source that is legitimately empty. `{"records": []}`, a CSV
    header row with no data rows and `{"results": []}` all parse cleanly to `[]`
    and return it; refusing THOSE is the import service's job, because only the
    service knows whether the caller declared `allow_empty`.
    """


class UnsupportedFormatError(ValueError):
    """A format we deliberately do not guess at."""

    def __init__(self, message: str, blocker: str | None = None) -> None:
        super().__init__(message)
        self.blocker = blocker


@dataclass
class ParsedRecord:
    """One candidate record plus the raw row it came from."""

    raw: dict[str, Any]
    record_type: str = "other"
    title: str | None = None
    abstract: str | None = None
    publication_year: int | None = None
    authors: list[str] = field(default_factory=list)
    container_title: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    #: `field -> the source key it was actually read from`, e.g.
    #: ``{"pmid": "external_ids.pubmed_id"}``. `ER-002.4` requires
    #: `field_provenance` to name "the field that produced" a normalized value so
    #: a surprising dedup match is traceable; with more than one spelling per
    #: identifier, only the parser knows which one won. A hardcoded guess one
    #: layer up is a false statement in an append-only audit column.
    #: Absent entries mean "the field of the same name" — the RIS, BibTeX and CSV
    #: adapters read one spelling each and do not populate this.
    field_sources: dict[str, str] = field(default_factory=dict)
    #: Source keys that carried something unusable and were skipped rather than
    #: refused — a malformed `external_ids` or one of its members. Annotated, not
    #: discarded: see :func:`_identified`. Empty for almost every row.
    unusable_sources: tuple[str, ...] = ()
    #: Set when the row could not be understood. A quarantined row still carries
    #: its `raw`, which is the point.
    quarantine_reason: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "normalized_title": normalize_text(self.title),
            "normalized_doi": normalize_doi(self.doi),
            "normalized_pmid": normalize_pmid(self.pmid),
        }


def payload_digest(payload: bytes) -> str:
    """`sha256:<hex>` of the exact submitted bytes."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _check_bounds(payload: bytes) -> None:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ImportTooLargeError(f"payload is {len(payload)} bytes; the limit is {MAX_PAYLOAD_BYTES}")


def _truncate(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_FIELD_CHARS]


#: Longest first: `BOM_UTF32_LE` starts with the two bytes of `BOM_UTF16_LE`, so
#: testing UTF-16 first would report a UTF-32 file as UTF-16.
_TEXT_BOMS = (
    (codecs.BOM_UTF32_LE, "UTF-32 (little-endian)"),
    (codecs.BOM_UTF32_BE, "UTF-32 (big-endian)"),
    (codecs.BOM_UTF16_LE, "UTF-16 (little-endian)"),
    (codecs.BOM_UTF16_BE, "UTF-16 (big-endian)"),
)


def _decode_text(payload: bytes, what: str) -> str:
    """Bytes -> text for the line-oriented formats, refusing what cannot be stored.

    ``errors="replace"`` is kept for the ordinary mis-encoding — a Latin-1 file
    is readable ASCII with a few damaged accents, and refusing it would cost a
    reviewer a corpus over three broken characters. Two inputs are refused
    outright instead, because "replace" turns both of them into damage that
    nothing downstream can detect:

    * **UTF-16/UTF-32.** Excel's "Unicode Text (*.txt)" and EndNote on Windows
      both emit UTF-16. Decoded as UTF-8 with replacement, a UTF-16-BE file
      becomes `\\x00T\\x00i...` -> `NUL T NUL i` and a UTF-16-LE file becomes
      `T NUL i NUL` — the header row often survives as recognisable words with
      NULs between the letters, so the file passed the "is this a CSV" gate and
      imported a corpus of `T\\x00i\\x00t\\x00l\\x00e` titles.
    * **An embedded NUL.** `U+0000` is valid UTF-8 and valid Python text, and it
      is the one code point Postgres accepts in neither `text` nor `jsonb`. It
      survived every check in this module and died at the INSERT as
      ``unsupported Unicode escape sequence``, which the DBAPI handler reported
      as `409 state_conflict` — a Postgres string, in a status meaning "your
      request conflicts with the review's state", for what is entirely a
      property of the uploaded bytes.

    Whole-payload refusal rather than per-row quarantine, deliberately, against
    this module's "a malformed row never discards a valid one" rule. Both
    conditions are properties of the *file*, not of a row: a UTF-16 file has no
    good rows, and a quarantined row must persist its `raw` — which is exactly
    the write that a NUL makes impossible. Quarantining would mean storing a
    `raw` we had silently repaired, which breaks the stronger rule that the
    original representation is always retained.
    """
    for bom, name in _TEXT_BOMS:
        if payload.startswith(bom):
            raise UnreadablePayloadError(
                f"{what} is encoded as {name}, not UTF-8 — Excel's 'Unicode Text' and EndNote on "
                "Windows both do this. Decoding it as UTF-8 would produce a corpus of records whose "
                "every field is interleaved with NUL bytes. Re-export as UTF-8."
            )
    if b"\x00" in payload:
        raise UnreadablePayloadError(
            f"{what} contains a NUL byte (U+0000), which PostgreSQL stores in neither text nor "
            "jsonb. This is almost always a UTF-16 file saved without a byte-order mark. "
            "Re-export as UTF-8."
        )
    return payload.decode("utf-8-sig", errors="replace")


def unsupported_format_blocker(format_hint: str) -> UnsupportedFormatError:
    """`01-…:427` — report the exact blocker, never a guessed mapping."""
    lowered = format_hint.lower()
    if "catchii" in lowered or "cachii" in lowered:
        return UnsupportedFormatError(
            "Catchii/Cachii workbook mappings are not supplied; importing one would require guessing column semantics",
            blocker="AHTA-DISC-003",
        )
    if lowered in {"xlsx", "xls", "workbook"}:
        return UnsupportedFormatError(
            "workbook import requires supplied mappings and round-trip fixtures",
            blocker="AHTA-DISC-004",
        )
    return UnsupportedFormatError(f"unsupported import format: {format_hint}")


# ---------------------------------------------------------------------------
# RIS
# ---------------------------------------------------------------------------

_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$")

#: A PMID label at the START of a field value, and nowhere else. See
#: :func:`_ris_pmid` — anchoring is the whole defence against MEDLINE's
#: `Comment in:` / `Erratum in:` notes, which carry ANOTHER paper's PMID.
#: Used with `.match()`, which anchors; `.search()` here would be the bug.
_LABELLED_PMID = re.compile(r"PMID\s*[:#]?\s*(\d{1,8})\b", re.IGNORECASE)

#: A bare PMID, when something else in the record already vouches for the
#: namespace. PubMed identifiers are sequential integers and the highest
#: assigned is around 4.1e7, so eight digits covers the whole namespace with
#: decades of room. The upper bound is what rejects an Embase accession such as
#: `2019123456`, which is ten digits of pure numerals and would otherwise look
#: exactly like a PMID.
_BARE_PMID = re.compile(r"^\d{1,8}$")

#: A bare PMID in a record that declares no source at all. Stricter than
#: :data:`_BARE_PMID` by exactly the amount the missing `DB` tag costs us; see
#: :func:`_ris_pmid`.
_UNCORROBORATED_PMID = re.compile(r"^[1-9]\d{6,7}$")

#: Tags scanned for a *labelled* PMID. `ID` is here for its label only — never
#: for its bare value; see :func:`_ris_pmid`.
_RIS_PMID_LABEL_TAGS = ("AN", "ID", "C2", "N1")

#: The record naming its own origin.
_PUBMED_SOURCE = re.compile(r"\b(?:pubmed|medline)\b", re.IGNORECASE)

#: …and the near-miss that is a DIFFERENT namespace. `DB  - PubMed Central`
#: contains the word "PubMed", but a PMC accession (`PMC8123456`, or the bare
#: `8123456` some exporters write) is not a PMID and normalizes into PubMed's
#: number space where it will collide with a real one.
_NOT_PUBMED_SOURCE = re.compile(r"pubmed\s*central|\bpmc\b", re.IGNORECASE)


def _ris_pmid(tags: dict[str, list[str]]) -> str | None:
    """The PMID, if this RIS entry actually carries one *about itself*.

    RIS has no PMID tag, so a PMID here is always an *inference*, and a wrong
    inference is far more expensive than a missing one: `normalized_pmid` is the
    second-strongest rule in
    :func:`domain.policies.deduplication_engine.find_pairs`, so a value read out
    of the wrong tag does not merely mislabel a record — it MERGES two different
    papers and asks a reviewer to pick a survivor between them.

    **A label is only trusted where the label starts the field.** This is the
    load-bearing rule, and it is not fussiness. MEDLINE records carry
    relational notes — `CIN`/`EIN`/`RIN`/`UIN` — and every RIS exporter puts
    them in `N1`::

        N1  - Comment in: Lancet. 2021 Jan 30;397(10272):341. PMID: 33357499
        N1  - Erratum in: N Engl J Med. 2021;384(2):199. PMID: 33357499

    That PMID belongs to the commentary or the erratum notice, NOT to the record
    holding the note. Scanning `N1` for a label anywhere in the value gave two
    entirely unrelated papers — one from Web of Science, one from Embase — the
    same `normalized_pmid`, and the dedup engine duly asked a reviewer which of
    the two to keep. Anchoring the label kills that whole class, including the
    variant where the exporter drops the `Comment in:` prefix and leaves only
    the citation, because prose still precedes the label.

    Zotero's and EndNote's self-identifying notes are unaffected: they write the
    field as `PMID: 33933206`, with the label first and nothing before it.

    `ID` is never read for its bare value. RIS defines `ID` as the library-local
    Reference ID, and Zotero, EndNote and Mendeley all number an export `1..N`.
    Importing two database searches into one review — the normal way a corpus is
    assembled — made every record in file A a "PMID match" against the
    same-ordinal record in file B.

    `AN` is an Accession Number, whose namespace depends on which database
    exported the record, so it is read against what the record says about its
    own origin:

    * The record names PubMed/MEDLINE (`DB  - Ovid MEDLINE(R)`) — any bare
      1-to-8-digit accession is that record's PMID.
    * The record names some OTHER database (`DB  - Embase`, `DB  - Scopus`, and
      critically `DB  - PubMed Central`, which contains the word "PubMed" and is
      not it) — nothing in `AN` is a PMID, whatever it looks like.
    * The record names nothing at all. Many exporters omit `DB` and `DP`
      entirely, and refusing those cost real dedup: two copies of one paper, both
      carrying `AN  - 33933206`, stopped merging. So the *shape* of the value
      stands in for the missing tag — seven or eight digits with no leading
      zero, which is every PMID assigned since 1976. That admits nothing the
      other accession namespaces emit: Web of Science prefixes `WOS:`, Scopus
      `2-s2.0-`, Embase either `L` or ten digits, CINAHL uses nine, PsycINFO
      embeds dashes. The leading-zero and seven-digit floors are what keep out
      zero-padded and small ordinal numbering.

    Everything rejected here still survives verbatim in `raw`, which is where a
    Web of Science accession belongs: evidence we retained, not an identifier we
    claimed to understand.
    """
    for tag in _RIS_PMID_LABEL_TAGS:
        for value in tags.get(tag, []):
            labelled = _LABELLED_PMID.match(value.strip())
            if labelled:
                return labelled.group(1)

    source = " ".join(tags.get("DB", []) + tags.get("DP", [])).strip()
    if source:
        if not _PUBMED_SOURCE.search(source) or _NOT_PUBMED_SOURCE.search(source):
            return None
        shape = _BARE_PMID
    else:
        shape = _UNCORROBORATED_PMID
    for value in tags.get("AN", []):
        candidate = value.strip()
        if shape.match(candidate):
            return candidate
    return None


def parse_ris(payload: bytes) -> list[ParsedRecord]:
    _check_bounds(payload)
    text = _decode_text(payload, "the RIS payload")
    records: list[ParsedRecord] = []
    current: dict[str, list[str]] = {}
    last_tag: str | None = None

    def flush() -> None:
        if not current:
            return
        if len(records) >= MAX_ROWS:
            raise ImportTooLargeError(f"more than {MAX_ROWS} records")
        records.append(_ris_to_record(current))

    for line in text.splitlines():
        if not line.strip():
            continue
        match = _RIS_LINE.match(line)
        if match:
            tag, value = match.group(1), match.group(2).strip()
            if tag == "ER":
                flush()
                current = {}
                last_tag = None
                continue
            if tag == "TY" and current:
                # A `TY` without a preceding `ER` — malformed, but recoverable:
                # treat it as the start of the next record rather than merging
                # two records into one.
                flush()
                current = {}
            current.setdefault(tag, []).append(value)
            last_tag = tag
        elif last_tag:
            # Continuation line.
            current[last_tag][-1] = f"{current[last_tag][-1]} {line.strip()}"
    flush()
    if not records:
        # RIS cannot express "this file is deliberately empty" — an empty RIS
        # export is zero bytes, which the route refuses before it gets here. So
        # bytes in and nothing out means the bytes were not RIS: a Latin-1 or
        # otherwise mangled export, or a PDF/CSV uploaded under
        # `source_kind=ris`. (A UTF-16 export is refused earlier still, by
        # `_decode_text`, which can name the encoding.)
        raise NoRecordsParsedError(
            "no RIS entry was found. RIS entries are `TAG  - value` lines terminated by `ER  -`; "
            "a file that is not RIS produces exactly this. Re-export as UTF-8 RIS, or correct source_kind."
        )
    return records


def _ris_to_record(tags: dict[str, list[str]]) -> ParsedRecord:
    raw = {k: (v if len(v) > 1 else v[0]) for k, v in tags.items()}
    ty = (tags.get("TY") or [""])[0].strip().upper()
    title = (tags.get("TI") or tags.get("T1") or tags.get("BT") or [None])[0]
    if title is None:
        return ParsedRecord(raw=raw, quarantine_reason="RIS entry has no title tag (TI/T1/BT)")
    return ParsedRecord(
        raw=raw,
        record_type=_RIS_TYPE_MAP.get(ty, "other"),
        title=_truncate(title),
        abstract=_truncate((tags.get("AB") or [None])[0]),
        publication_year=normalize_year((tags.get("PY") or tags.get("Y1") or [None])[0]),
        authors=[a for a in (tags.get("AU") or []) if a],
        container_title=(tags.get("JO") or tags.get("JF") or tags.get("T2") or [None])[0],
        doi=(tags.get("DO") or [None])[0],
        pmid=_ris_pmid(tags),
        url=(tags.get("UR") or [None])[0],
    )


# ---------------------------------------------------------------------------
# BibTeX
# ---------------------------------------------------------------------------


def _strip_brace_protection(value: str | None) -> str | None:
    r"""Remove BibTeX brace-protection, keeping what it protected.

    ``title = {The {DNA} Helix and {RNA}}`` means "The DNA Helix and RNA, and do
    not case-fold DNA or RNA". bibtexparser hands us the inner
    ``The {DNA} Helix and {RNA}``, and the previous ``strip("{} ")`` chewed
    characters off both ends of *that* string, yielding
    ``The {DNA} Helix and {RNA`` — a title with a stray opening brace and a
    missing closing one, which is what the reviewer, the record export and the
    PRISMA appendix all then showed. Google Scholar, Zotero and every major
    publisher brace-protect acronyms, so this is a large share of real BibTeX
    rather than an edge case.

    An unbalanced closing brace is KEPT. Malformed input should stay visibly
    malformed; inventing a repair is how a parser starts lying about its source.

    Deliberately NOT a LaTeX engine. ``\'{e}``, ``\textbf{...}``, ``$x_{i}$``
    and escaped ``\{`` are left exactly as they arrive. Decoding LaTeX is a
    separate job with its own failure modes, and a half-decoder that turns
    ``\'{e}`` into ``\'e`` is worse than one that does not touch it.
    """
    if value is None:
        return None
    depth = 0
    out: list[str] = []
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        elif char == "}":
            out.append(char)
        else:
            out.append(char)
    return "".join(out).strip() or None


def parse_bibtex(payload: bytes) -> list[ParsedRecord]:
    _check_bounds(payload)
    text = _decode_text(payload, "the BibTeX payload")
    try:
        import bibtexparser
        from bibtexparser.bparser import BibTexParser
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise UnsupportedFormatError("bibtexparser is not installed") from exc

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    try:
        database = bibtexparser.loads(text, parser=parser)
    except Exception as exc:  # noqa: BLE001 - a whole-file parse failure is one quarantine
        return [ParsedRecord(raw={"raw_text": text[:MAX_FIELD_CHARS]}, quarantine_reason=f"BibTeX parse failed: {exc}")]

    if len(database.entries) > MAX_ROWS:
        raise ImportTooLargeError(f"more than {MAX_ROWS} entries")

    records: list[ParsedRecord] = []
    for entry in database.entries:
        title = _strip_brace_protection(entry.get("title"))
        if not title:
            records.append(ParsedRecord(raw=dict(entry), quarantine_reason="BibTeX entry has no title field"))
            continue
        authors = [a.strip() for a in re.split(r"\s+and\s+", entry.get("author", "")) if a.strip()]
        records.append(
            ParsedRecord(
                raw=dict(entry),
                record_type=_BIBTEX_TYPE_MAP.get((entry.get("ENTRYTYPE") or "").lower(), "other"),
                title=_truncate(title),
                abstract=_truncate(entry.get("abstract")),
                publication_year=normalize_year(entry.get("year")),
                authors=authors,
                container_title=_strip_brace_protection(entry.get("journal") or entry.get("booktitle")),
                doi=entry.get("doi"),
                pmid=entry.get("pmid"),
                url=entry.get("url"),
            )
        )
    if not records:
        # bibtexparser returns an empty database for anything with no `@type{`
        # in it — a RIS or CSV file declared as `source_kind=bibtex` reaches
        # here, and used to import as a successful batch of nothing.
        raise NoRecordsParsedError(
            "no BibTeX entry was found. BibTeX entries start with `@article{`, `@misc{` and so on; "
            "a file with none of those is not BibTeX. Correct source_kind, or re-export."
        )
    return records


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

#: Header aliases, lowercased. A header we do not recognise is kept in `raw` and
#: simply not mapped — never guessed onto a field it might not be.
_CSV_ALIASES = {
    "title": "title",
    "article title": "title",
    "abstract": "abstract",
    "year": "publication_year",
    "publication year": "publication_year",
    "authors": "authors",
    "author": "authors",
    "journal": "container_title",
    "source title": "container_title",
    "doi": "doi",
    "pmid": "pmid",
    "pubmed id": "pmid",
    "pmcid": "pmcid",
    "arxiv": "arxiv_id",
    "url": "url",
    "record_type": "record_type",
}


def parse_csv(payload: bytes) -> list[ParsedRecord]:
    """A delimited export, gated on its header row.

    The header gate is checked FIRST and independently of how many data rows
    follow. A file whose header names not one recognised bibliographic column is
    not a CSV of records, and the number of lines under it says nothing about
    that: a PDF declared as `source_kind=csv` parses as a two-row table with the
    header `%PDF-1.7`, so gating on "zero rows AND no recognised column" let it
    through as `201` with two quarantined rows — the caller was told their PDF
    imported, and the quarantine reason blamed a missing title column.

    A recognisable header with zero data rows still returns `[]`. That IS the
    shape of a search that found nothing — every database exports its header row
    regardless — and the decision about whether an empty corpus may be recorded
    belongs to the import service, which is the layer that knows whether the
    caller declared it.
    """
    _check_bounds(payload)
    text = _decode_text(payload, "the CSV payload")
    records: list[ParsedRecord] = []

    # `csv.field_size_limit` is a process-global 128 KiB, which is smaller than a
    # single Embase or PsycINFO abstract — a 140k-character abstract raised
    # `_csv.Error: field larger than field limit` and reached the caller as a
    # 500 blamed on nothing in particular. The payload bound is the real limit
    # and `_truncate` bounds what is stored, so the parse limit is raised to the
    # payload bound for the duration and restored afterwards: leaving it raised
    # would silently change the behaviour of every other csv reader in the
    # process. Raised before `fieldnames` is touched, because reading the header
    # is itself a parse and an over-long header row would hit the same limit.
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_PAYLOAD_BYTES)
    try:
        reader = csv.DictReader(io.StringIO(text))
        try:
            headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        except csv.Error as exc:
            raise NoRecordsParsedError(f"the CSV header row could not be read: {exc}") from exc
        if not any(header in _CSV_ALIASES for header in headers):
            raise NoRecordsParsedError(
                "no column in the header row matched a recognised bibliographic header "
                f"({', '.join(sorted(_CSV_ALIASES))}), so this file is not a CSV of records. "
                "An empty search result still exports its header row."
            )
        try:
            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    raise ImportTooLargeError(f"more than {MAX_ROWS} rows")
                raw = {k: v for k, v in row.items() if k is not None}
                mapped: dict[str, Any] = {}
                for header, value in raw.items():
                    key = _CSV_ALIASES.get((header or "").strip().lower())
                    if key and value:
                        mapped[key] = value
                title = mapped.get("title")
                if not title:
                    records.append(ParsedRecord(raw=raw, quarantine_reason="CSV row has no recognisable title column"))
                    continue
                record_type = (mapped.get("record_type") or "").strip().lower()
                records.append(
                    ParsedRecord(
                        raw=raw,
                        record_type=record_type if record_type in _RIS_TYPE_MAP.values() else "other",
                        title=_truncate(title),
                        abstract=_truncate(mapped.get("abstract")),
                        publication_year=normalize_year(mapped.get("publication_year")),
                        authors=[a.strip() for a in re.split(r"[;|]", mapped.get("authors", "")) if a.strip()],
                        container_title=mapped.get("container_title"),
                        doi=mapped.get("doi"),
                        pmid=mapped.get("pmid"),
                        pmcid=mapped.get("pmcid"),
                        arxiv_id=mapped.get("arxiv_id"),
                        url=mapped.get("url"),
                    )
                )
        except csv.Error as exc:
            # Dialect-level damage the raised limit does not cover: a newline
            # inside an unquoted field on a strict dialect.
            raise NoRecordsParsedError(f"payload could not be read as CSV: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)
    return records


# ---------------------------------------------------------------------------
# Canonical JSON, and the discovery-engine session export
# ---------------------------------------------------------------------------


def _require_storable(value: str, what: str, path: tuple[object, ...], *, is_key: bool = False) -> None:
    """Text that PostgreSQL can actually hold.

    Two code points are valid JSON, valid Python `str`, and storable in neither
    `text` nor `jsonb`. `json.loads` accepts both without complaint, so both
    reached the write and came back as a 500 or as somebody else's error string:

    * **A lone surrogate.** `"a\\ud800b"` is well-formed JSON — RFC 8259 permits
      an unpaired escape — and produces a `str` that cannot be encoded to UTF-8
      at all. RFC 8785 canonicalization refused it first, with
      ``UnsupportedTypeError: lone surrogate U+D800 is not valid Unicode``,
      which the app had no handler for: `500`, `text/plain`, no problem+json.
    * **NUL.** `U+0000` encodes fine and PostgreSQL rejects it, with
      ``unsupported Unicode escape sequence`` — a raw driver string served as
      `409 state_conflict`.

    Refused for the whole payload rather than quarantined per row. A quarantined
    row must persist its `raw`, and these are precisely the values that make
    that INSERT impossible; storing a repaired `raw` would break the rule that
    the original representation is always retained. There is no honest partial
    answer here, so the answer is a typed refusal naming the code point.
    """
    if "\x00" in value:
        raise UnreadablePayloadError(
            f"{what} contains a NUL character (U+0000) at {_where(path, is_key=is_key)}. PostgreSQL "
            "stores U+0000 in neither text nor jsonb, so this record cannot be persisted even as a "
            "quarantined row. Remove it and re-submit."
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnreadablePayloadError(
            f"{what} carries an unpaired UTF-16 surrogate at index {exc.start} of "
            f"{_where(path, is_key=is_key)}. A `\\uD800`-`\\uDFFF` escape with no pair is accepted "
            "by JSON parsers but is not valid Unicode, cannot be canonicalized under RFC 8785 and "
            "cannot be stored. Emit the character as a surrogate PAIR, or drop it."
        ) from exc


def _path_label(path: tuple[object, ...]) -> str:
    """A JSON pointer-ish path, elided in the middle when it is absurdly long.

    The depth failure produces a 64-segment path of nothing but `[0]`, which is
    noise in a problem+json `detail`. The ends are the informative part.
    """
    parts = [f"[{p}]" if isinstance(p, int) else f".{p}" for p in path]
    if len(parts) > 8:
        parts = parts[:4] + [f" … {len(parts) - 8} more … "] + parts[-4:]
    return "$" + "".join(parts)


def _where(path: tuple[object, ...], *, is_key: bool = False) -> str:
    label = _path_label(path)
    return f"the object key {label}" if is_key else label


def _audit_json(data: Any, what: str) -> None:
    """One iterative pass for the things that survive `json.loads` and then kill us.

    Iterative on an explicit stack, not recursive, because the entire point is to
    be the check a recursive walker cannot perform on its own input.

    The path is carried as a tuple and rendered only when something fails. A
    50,000-record import walks a few million nodes through here, and formatting a
    human-readable path at every one of them — to throw all of them away — was
    the obvious way to make a correctness check cost real import latency.
    """
    stack: list[tuple[Any, int, tuple[object, ...]]] = [(data, 1, ())]
    while stack:
        node, depth, path = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise UnreadablePayloadError(
                f"{what} nests more than {MAX_JSON_DEPTH} levels deep at {_path_label(path)}. A "
                "bibliographic record is a flat object with a list of authors in it; nothing "
                "legitimate is this deep, and the recursive walkers downstream (denylist scan, "
                "RFC 8785 hashing) would exhaust the stack on it."
            )
        if isinstance(node, str):
            _require_storable(node, what, path)
        elif isinstance(node, dict):
            for key, value in node.items():
                _require_storable(key, what, (*path, key), is_key=True)
                stack.append((value, depth + 1, (*path, key)))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                stack.append((item, depth + 1, (*path, index)))


def _decode_json(payload: bytes, what: str) -> Any:
    """Bytes -> JSON, with every failure mode typed.

    `json.loads(bytes)` sniffs the encoding itself and raises a bare
    `UnicodeDecodeError` on Latin-1 or UTF-16 input — a subclass of ValueError
    that the `json.JSONDecodeError` handler did not catch, so a mis-encoded
    upload arrived as a 500. `utf-8-sig` rather than `utf-8` because Windows
    tooling emits a BOM in front of otherwise valid UTF-8 JSON.

    `RecursionError` is caught here too, and is the SECOND line of the defence
    against deep nesting rather than the first — a distinction worth being exact
    about, because the obvious story is wrong. `{"records":` + `[` * 20000 +
    `]` * 20000 is a **40 KB** body needing no authentication-shaped setup, and
    it did produce a bare `500` with a plain-text body — not even problem+json
    for the gateway to forward, since `RecursionError` derives from
    `RuntimeError` and slipped past every `ValueError` handler. But it was not
    raised in here: measured on CPython 3.14, this decoder's C scanner absorbs
    ~114,000 levels, and the throw came from `find_forbidden_keys` — the denylist
    walk the import service runs over the parsed rows, which gives out around
    1,046. :func:`_audit_json` and `MAX_JSON_DEPTH` are what actually close that,
    and they close it for RFC 8785 hashing and the JSONB write at the same time.

    This arm still earns its place: past ~114,000 levels the decoder does throw,
    and it throws before `_audit_json` gets a structure to measure. There is no
    way to bound the depth first without writing a second JSON lexer.
    """
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnreadablePayloadError(
            f"{what} is not UTF-8: {exc.reason} at byte {exc.start}. JSON is UTF-8 by RFC 8259, "
            "so re-export the file as UTF-8 rather than Latin-1 or UTF-16."
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnreadablePayloadError(f"{what} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        raise UnreadablePayloadError(
            f"{what} nests too deeply to parse; the limit is {MAX_JSON_DEPTH} levels. "
            "A bibliographic record is a flat object with a list of authors in it."
        ) from exc
    _audit_json(data, what)
    return data


def parse_canonical_json(payload: bytes) -> list[ParsedRecord]:
    _check_bounds(payload)
    data = _decode_json(payload, "payload")
    items = data.get("records") if isinstance(data, dict) else data
    if not isinstance(items, list):
        # `{"records": []}` and `[]` are structurally explicit emptiness and
        # parse to `[]`; whether an empty corpus may be RECORDED is the import
        # service's decision. Anything that is not a list is a shape error, not
        # emptiness, and is refused here.
        raise UnsupportedFormatError("canonical JSON must be a list of records, or {'records': [...]}")
    if len(items) > MAX_ROWS:
        raise ImportTooLargeError(f"more than {MAX_ROWS} records")
    records: list[ParsedRecord] = []
    for item in items:
        if not isinstance(item, dict):
            records.append(ParsedRecord(raw={"value": item}, quarantine_reason="record is not an object"))
            continue
        records.append(_json_to_record(item))
    return records


#: The key discovery-engine puts the record list under, at every
#: `document_version` it has released.
#:
#: Checked in the producer rather than assumed:
#: `review_export_service._document` builds the sealed document with
#: ``"records": records``, and its `REVIEW_EXPORT_DOCUMENT_VERSION` history
#: records what each bump changed — 2 removed `taken_at`, 3 removed
#: `session_name`. Neither touched the record list. `results` and `items` are
#: not discovery-engine spellings at any version.
_DISCOVERY_RECORD_KEY = "records"

#: Also accepted, but only from a body that does NOT name a document version.
#: These are the hand-built and non-discovery-engine shapes this parser served
#: before the frozen batch existed.
_RESULT_LIST_KEYS = ("results", _DISCOVERY_RECORD_KEY, "items")


def _result_list_key(data: dict[str, Any]) -> str:
    """Which key holds the records — by the producer's contract, not by list order.

    This was ``for key in ("results", "records", "items"): if key in data``,
    taking the first key PRESENT. That is defensible only if at most one of them
    can ever appear, and nothing enforced that. Measured on the old code:

    * ``{"results": [one], "records": [A, B]}`` imported ONE record and dropped
      two, silently, `201`;
    * ``{"results": [], "records": [A]}`` imported zero — a sealed, screened,
      internally consistent corpus that was never there.

    The docstring below defends that loop on the grounds that a renamed key
    upstream lands loudly instead of importing an empty corpus. True — but only
    for names outside these three. For a body carrying two of them the loop IS
    the silent partial import it was written to prevent.

    Two rules replace it:

    **A body that names its `document_version` must use `records`.** The version
    is the producer identifying itself as a discovery-engine frozen batch, and
    that producer has emitted `records` at every version. A document declaring a
    version and carrying `results` did not come from it, whatever else is true
    of it, and importing it would attach discovery-engine provenance to a
    document discovery-engine did not write.

    **An unversioned body may use any ONE of the three, and no more.** The
    tolerance predates the frozen batch and the bodies relying on it are not
    discovery-engine documents. But two keys is a self-contradicting document,
    and it is refused rather than resolved: choosing `records` because it is
    "the real one" would be this parser deciding which half to believe, and the
    reviewer would never learn the other half was there.
    """
    present = [key for key in _RESULT_LIST_KEYS if key in data]

    if "document_version" in data:
        if _DISCOVERY_RECORD_KEY in data and len(present) == 1:
            return _DISCOVERY_RECORD_KEY
        raise UnsupportedFormatError(
            f"this document declares document_version {data['document_version']!r}, so its records "
            f"must be a single top-level '{_DISCOVERY_RECORD_KEY}' list; it carries "
            f"{present or 'none of the accepted keys'}. Every released version of a "
            "discovery-engine review-export batch puts the record list under "
            f"'{_DISCOVERY_RECORD_KEY}'. Nothing was imported."
        )

    if len(present) > 1:
        raise UnsupportedFormatError(
            f"this session export carries more than one record list: {present}. Which one is the "
            "corpus is the producer's statement to make, not this parser's — reading the first and "
            "discarding the rest would import a subset and report it as the whole. Nothing was "
            "imported: send exactly one of "
            f"{', '.join(repr(k) for k in _RESULT_LIST_KEYS)}."
        )

    if not present:
        raise UnsupportedFormatError(
            "session export carries no result list: expected one of 'results', 'records' or 'items'. "
            "A renamed key upstream lands here rather than importing an empty corpus."
        )

    return present[0]


def _reconcile_record_count(data: dict[str, Any], items: list[Any], key: str) -> None:
    """Check the document's own `record_count` against the list actually read.

    `discovery-engine` writes ``"record_count": len(records)`` into the document
    BEFORE it is canonicalised and digested
    (`review_export_service._document`), and has done so in every
    `document_version` — 1, 2 and 3. It is therefore not a transport header a
    proxy can rewrite but the producer's own statement about the bytes it
    sealed, frozen at the same instant as the records.

    Nothing in this service read it. `record_count` appeared nowhere in `src/`
    except one docstring, so a document declaring 999 records against one record
    imported as a one-record corpus with a `201`, and so did one declaring 0.

    ## What this catches that the digest does not

    On `/imports/from-discovery` the fetched bytes are hashed and compared to
    `expected_digest`, so a document truncated IN TRANSIT already fails there.
    This is not a second copy of that check, and it is not redundant with it:

    * it catches a document that arrives WHOLE and is still read wrongly —
      `{"record_count": 93, "results": [x], "records": [93 items]}` is
      byte-perfect with a valid digest, and the key-selection loop below used to
      take `results` and import one record. `record_count` is the only thing in
      the document that makes those two facts contradict each other;
    * it covers every route where no digest is checked against anything at all.
      `POST /imports?source_kind=...` hashes whatever arrived and compares it to
      nothing, so the digest proves only that a retry is a replay.

    ## Absent: accepted, except from a document that names its own version

    A missing `record_count` is not a disagreement, and every producer this
    parser serves other than discovery-engine — and every hand-built
    `{"results": [...]}` body — omits it. Refusing on absence would break
    working producers over a field they never promised.

    `document_version` is where that stops. It is the producer identifying
    itself as a discovery-engine frozen batch, and every version of that
    document emits `record_count` unconditionally. A document carrying a version
    and no count is not an OLD document — it is an incoherent one, or one a
    field was stripped from. Accepting it would make this whole reconciliation
    opt-out: a producer wanting its count unchecked need only leave it out.
    """
    declared = data.get("record_count")

    if "record_count" not in data:
        if "document_version" in data:
            raise UnsupportedFormatError(
                f"this document declares document_version {data['document_version']!r} but carries no "
                "record_count. Every version of a discovery-engine review-export batch writes that "
                "field into the digested bytes, so a batch without it has been altered or truncated "
                "after it was sealed. Nothing was imported."
            )
        return

    # `bool` before `int`: `True` is an `int` in Python and would compare equal
    # to a one-record document, so a producer emitting `true` would be accepted
    # as a correct count of one. `1.5` and `"1"` are refused for the same
    # family of reason — a stringified count compares unequal to every length
    # and would be reported as a corpus mismatch, sending whoever reads the
    # error looking for missing records that are all present.
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise UnsupportedFormatError(
            f"this document's record_count is {declared!r}, which is not a whole number. It is the "
            "producer's count of the records it sealed and has to be comparable with one. Nothing "
            "was imported."
        )

    if declared != len(items):
        raise UnsupportedFormatError(
            f"this document declares record_count {declared} but carries {len(items)} records under "
            f"'{key}'. The count is written into the digested bytes by the producer, so the two "
            "disagreeing means the document is short, truncated, or was read under the wrong key. "
            "Nothing was imported: an import of the smaller number would seal a subset of the "
            "corpus and report it as the whole one."
        )


def parse_discovery_session_export(payload: bytes) -> tuple[list[ParsedRecord], dict[str, Any] | None]:
    """A `discovery-engine` ``GET /api/sessions/{id}/export`` body.

    Returns the records and, separately, the PRISMA acquisition provenance —
    `per_source_counts`, `raw_total`, `deduped_total` — when the export carries
    it. Identified counts are derived from our own `import_rows` either way, so
    a missing block degrades provenance without blocking the import.

    ## ✅ CONNECTED 2026-08-13. The provenance arm has now run, on real data.

    Measured end to end rather than asserted. `discovery-engine`
    `POST /api/sessions/{id}/review-export` was taken against the live 93-paper
    `tocilizumab covid-19 randomised` session (43 queries), and the 463,808 bytes
    it froze were handed to THIS function inside the running
    `evidence-review` pod:

        records            : 93
        quarantined        : 0
        provenance         : {'per_source_counts': {'europe_pmc': 623,
                               'pubmed': 623, 'semantic_scholar': 75},
                              'raw_total': 1321, 'deduped_total': None,
                              'session_id': '019fe51a-…'}
        records with doi   : 68
        records with pmid  : 59

    `deduped_total` is `None` **by the producer's deliberate choice**, not for
    want of a value: summing its per-query figures double-counts a paper two
    queries both found, and the record count has had the open-access filter
    applied, so reporting either would fold "excluded as ineligible" into
    "duplicate records removed". The producer emits a stated basis alongside it.

    ## What is still true: the `/export` body remains unimportable

    The connected producer is the frozen `review-export` batch, NOT
    `GET /api/sessions/{id}/export`. That endpoint still returns `SessionExport`
    — ``{session_id, format, data, paper_count}`` with the records inside `data`
    as a JSON *string* under `papers` — and is still refused here with
    *"session export carries no result list"*. That refusal is still correct and
    is deliberately unchanged: a session export is mutated by later searches, so
    a shape this parser merely tolerated would look reproducible without being
    so. The frozen batch is the one that is reproducible, because its bytes
    cannot move.

    ## The history this replaces, kept because it explains the shape

    An earlier docstring said *"`20b498e` on `discovery-engine` `main` added
    those fields; older exports do not have them"*, which reads as "new exports
    have it, old ones do not" — a migration story, and finished work. Both
    halves were false, and both were checked rather than assumed:

    * **`20b498e` touches no session-export file.** `git show --stat` on that
      commit lists `models/search_query.py`, `schemas/search.py`,
      `services/search_service.py`, a migration and tests. Not
      `routers/sessions.py`, not `schemas/session.py`, not
      `services/export_service.py`. It added the three counts to the
      **`search_queries` table**, which is a different artifact.
    * **No discovery-engine code writes them into a session export.** The only
      references outside its models are in `search_service`, where the counts
      are computed and persisted on a `SearchQuery` row. `ExportService` does
      not read that table.

    That was the state until `discovery-engine` #55 added the frozen batch and
    #56 put its digest through the released RFC 8785 canonicaliser. The harvest
    branch below is no longer dead code: it is the arm the live producer feeds,
    and the numbers at the top of this docstring came out of it.

    ## The `/export` mismatch that made the gap visible

    `SessionExport`
    (`discovery-engine/src/schemas/session.py`) is
    ``{session_id, format, data, paper_count}`` where **`data` is a JSON
    *string*** whose object nests the records under a **`papers`** key.
    `_result_list_key` requires `results`, `records` or `items` at the TOP level
    of the body, so a genuine `GET /api/sessions/{id}/export?format=json` does
    not import partially or lose its provenance — it is refused outright with
    *"session export carries no result list"*.

    That refusal is the correct behaviour and is why the seam was visible at all
    rather than importing an empty corpus. It was connected by a change to
    discovery-engine's contract — the persisted review-export batch — and not by
    a second accepted key here, deliberately: a session export is mutated by
    later searches, so a shape this parser merely tolerated would look
    reproducible without being so. **That reasoning still holds and the `papers`
    key is still not accepted.** The refusal above is load-bearing, not
    leftover.

    ## Correction: that argument only ever covered names outside the three

    The paragraph above defends accepting three key names on the grounds that a
    renamed key upstream lands loudly instead of importing an empty corpus. That
    is true of `papers`, and it was NOT true of a body carrying two of the three
    accepted names. `{"results": [one], "records": [A, B]}` imported one record
    and dropped two, silently, `201` — the exact silent partial import this
    docstring claims the shape prevents — because the selection took the first
    key PRESENT rather than the one the producer's contract names.

    `_result_list_key` replaces that: a body naming its `document_version` must
    carry `records` (the key discovery-engine emits at every released version),
    an unversioned body may carry any ONE of the three, and two of them is
    refused rather than resolved by list order. See that function for the
    measurements.
    """
    _check_bounds(payload)
    data = _decode_json(payload, "session export")
    if not isinstance(data, dict):
        raise UnsupportedFormatError("session export must be a JSON object")

    # `data.get("results") or data.get("records") or ... or []` conflated three
    # different facts into one empty list: the key is ABSENT (an upstream rename
    # — the export would then import as an empty corpus forever, and nothing
    # downstream would disagree), the key is NULL (upstream reporting a failure),
    # and the key is `[]` (a search that legitimately found nothing). Only the
    # third is an import.
    key = _result_list_key(data)
    items = data[key]
    if items is None:
        raise UnsupportedFormatError(
            f"session export's '{key}' is null. A session that found nothing must send []; null is "
            "upstream reporting a failure, and importing it would record that failure as a completed search."
        )
    if not isinstance(items, list):
        raise UnsupportedFormatError(f"session export's '{key}' is a {type(items).__name__}, not a list")
    if len(items) > MAX_ROWS:
        raise ImportTooLargeError(f"more than {MAX_ROWS} results")

    _reconcile_record_count(data, items, key)

    provenance: dict[str, Any] = {}
    for key in ("per_source_counts", "raw_total", "deduped_total", "session_id", "query", "executed_at"):
        # Both flat and nested-under-`prisma` shapes appear in the wild.
        if key in data:
            provenance[key] = data[key]
        elif isinstance(data.get("prisma"), dict) and key in data["prisma"]:
            provenance[key] = data["prisma"][key]

    return [
        _json_to_record(i)
        if isinstance(i, dict)
        else ParsedRecord(raw={"value": i}, quarantine_reason="result is not an object")
        for i in items
    ], (provenance or None)


class _FieldShapeError(ValueError):
    """A JSON field whose type makes it unusable. Internal: it becomes a quarantine."""

    def __init__(self, field: str, value: object, expected: str) -> None:
        super().__init__(f"field {field!r} must be {expected}; got {type(value).__name__}")
        self.field = field


def _require_text(field: str, value: object) -> str | None:
    """A field that must arrive as a string, or the row is quarantined.

    Applied to `title`, `abstract`, `container_title`, `doi` and `url`. A
    container used to be handed straight to a `String` column and blew up at
    flush, so a malformed upload was reported as a database error rather than as
    bad input. A *number* is refused here too: `"doi": 10.1000` is a producer
    bug, and coercing it to `"10.1000"` would manufacture a plausible-looking
    DOI — the highest-confidence merge rule in the dedup engine — out of
    nothing.
    """
    if value is None or isinstance(value, str):
        return value
    raise _FieldShapeError(field, value, "a string")


def _require_scalar(field: str, value: object) -> object:
    """A field that must arrive as one value, not a container of them.

    `normalize_year` takes `str(value)` and pulls four digits out of it, so
    `{"publication_year": [1999]}` produced the string `"[1999]"` and the year
    `1999` — a list silently coerced to the scalar it happened to contain, which
    is exactly what :func:`_require_text` exists to refuse elsewhere. Year is
    not cosmetic: `normalized_title + publication_year` is the third dedup rule,
    so a year invented out of a container's `repr` decides merges.

    A `[1999, 2001]` would have produced `1999` just as confidently, and that is
    the tell — the coercion has no idea it discarded the second element.
    """
    if value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    raise _FieldShapeError(field, value, "a single string or number")


def _identifier_text(field: str, value: object) -> str | None:
    """An identifier field, where an integer is a faithful encoding.

    `{"pmid": 33933206}` is how half the world's JSON emits a PMID and it means
    exactly what `"33933206"` means, so it is coerced. A container still is not,
    and the guard is kept for a reason that has changed since it was written.

    It used to say `str({"a": 1})` is `"{'a': 1}"`, which `normalize_pmid`
    reduces to the digits `1` — an object-shaped pmid silently becoming the
    dedup key `1`. That is no longer what happens: `normalize_pmid` is anchored
    now and returns `None` for it, measured. The refusal here is therefore no
    longer load-bearing against a false merge, and it stays anyway, because the
    alternative is to let a container reach a normalizer and rely on that
    normalizer's shape to be the only thing between a `dict` and a dedup key.
    `_require_text` and `_require_scalar` exist for the same reason: a field
    whose SHAPE is wrong is quarantined with its bytes, where a human can see
    it, rather than silently coerced into whatever the far end tolerates today.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise _FieldShapeError(field, value, "a string or an integer")


def _json_to_record(item: dict[str, Any]) -> ParsedRecord:
    try:
        return _json_record_fields(item)
    except _FieldShapeError as exc:
        return ParsedRecord(raw=item, quarantine_reason=f"unusable {exc}")


def _external_ids(item: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The nested identifier object, if the producer writes one.

    `discovery-engine`'s `ExportService._paper_to_dict` emits every identifier
    inside `external_ids` and never at the top level, so a record read flat-only
    imported with `doi`, `pmid` and `arxiv_id` all NULL — accepted, conserved,
    `201`, and two of the three dedup rules quietly reduced to no-ops.

    Anything that is not an object reads as "no nested identifiers", exactly as
    absent and `null` do, and is reported as an unusable source rather than
    refused. This key was IGNORED entirely before the nested read existed, so a
    record whose flat `title`, `doi` and `pmid` are all perfectly readable used
    to import and must still import: `"external_ids": []` is how PHP, Perl and
    Ruby serialise an empty map, `canonical_json` is arbitrary user-pasted JSON,
    and discarding a whole record over a sibling key we did not need would be a
    regression caused entirely by opting in to reading it.
    """
    value = item.get("external_ids")
    if isinstance(value, dict):
        return value, ()
    if value is None:
        return {}, ()
    return {}, ("external_ids",)


def _present(value: object) -> bool:
    """Did the producer actually put something here?

    `None` is an absent key and `""` is a producer saying it has nothing. Every
    other value is present — including `0` and `[]`, which are wrong-SHAPED
    rather than absent and must reach the type check that refuses them.
    """
    return value is not None and value != ""


def _identified(
    field: str,
    primary: object,
    fallbacks: Sequence[tuple[str, object]],
    validate: Callable[[str, object], str | None],
) -> tuple[str, str | None, tuple[str, ...]]:
    """One identifier, its source label, and any source that was unusable.

    Two tiers, and the difference between them is the whole design:

    * `primary` is what this parser read BEFORE `external_ids` existed here,
      passed to `validate` exactly as it was. Its outcomes — including its
      refusals — are unchanged, so a payload that imported yesterday imports
      today with the same values, and one that quarantined still quarantines.
      Strictness belongs to the VALUE, never to the position of a key in a
      fallback chain: a chain that steps over `{"doi": 0}` on its way to a
      nicer-looking key has quietly declined to report a producer bug.
    * `fallbacks` are keys this parser newly opted in to reading — `pdf_url` and
      the members of `external_ids`. They are consulted only when the primary
      yielded nothing, and they are BEST-EFFORT: a malformed one is skipped and
      named in the returned tuple, never fatal. A key that was ignored last
      week cannot start discarding records this week, or reading more of a
      payload becomes a way to import less of it.

    The label travels with the value because `field_provenance` records it:
    `doi`, `external_ids.doi` and `external_ids.pubmed_id` are three different
    answers to "where did this dedup key come from", and only one of them is
    true for any given row.
    """
    value = validate(field, primary)
    if _present(value):
        return field, value, ()
    unusable: list[str] = []
    for label, candidate in fallbacks:
        if not _present(candidate):
            continue
        try:
            resolved = validate(label, candidate)
        except _FieldShapeError:
            unusable.append(label)
            continue
        if _present(resolved):
            return label, resolved, tuple(unusable)
    return field, value, tuple(unusable)


#: Spellings real producers emit for a type that IS in the vocabulary, keyed by
#: the punctuation-normalised form. Every entry is a vocabulary the corpus
#: actually arrives in, not a guess:
#:
#: * **Crossref** `type`: ``journal-article`` — normalisation alone resolves it —
#:   and ``posted-content``, which is how Crossref registers a preprint. This was
#:   the sharpest case: `journal-article` is Crossref's literal string, so every
#:   Crossref-derived producer stored `"other"`.
#: * **RIS** `TY` tags: ``JOUR``, ``RPRT``, ``DATA``.
#: * **BibTeX** entry types: ``article``, ``techreport``.
#: * **JATS** `article-type`: ``research-article``.
#:
#: Spellings for types genuinely OUTSIDE the vocabulary are deliberately absent:
#: Crossref `proceedings-article`, RIS `CPAPER`, BibTeX `inproceedings` and
#: `systematic_review` have no honest target among the five and must land on
#: `other` — annotated, so the value is not lost.
_RECORD_TYPE_ALIASES = {
    "article": "journal_article",
    "research_article": "journal_article",
    "jour": "journal_article",
    "posted_content": "preprint",
    "techreport": "report",
    "tech_report": "report",
    "rprt": "report",
    "data": "dataset",
}


def _canonical_record_type(declared: str | None) -> tuple[str, tuple[str, ...]]:
    """The stored `record_type`, plus an annotation when a declared value was dropped.

    This was one expression: ``declared if declared in (...) else "other"``. It
    rewrote anything outside the five to `"other"` and said nothing. Measured
    before the fix: canonical-JSON records declaring `article`,
    `journal-article`, `Journal Article`, `conference_paper` and
    `systematic_review` all returned `201` and were stored and SEALED as
    `"other"`, with nothing in the batch response reporting a coercion.

    `record_type` is `RECORD_CONTENT_FIELDS[0]`, so it flows into `content_hash`
    → `member_manifest_hash` → `items_hash` → `manifest_hash` on append-only
    tables. The rewrite was therefore sealed, and what the producer said survived
    only in `import_rows.raw`, which nothing that reads a record reads.

    ## Two different problems, two different answers

    **A spelling is not an unknown type.** `journal-article` (Crossref),
    `Journal Article` (RIS/Zotero) and `article` (BibTeX) all mean the type this
    service already has a value for. Punctuation normalisation plus
    `_RECORD_TYPE_ALIASES` resolves them. The stored value stays inside the five,
    so this does not widen anything.

    **A genuinely unknown type is still a real study.** `conference_paper` and
    `systematic_review` have no honest target among the five. They still store
    as `"other"` — and the declared value is recorded in `unusable_sources`,
    which reaches `field_provenance` on `GET /records/{id}`.

    ## Why not the two obvious fixes

    *Widen the vocabulary*: the database refuses it. `ck_record_version_record_type`
    would reject the INSERT at flush, turning a metadata surprise into an import
    reported as a database error.

    *Refuse or quarantine the record*: an unknown `record_type` is a real paper
    with an unusual label on it. Dropping the record over a metadata field is
    the precise failure the coercion existed to avoid, and it is worse than the
    silence. Note the SHAPE case is different and is unchanged: `record_type:
    ["report"]` is a producer emitting the wrong JSON type and still quarantines
    via `_require_text`, which is already loud (`quarantined_count`).

    `field_provenance` is NOT in `RECORD_CONTENT_FIELDS`, so the annotation half
    changes no `content_hash` and breaks no seal. The alias half DOES change
    `content_hash` — for newly imported records only, and the tables are
    append-only, so no existing row or seal moves.
    """
    if not declared:
        # Absent is not a claim. Annotating it would put an entry on most rows of
        # a real corpus — RIS, BibTeX and CSV rows mostly declare no type — and
        # drown the cases that mean something.
        return "other", ()

    # Punctuation only. `journal-article`, `Journal Article` and `journal_article`
    # are one type written three ways, and no producer distinguishes them.
    normalized = declared.strip().lower().replace("-", "_").replace(" ", "_")

    if normalized in RECORD_TYPES:
        return normalized, ()
    if normalized in _RECORD_TYPE_ALIASES:
        return _RECORD_TYPE_ALIASES[normalized], ()

    # Coerced, and said so. The declared value travels so the reader of a record
    # typed `other` can tell "the producer said systematic_review and we could
    # not store it" from "the producer said nothing".
    #
    # BOUNDED, and not verbatim. This string is written into `field_provenance`,
    # a JSONB column on an APPEND-ONLY table — it can never be edited or deleted
    # once a batch lands. `record_type` is producer-controlled text of no fixed
    # length, so an unbounded copy lets any caller write an arbitrarily large
    # value into permanent audit storage, once per record, through a route that
    # answers 201. Every other producer-controlled string on this path already
    # goes through `_truncate` (`title`, `abstract`); this one did not, purely
    # because it arrived by a different door.
    #
    # `MAX_RECORD_TYPE_ANNOTATION_CHARS` is far tighter than `MAX_FIELD_CHARS`
    # deliberately: this value is a TYPE, drawn from closed vocabularies whose
    # longest real member is a couple of dozen characters. Anything longer is
    # not a type a producer meant, so keeping more of it buys the reader nothing
    # and costs the ledger space it can never reclaim. The ellipsis is the
    # signal that truncation happened, so a reader is never shown a shortened
    # value as though it were the whole one.
    annotation = declared.strip()
    if len(annotation) > MAX_RECORD_TYPE_ANNOTATION_CHARS:
        annotation = annotation[:MAX_RECORD_TYPE_ANNOTATION_CHARS] + "…"
    return "other", (f"record_type={annotation}",)


def _json_record_fields(item: dict[str, Any]) -> ParsedRecord:
    title = _require_text("title", item.get("title") or item.get("name"))
    if not title:
        return ParsedRecord(raw=item, quarantine_reason="record has no title")
    authors = item.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"[;|,]", authors) if a.strip()]
    elif not isinstance(authors, list):
        raise _FieldShapeError("authors", authors, "a list or a delimited string")
    record_type, record_type_unusable = _canonical_record_type(_require_text("record_type", item.get("record_type")))

    # Identifiers. The first argument of each is the expression this parser used
    # before it read anything nested, unchanged; everything after it is a
    # best-effort fallback. Only spellings a producer actually emits are listed —
    # an alias nobody writes is a claim about a shape we have never seen.
    ids, unusable = _external_ids(item)
    doi_field, doi, doi_unusable = _identified(
        # `doi` inside `external_ids`: every discovery-engine adapter writes it.
        "doi",
        item.get("doi"),
        (("external_ids.doi", ids.get("doi")),),
        _require_text,
    )
    pmid_field, pmid, pmid_unusable = _identified(
        # `pmid`: discovery-engine's pubmed + europe_pmc adapters. `pubmed_id`:
        # its semantic_scholar adapter (lowercased from S2's `PubMed`) and its
        # consensus adapter (from OpenAlex's `ids.pmid`).
        "pmid",
        item.get("pmid"),
        (("external_ids.pmid", ids.get("pmid")), ("external_ids.pubmed_id", ids.get("pubmed_id"))),
        _identifier_text,
    )
    pmcid_field, pmcid, pmcid_unusable = _identified(
        # No discovery-engine adapter writes a PMCID today. The key is read
        # because `external_ids` is the shape ERS is now told to expect, and a
        # producer that adds one must not need a second fix here to be believed.
        "pmcid",
        item.get("pmcid"),
        (("external_ids.pmcid", ids.get("pmcid")),),
        _identifier_text,
    )
    arxiv_field, arxiv_id, arxiv_unusable = _identified(
        # `arxiv_id` inside `external_ids`: discovery-engine's arxiv +
        # semantic_scholar adapters. The flat `arxiv` alias predates this and
        # stays in the primary expression, where it has always been.
        "arxiv_id",
        item.get("arxiv_id") or item.get("arxiv"),
        (("external_ids.arxiv_id", ids.get("arxiv_id")),),
        _identifier_text,
    )
    url_field, url, url_unusable = _identified(
        # `pdf_url` is a top-level sibling of `external_ids`, not a member of it
        # — it is where `_paper_to_dict` puts the only link a paper carries. It
        # is a fallback, not a primary: nothing read it before, so nothing may
        # start failing on it.
        "url",
        item.get("url") or item.get("link"),
        (("pdf_url", item.get("pdf_url")),),
        _require_text,
    )

    return ParsedRecord(
        raw=item,
        record_type=record_type,
        title=_truncate(title),
        abstract=_truncate(_require_text("abstract", item.get("abstract") or item.get("summary"))),
        publication_year=normalize_year(
            _require_scalar(
                "publication_year", item.get("publication_year") or item.get("year") or item.get("published")
            )
        ),
        authors=[str(a) if not isinstance(a, dict) else str(a.get("name", "")) for a in authors if a],
        container_title=_require_text(
            "container_title", item.get("container_title") or item.get("journal") or item.get("venue")
        ),
        doi=doi,
        pmid=pmid,
        pmcid=normalize_pmcid(pmcid),
        arxiv_id=arxiv_id,
        url=url,
        field_sources={
            "doi": doi_field,
            "pmid": pmid_field,
            "pmcid": pmcid_field,
            "arxiv_id": arxiv_field,
            "url": url_field,
        },
        # `record_type` first: it is the record's own field, and the identifier
        # annotations that follow are about nested source keys.
        unusable_sources=(
            record_type_unusable
            + unusable
            + doi_unusable
            + pmid_unusable
            + pmcid_unusable
            + arxiv_unusable
            + url_unusable
        ),
    )


PARSERS = {
    "ris": parse_ris,
    "bibtex": parse_bibtex,
    "csv": parse_csv,
    "canonical_json": parse_canonical_json,
}
