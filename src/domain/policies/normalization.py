"""Field normalization — the deterministic half of deduplication.

`ER-003.2` requires the baseline engine to be deterministic and free of ML, so
every comparison it makes is a comparison of values produced here. Normalization
is therefore persisted onto `review_record_versions` rather than computed at
compare time: a run's `input_digest` (`ER-003.1`) has to be a function of stored
bytes, and a normalizer that changed under a running comparison would make two
runs over identical input disagree while both claimed the same digest.

The rule, applied in this order and no other:

1. **NFKC** — so `ﬁ` and `fi`, and full-width digits and ASCII digits, are the
   same characters before anything else looks at them.
2. **casefold** — not `lower()`. `lower()` leaves `ß` alone; `casefold()` maps it
   to `ss`, which is what a German-language title needs.
3. **punctuation and symbols stripped** to spaces, by Unicode category, not by an
   ASCII list — a title from a non-Latin script must normalize the same way.
4. **whitespace collapsed** and trimmed.

## Identifier normalizers refuse rather than guess

`normalize_text` produces a *comparison key* out of prose, so any input has some
answer. The identifier normalizers do not: `normalized_doi` and `normalized_pmid`
are the two highest-confidence rules in
:func:`domain.policies.deduplication_engine.find_pairs`, and a key emitted for a
value that is not that identifier does not merely mislabel one record — it MERGES
two unrelated studies and asks a reviewer to elect a survivor between them. On an
append-only ledger the accepted merge is indistinguishable from a decision.

So each identifier normalizer returns ``None`` — "no key", which `_group_by`
skips — for anything that cannot be that identifier.

**A refusal is not free, and this module used to claim it nearly was.** The
sentence that stood here said a refusal "costs at most a fall-through to the next
rule". For a record with no `publication_year` there is no next rule:
`normalized_title_year` requires BOTH, and
:mod:`domain.policies.deduplication_engine` says why in its own docstring — "a
record with no year cannot match by this rule at all, because a null year
matching another null year would cluster every untitled-year record in the
corpus into one". Preprint servers, grey literature and half the CSV exports in
circulation ship records with no year. For those, refusing the DOI and the PMID
does not demote the pair to a weaker rule; it deletes the pair. So the shapes
below are drawn to exclude what the identifier CANNOT be, not to admit only what
it usually looks like, and every bound is justified where it is defined.

This matters more, not less, once discovery-engine's nested `external_ids` are
actually read: today those identifiers arrive ``NULL`` and the missing shape check
is latent. The moment they arrive populated, an unvalidated normalizer turns a
fixed import path into a false-merge generator.

## Extract the identifier first, then judge it

A shape gate run against the raw cell refuses the packaging, not the value. Both
normalizers therefore strip what they can NAME — a resolver host, the
identifier's own label, a trailing status annotation, a spreadsheet's thousands
separators — and only then apply their anchored shape.

This half was missing for PMID and it cost the merge the engine exists to find.
`normalize_pmid` had no equivalent of `_DOI_PREFIXES`, so every rendering PubMed
itself emits returned ``None``: the article URL, the `PMID: 33933206 [PubMed -
indexed for MEDLINE]` summary line, MEDLINE's `PMID- 33933206`, the trailing
`PubMed PMID: 33933206.` citation form, and the `33,933,206` a spreadsheet
writes the moment anyone opens a numeric `pmid` column in Excel. Every one of
those is an unambiguous PMID — `imports.import_parser` says exactly that about
the first of them — and the digits-only reading that preceded the shape gate
produced the CORRECT key for all of them.

That makes this direction categorically worse than the DOI sentinels. `NA` as a
DOI produced a garbage key that could only match an identically garbled twin; a
PubMed URL produced the canonical PMID, so it merged ACROSS databases — one
source rendering the identifier as a link, another as a bare integer. Refusing it
is not a false merge avoided, it is a real merge lost.

What is stripped is enumerated, never inferred. The shape is still the gate, and
a stripper that chewed through prose would hand the gate a value it was never
shown — which is precisely how `PMID: 123. Comment 999` once became `123999`.

## What this change does to corpora that ALREADY exist

`normalized_doi` and `normalized_pmid` are persisted onto
`review_record_versions` by the same path as `normalized_title` and under the
same `trg_review_record_versions_immutable` trigger. The deferral of
`normalize_text` below therefore does not rest on "identifiers are different";
it rests on a narrower and checkable property, so state it exactly:

* **Rows already written keep their old keys.** An in-place backfill is refused
  — the trigger raises `restrict_violation` on UPDATE, as the owning role with
  the tenant GUC set. An existing corpus KEEPS the false merges this change
  prevents. Nothing here repairs history, and no code in this commit pretends to.
* **A fresh corpus recomputes.** A new import writes a new `corpus_version_id`,
  which changes `input_digest`, so `create_run` computes instead of replaying.
  Every corpus imported after this deploys gets the corrected keys.
* **Only a REWRITE can break a pair that used to match, and the rewrite surface
  here is one nameable class.** A key that becomes ``None`` cannot break a pair:
  a ``None`` never created one. Measured against the previous implementation,
  `normalize_pmid` now returns either its old value or ``None`` for every input,
  and so does `normalize_doi` — with the single exception of a bare-host
  resolver URL (`doi.org/10.…`, `www.doi.org/10.…`, `dx.doi.org/10.…`), whose
  old "key" was the entire URL and is now the DOI inside it. Two such rows
  straddling the deploy stop matching. They are rows whose stored key was never
  a DOI, and the new one is what every other rendering of that DOI already
  produced, so the class shrinks rather than grows.

A transform that rewrote *canonical* identifiers would not be bounded like that,
which is why no trailing-punctuation strip survives in :func:`normalize_doi`.

## `normalize_text` strips `+` and `-`, and that is a LIVE defect left unfixed

Categories `Sm` and `Pd` cover `+` and `-`, so::

    normalize_text("HER2+ breast cancer") == normalize_text("HER2- breast cancer")

Both become `her2 breast cancer`. Share a publication year and the third rule
merges a positive and a negative cohort — ordinary oncology title syntax, and the
same erasure hits `CD4+`/`CD4-`, `ER+`/`ER-`, `BRCA1+/-` and every `pre-`/`post-`
distinction that survives only in a hyphen.

It is NOT fixed here, and the reason is persistence rather than doubt about the
defect. `normalized_title` is written onto `review_record_versions` at import
(`application.services.imports._record_version`) and never recomputed at compare
time — deliberately, so a run's `input_digest` is a function of stored bytes.
That table carries `trg_review_record_versions_immutable`, a `BEFORE UPDATE OR
DELETE` trigger that raises `restrict_violation` with "a correction is a new
version, never an edit". Changing this function alone would therefore leave one
title normalizing two ways either side of the deploy, and a genuine duplicate pair
straddling that line would silently STOP matching — trading a false merge for a
missed one, which is the same loss wearing a quieter face.

The identifier normalizers sit under the SAME trigger and the same persistence,
so the difference is not the column — it is that `normalize_text` rewrites the
key of every title in the corpus, while the identifier changes above either null
a key or rewrite one narrow, nameable class of them. "What this change does to
corpora that already exist", above, states that exactly and does not round it up.
Every title is a straddle; a bare-host resolver URL is a straddle you can count.

A fix has to arrive as one commit carrying all four of:

1. a Python data migration (not SQL — Postgres has no `str.casefold` and no
   Unicode-category walk) that recomputes `normalize_text(title)` for every row
   of `review_record_versions`, across all tenants with RLS bypassed;
2. `ALTER TABLE review_record_versions DISABLE TRIGGER
   trg_review_record_versions_immutable` around that update, and a re-enable that
   is not conditional on the update succeeding;
3. a bump of `deduplication_engine.ENGINE_VERSION`, because `input_digest` hashes
   `(corpus_version_id, engine_id, engine_version, config)` and NOT the
   normalization ruleset — without the bump, `create_run` sees a matching digest
   and REPLAYS the stale clusters, so the corrected ones are never computed;
4. the `Sm`/`Pd` carve-out itself, which must keep stripping `–`/`—` (`Pd` also
   covers en and em dashes, which are typographic, not semantic) and strip `±`
   and `×` only if `Sm` is narrowed rather than dropped.

`tests/unit/test_identifier_shape_gates.py` pins the current wrong behaviour so
that whoever changes this function is forced to notice the other three.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")

#: Unicode general categories that are punctuation or symbols. Category prefixes
#: rather than a character list, so this stays correct outside ASCII.
_STRIP_CATEGORIES = ("P", "S")

#: A URL scheme and the `www.` a link acquires from being pasted, in front of
#: EITHER identifier's resolver host. Factored out of the two prefix tables so
#: neither has to enumerate the scheme x `www.` permutations of every host it
#: knows — the omissions are what let `doi.org/10.1000/x` and
#: `www.ncbi.nlm.nih.gov/pubmed/33933206` through unrecognised.
#:
#: Unconditional and harmless: no identifier accepted below can begin with a
#: scheme, and what remains after the strip still has to pass an anchored shape.
_URL_NOISE = re.compile(r"^(?:https?://)?(?:www\.)?")

#: The resolver hosts and labels a DOI arrives wearing, stripped so
#: `https://doi.org/10.1000/x`, `doi.org/10.1000/x`, `dx.doi.org/10.1000/x`,
#: `doi:10.1000/x` and `10.1000/x` are one value rather than five. Scheme and
#: `www.` are already gone by :data:`_URL_NOISE`, so only the hosts appear here.
#:
#: The bare-host forms are not hypothetical tidiness: a `doi.org/…` with no
#: scheme is what a spreadsheet's auto-linked cell and most "copy DOI" buttons
#: produce, and without them the shape gate refuses the DOI rather than the
#: prefix.
_DOI_PREFIXES = (
    "doi.org/",
    "dx.doi.org/",
    "doi:",
    "doi ",
)

#: What a DOI *is*, per the DOI Handbook: the literal `10.`, a registrant code of
#: 4-9 digits (with the rare dotted sub-prefix, `10.1000.10/…`), a `/`, and a
#: non-empty suffix. Whitespace in the suffix is legal-but-discouraged and in
#: practice means the field also holds trailing prose, so it is refused.
#:
#: This one check is what rejects every sentinel an export template writes into
#: an unfilled DOI cell — `NA`, `N/A`, `0`, `-`, `none` — without enumerating
#: them. `parse_csv` skips a BLANK cell (`if key and value`) but not a filled one
#: saying "nothing", and Ovid, Embase and Covidence all fill it.
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}(?:\.\d+)*/\S+$")

#: The two PubMed article-URL forms, stripped so the PMID inside them survives
#: the shape gate. `_URL_NOISE` has already removed the scheme and `www.`; the
#: trailing `/` of `pubmed.ncbi.nlm.nih.gov/33933206/` goes with the trailing
#: period, in :func:`normalize_pmid`.
#:
#: `import_parser`'s own module docstring states the warrant: "A
#: `pubmed.ncbi.nlm.nih.gov/<pmid>/` URL in a RIS `UR` tag is an unambiguous
#: PMID." It is no less unambiguous in a CSV column headed `pmid`, a JSON `pmid`
#: key or a BibTeX `pmid` field — the three paths that reach this function.
#:
#: Deliberately NOT here: `ncbi.nlm.nih.gov/pmc/…`, `europepmc.org/…` and any
#: other host. A host is only listed when everything after it is a PMID and
#: nothing else. PMC is the collision this module exists to refuse.
_PMID_PREFIXES = (
    "pubmed.ncbi.nlm.nih.gov/",
    "ncbi.nlm.nih.gov/pubmed/",
)

#: The bracketed status PubMed's own "Summary (text)" and "PubMed" display
#: formats append: `PMID: 33933206 [PubMed - indexed for MEDLINE]`, `33933206
#: [PubMed]`, `[PubMed - as supplied by publisher]`. It annotates the record's
#: indexing state and is never part of the number.
#:
#: One trailing group only, and non-nesting (`[^\[\]]*`), so this cannot eat an
#: arbitrary tail. It can only turn a refusal into a bare candidate that the
#: anchored shape then judges: `PMC3312970 [PubMed Central]` becomes
#: `pmc3312970`, which is still refused.
_PMID_STATUS_ANNOTATION = re.compile(r"\s*\[[^\[\]]*\]\s*$")

#: A number written with thousands separators, which is what a spreadsheet does
#: to a numeric `pmid` column the moment the CSV is opened in Excel or Numbers
#: and saved again: `33933206` comes back as `33,933,206`.
#:
#: Strictly grouped — `\d{1,3}` then one or more `,\d{3}` — and bounded by word
#: boundaries, so it rewrites `33,933,206` and leaves `12,3456` and every prose
#: comma alone. This is emphatically NOT the old "concatenate every digit in the
#: field" reading: `PMID: 123. Comment 999` contains no group and is untouched,
#: and the anchored shape still has the final say.
_THOUSANDS_SEPARATED = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")

#: What a PMID is: a positive PubMed integer, optionally wearing its own label.
#:
#: **Bounds, and why these.** PubMed identifiers are sequential integers assigned
#: from 1 (PMID 1 is a 1975 record and is still resolvable), and the highest
#: assigned is around 4.1e7. So:
#:
#: * **No upper bound below 8 digits.** 8 digits reaches 99,999,999 — decades of
#:   headroom at the current ~1.5M/year — while REFUSING the 9- and 10-digit
#:   neighbours that would otherwise pass: Embase accessions are ten digits of
#:   pure numerals (`2019123456`) and CINAHL's are nine. This is the same bound
#:   `imports.import_parser._BARE_PMID` already applies on the RIS path, and it
#:   is deliberately the same number: two different answers to "how long is a
#:   PMID" in one service is a defect waiting for whichever path is read second.
#: * **No lower bound.** A seven-digit floor would be convenient and would
#:   discard PubMed's entire first decade. `_UNCORROBORATED_PMID` in the RIS path
#:   DOES impose that floor, and the difference is exactly the corroboration:
#:   there, the value comes out of a generic `AN` accession tag in a record that
#:   declared no source, so the SHAPE has to stand in for a missing namespace.
#:   Here the field is `pmid` — a CSV column headed `pmid`/`pubmed id`, a JSON
#:   `pmid` key, a BibTeX `pmid` field. The namespace is declared, so the shape
#:   only has to exclude what a PMID cannot be.
#: * **Leading digit `[1-9]`.** This is what a PMID cannot be. `0` is not an
#:   assigned identifier, it is the sentinel an export template writes into an
#:   unfilled numeric column — and as a key it merges EVERY record in the import
#:   into one cluster. Zero-padding (`0000123`) is likewise an exporter's ordinal
#:   counter, never PubMed's rendering.
#:
#: **Anchored at both ends**, which is the other half of the guard. The old
#: implementation concatenated every digit anywhere in the field, so
#: `PMC3312970` — a PubMed Central accession, a DIFFERENT namespace — was
#: manufactured into the PMID `3312970`, and a MEDLINE relational note
#: (`PMID: 123. Comment 999`) was assembled into `123999`, an identifier that
#: names nothing. `_ris_pmid` refuses both, for reasons written out at length in
#: its docstring; anchoring here is that same refusal, applied to the CSV,
#: BibTeX and JSON paths it was never wired into.
#:
#: **The label, and how far it may extend.** `pmid`, then optionally `:`, `#` or
#: `-` — MEDLINE's own `.nbib` tag format is `PMID- 33933206`, with a hyphen and
#: no colon. The label may itself be preceded by the single literal word
#: `pubmed`, which is the Vancouver citation tail (`PubMed PMID: 33933206.`)
#: that NLM's own author instructions specify.
#:
#: `pubmed` is a LITERAL, anchored, and one word. That distinction is the whole
#: guard `_ris_pmid` is built on: a label reached through arbitrary preceding
#: prose belongs to a `Comment in:` / `Erratum in:` note and names ANOTHER
#: paper. `^(?:pubmed\s+)?` cannot match such a note — `comment in: … pmid:
#: 33357499` still fails at position 0 — while `.search()` or a `.*` prefix
#: would match it, which is exactly the defect this constant was written to end.
_PMID_SHAPE = re.compile(r"^(?:pubmed\s+)?(?:pmid\s*[:#-]?\s*)?([1-9]\d{0,7})$")


def normalize_text(value: str | None) -> str | None:
    """NFKC + casefold + strip punctuation/symbols + collapse whitespace."""
    if value is None:
        return None
    folded = unicodedata.normalize("NFKC", value).casefold()
    stripped = "".join(" " if unicodedata.category(ch).startswith(_STRIP_CATEGORIES) else ch for ch in folded)
    collapsed = _WHITESPACE.sub(" ", stripped).strip()
    return collapsed or None


def normalize_doi(value: str | None) -> str | None:
    """Lowercase, prefix-stripped DOI — or ``None`` if it is not a DOI.

    Punctuation is NOT stripped, at either end or in the interior. A DOI's
    slashes and dots are its structure — `10.1000/182` and `101000182` are not
    the same identifier — which is why DOI has its own normalizer rather than
    reusing :func:`normalize_text`.

    **Trailing punctuation is not stripped either, and that reverses an earlier
    version of this function.** The premise of the strip was that a terminating
    period belongs to the citation rather than to the identifier. Measured
    against 20,000 DOIs sampled from the Crossref API, three END in a period,
    and Crossref resolves them WITH it::

        HTTP 200  10.56428/aqij.2022.1.2.     HTTP 404  10.56428/aqij.2022.1.2
        HTTP 200  10.30966/2018.riga.7.4.     HTTP 404  10.30966/2018.riga.7.4
        HTTP 200  10.1136/qhc.0100132..       (both forms registered)

    A trailing `.` is therefore sometimes the sentence's and sometimes the
    registrant's, and nothing available to a deterministic offline normalizer
    separates the two. Stripping it corrupts a registered identifier in order to
    repair an unregistered one — and it does so by REWRITING a persisted key
    rather than nulling it, which is the one direction the immutability of
    `review_record_versions` cannot absorb: two rows with the identical raw DOI,
    one imported either side of the deploy, stop pairing.

    The cost of not stripping is real and is left standing rather than traded
    for that. EndNote writes the citation's period into the DOI field, so
    `10.1016/j.jclinepi.2009.06.005.` and the same DOI without it are two keys
    and do not merge;
    `test_a_trailing_period_still_hides_a_real_duplicate_and_this_is_a_known_limitation`
    pins it. The fix does not live in this function: a normalizer returning one
    key cannot express "either of these two". It lives in a matcher that groups
    a record under every key it could carry, which is an engine change and an
    `ENGINE_VERSION` bump, not a rewrite of a stored value.

    The shape check (:data:`_DOI_SHAPE`) is the point of this function. Without
    it, an export template's `NA` became the key `na`, and two unrelated studies
    matched on `normalized_doi` — the strongest evidence the engine can report.
    A value that is not a DOI yields no key at all, and the record falls through
    to the PMID and title+year rules like any record that simply has no DOI.
    """
    if value is None:
        return None
    candidate = _URL_NOISE.sub("", unicodedata.normalize("NFKC", value).strip().casefold(), count=1)
    for prefix in _DOI_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.strip()
    return candidate if _DOI_SHAPE.match(candidate) else None


def normalize_pmid(value: str | None) -> str | None:
    """A PubMed identifier, or ``None``.

    The same number reaches this function in every way PubMed and its consumers
    render it, and all of these are ONE record::

        33933206                                        a bare integer
        PMID:33933206  PMID: 33933206  PMID- 33933206    label forms, incl. .nbib
        PubMed PMID: 33933206.                           the Vancouver citation tail
        PMID: 33933206 [PubMed - indexed for MEDLINE]    PubMed's own text display
        33933206 [PubMed]                                the short display
        https://pubmed.ncbi.nlm.nih.gov/33933206/        the article URL
        www.ncbi.nlm.nih.gov/pubmed/33933206             the legacy article URL
        33,933,206                                       a spreadsheet's doing

    Each is unpacked by a named, enumerated strip — :data:`_PMID_PREFIXES`,
    :data:`_PMID_STATUS_ANNOTATION`, :data:`_THOUSANDS_SEPARATED` and the label
    in :data:`_PMID_SHAPE` — and only the number that survives is judged.

    Extraction before judgement is not decoration here, it is the difference
    between a merge and a lost pair. Two sources hold the same paper; one
    renders the identifier as a link and one as an integer. That is the
    cross-database duplicate the engine exists to find, and a shape gate applied
    to the raw cell returns ``None`` for the link and finds nothing.

    Anything NOT on that list is still refused rather than reduced to its
    digits. The digits-only reading turned a PMC accession into a PubMed one,
    assembled a PMID out of a relational note's prose, and accepted `0` — which,
    being a perfectly good dictionary key, merged an entire import into a single
    cluster. Every strip above is anchored and bounded precisely so that it
    cannot reopen those: the refusals are tested alongside the acceptances in
    `tests/unit/test_identifier_shape_gates.py`, and neither list is optional.
    The bounds and the reasoning behind each of them are on :data:`_PMID_SHAPE`.
    """
    if value is None:
        return None
    candidate = _URL_NOISE.sub("", unicodedata.normalize("NFKC", value).strip().casefold(), count=1)
    for prefix in _PMID_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = _PMID_STATUS_ANNOTATION.sub("", candidate)
    # A PMID is decimal digits, so a trailing `.` or `/` cannot belong to it —
    # unlike a DOI suffix, where both are structural. See :func:`normalize_doi`.
    candidate = candidate.strip().rstrip("./").strip()
    candidate = _THOUSANDS_SEPARATED.sub(lambda m: m.group(0).replace(",", ""), candidate)
    match = _PMID_SHAPE.match(candidate)
    return match.group(1) if match else None


def normalize_pmcid(value: str | None) -> str | None:
    """`PMC` + digits, uppercased."""
    if value is None:
        return None
    digits = "".join(ch for ch in unicodedata.normalize("NFKC", value) if ch.isdigit())
    return f"PMC{digits}" if digits else None


def normalize_year(value: object) -> int | None:
    """A four-digit year, or nothing.

    Bibliographic sources supply `2019`, `"2019"`, `"2019 Mar"`, `"2019-03-14"`
    and `"c2019"`. Anything that does not yield a plausible four-digit year
    yields ``None`` rather than a guess: a wrong year is a false dedup key, and a
    missing one merely falls through to the next rule.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 2999 else None
    match = re.search(r"(1[0-9]{3}|2[0-9]{3})", str(value))
    return int(match.group(1)) if match else None
