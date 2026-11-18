"""An identifier must look like one, or it is not a dedup key.

Every test here asserts the CONSEQUENCE — `find_pairs` / `build_clusters` — not
a normalizer's return value. The harm being prevented is not "a funny string in
a column": it is two unrelated studies collapsed into one cluster, with a human
then asked which of them to keep. On an append-only ledger whose whole claim is
auditability, an accepted false merge is silent data loss that looks like a
decision.

The converse is pinned in the same file and is not optional. A change that ends
false merges by refusing real identifiers has not fixed anything — it has moved
the loss from "two studies became one" to "one study stayed two", and the second
is only cheaper because nobody signs it.

Section 3 is that converse, and it exists because the first revision of this file
did not have it. The shape gates went in with only false-merge tests, and they
turned every rendering PubMed itself emits — the article URL, `PMID: … [PubMed -
indexed for MEDLINE]`, MEDLINE's `PMID- `, `PubMed PMID: ….` — into `None`,
which the whole suite reported as green. So the rule for this file:

    a test that asserts a refusal is only admissible next to a test that asserts
    the acceptance one character away from it.

Section 3b holds those neighbours: every strip added to extract an identifier is
paired with the near-miss it must NOT admit.
"""

from __future__ import annotations

import uuid

import pytest

from domain.policies.deduplication_engine import CandidateRecord, build_clusters, find_pairs
from domain.policies.normalization import normalize_doi, normalize_pmid, normalize_text

#: Fixed ids. The engine sorts by `str(review_record_id)` everywhere, so a random
#: id would make a failure's pair ordering vary between runs.
_IDS = [uuid.UUID(f"00000000-0000-4000-8000-{i:012d}") for i in range(8)]


def _candidate(
    index: int,
    *,
    title: str | None = None,
    doi: str | None = None,
    pmid: str | None = None,
    year: int | None = None,
) -> CandidateRecord:
    """A record as the import path would have persisted it.

    Deliberately built through the real normalizers rather than by setting
    `normalized_*` directly: the defect under test lives in normalization, and a
    test that hand-wrote the normalized values would pass whatever normalization
    did.
    """
    return CandidateRecord(
        review_record_id=_IDS[index],
        review_record_version_id=_IDS[index],
        normalized_doi=normalize_doi(doi),
        normalized_pmid=normalize_pmid(pmid),
        normalized_title=normalize_text(title),
        publication_year=year,
        title=title,
        doi=doi,
        pmid=pmid,
    )


# --------------------------------------------------------------------------
# 1. A DOI column that says "not available" is not a DOI.
# --------------------------------------------------------------------------


def test_a_not_available_doi_column_does_not_merge_two_unrelated_studies() -> None:
    """Ovid, Embase and Covidence export templates write `NA`, not a blank cell.

    `parse_csv` skips a blank (`if key and value`) and keeps `NA`, so the string
    reaches the normalizer, which lowercases it into a key and hands it to the
    HIGHEST-confidence rule in the engine.
    """
    left = _candidate(0, title="Aspirin for primary prevention", doi="NA", year=2011)
    right = _candidate(1, title="Tai chi for fibromyalgia", doi="NA", year=2019)

    assert find_pairs([left, right]) == []
    assert build_clusters(find_pairs([left, right])) == []


def test_a_zero_doi_column_does_not_merge_two_unrelated_studies() -> None:
    """The other sentinel the same templates emit for an unfilled cell."""
    left = _candidate(0, title="Aspirin for primary prevention", doi="0", year=2011)
    right = _candidate(1, title="Tai chi for fibromyalgia", doi="0", year=2019)

    assert find_pairs([left, right]) == []


def test_a_slashed_not_available_is_not_a_doi_merely_because_it_has_a_slash() -> None:
    """`N/A` survives the "a DOI contains a slash" heuristic. It is still not one."""
    left = _candidate(0, title="Aspirin for primary prevention", doi="N/A", year=2011)
    right = _candidate(1, title="Tai chi for fibromyalgia", doi="n/a", year=2019)

    assert find_pairs([left, right]) == []


def test_a_trailing_period_still_hides_a_real_duplicate_and_this_is_a_known_limitation() -> None:
    """A MISSED merge that is deliberately NOT fixed, pinned so it stays visible.

    EndNote ends a DOI field with the sentence's period, so two copies of one
    paper — one from EndNote, one from the publisher — carry `10.1016/….005.`
    and `10.1016/….005` and do not match.

    An earlier revision of this branch fixed it by rstripping `.,;:`. That is
    withdrawn, because the premise is empirically false: of 20,000 DOIs sampled
    from the Crossref API, THREE end in a period and Crossref resolves them with
    it and 404s without::

        HTTP 200 10.56428/aqij.2022.1.2.    HTTP 404 10.56428/aqij.2022.1.2
        HTTP 200 10.30966/2018.riga.7.4.    HTTP 404 10.30966/2018.riga.7.4
        HTTP 200 10.21544/2446-7014.n143.p06.  HTTP 404 …n143.p06

    So the strip corrupted registered identifiers to repair unregistered ones,
    and — worse for a persisted column — it REWROTE a key rather than nulling
    it, which `trg_review_record_versions_immutable` cannot absorb.

    The real fix is not in the normalizer, which can only return one key. It is
    a matcher that groups a record under EVERY key it could carry, which is a
    `deduplication_engine` change and an `ENGINE_VERSION` bump. Until then this
    test asserts the current, lossy behaviour on purpose.
    """
    left = _candidate(0, title="Effect of X on Y", doi="10.1016/j.jclinepi.2009.06.005.", year=2009)
    right = _candidate(1, title="Effect of X on Y: a trial", doi="10.1016/j.jclinepi.2009.06.005", year=2009)

    assert find_pairs([left, right]) == []


def test_a_registered_doi_that_ends_in_a_period_keeps_it() -> None:
    """`10.30966/2018.riga.7.4.` is registered WITH the period; without it, 404.

    Two records carrying that DOI verbatim must still be one cluster. This is
    the case the withdrawn punctuation strip broke, and it is the reason the
    strip is not coming back in a "narrower" form: no offline rule separates the
    registrant's period from the citation's.
    """
    records = [
        _candidate(0, title="A survey of the Riga corpus", doi="10.30966/2018.riga.7.4.", year=2018),
        _candidate(1, title="A survey of the Riga corpus (preprint)", doi="10.30966/2018.RIGA.7.4.", year=2018),
    ]

    assert normalize_doi("10.30966/2018.riga.7.4.") == "10.30966/2018.riga.7.4."
    pairs = find_pairs(records)
    assert [p.matched_on for p in pairs] == ["normalized_doi"]
    assert len(build_clusters(pairs)) == 1


# --------------------------------------------------------------------------
# 2. A PMID must be a PubMed integer, not any digits found in the field.
# --------------------------------------------------------------------------


def test_a_pmc_accession_does_not_enter_pubmeds_number_space() -> None:
    """`PMC3312970` and PMID `3312970` are two namespaces and two papers.

    Concatenating the digits manufactures the second out of the first. This is
    the exact collision `_ris_pmid` refuses via `_NOT_PUBMED_SOURCE` — the guard
    exists, it was simply never wired into the CSV, BibTeX and JSON paths.
    """
    left = _candidate(0, title="Structural basis of protein folding", pmid="PMC3312970", year=2012)
    right = _candidate(1, title="Cognitive therapy for insomnia", pmid="3312970", year=2018)

    assert find_pairs([left, right]) == []


def test_a_pmid_is_not_assembled_out_of_prose() -> None:
    """A MEDLINE relational note carries somebody ELSE's PMID.

    `PMID: 123. Comment 999` normalized to `123999` — an identifier that names
    no record in PubMed at all, and that any other record with the same prose
    shape would also produce.
    """
    left = _candidate(0, title="Statins in the elderly", pmid="PMID: 123. Comment 999", year=2014)
    right = _candidate(1, title="Vitamin D and falls", pmid="123999", year=2020)

    assert find_pairs([left, right]) == []


def test_a_zero_pmid_column_does_not_merge_every_record_in_the_corpus() -> None:
    """The worst case, and the easiest to produce: one unfilled numeric column.

    `0` is a key like any other, so N records sharing it become ONE cluster of N
    and a reviewer is asked to elect a single survivor from the entire import.
    """
    records = [
        _candidate(0, title="Aspirin for primary prevention", pmid="0", year=2011),
        _candidate(1, title="Tai chi for fibromyalgia", pmid="0", year=2019),
        _candidate(2, title="Screening for atrial fibrillation", pmid="0", year=2021),
    ]

    assert find_pairs(records) == []
    assert build_clusters(find_pairs(records)) == []


def test_a_ten_digit_embase_accession_is_not_a_pmid() -> None:
    """`2019123456` is ten digits of pure numerals and is an Embase accession.

    Same bound `_BARE_PMID` already applies on the RIS path, for the same reason.
    """
    left = _candidate(0, title="Statins in the elderly", pmid="2019123456", year=2014)
    right = _candidate(1, title="Vitamin D and falls", pmid="2019123456", year=2020)

    assert find_pairs([left, right]) == []


def test_a_zero_padded_ordinal_is_not_a_pmid() -> None:
    """Exporters number a file `0000001..0000N`. PubMed does not zero-pad."""
    left = _candidate(0, title="Aspirin for primary prevention", pmid="0000123", year=2011)
    right = _candidate(1, title="Tai chi for fibromyalgia", pmid="0000123", year=2019)

    assert find_pairs([left, right]) == []


# --------------------------------------------------------------------------
# 3. A REFUSAL IS A LOSS TOO. The identifier has to be extracted before it is
#    judged, or the shape gate refuses the packaging instead of the value.
#
#    This section is the half that was missing when the shape gates were first
#    written, and its absence is how the regression below shipped: `PMID` had
#    no equivalent of `_DOI_PREFIXES`, so every rendering PubMed itself emits
#    returned `None`. The digits-only reading it replaced produced the CORRECT
#    key for all of them, so this was not a false merge avoided — it was a
#    cross-database merge lost, which is the merge the engine exists to find.
# --------------------------------------------------------------------------

#: One PMID — 33933206 is a real PubMed record — in every way it reaches a
#: `pmid` field on the three paths that use these normalizers (CSV `pmid` /
#: `pubmed id`, JSON `pmid`, BibTeX `pmid`).
#:
#: `import_parser`'s own module docstring calls the first of these "an
#: unambiguous PMID". None of them is ambiguous; they are one number wearing
#: PubMed's URL, PubMed's display format, MEDLINE's `.nbib` tag, the Vancouver
#: citation tail, and Excel's thousands separators.
_PUBMED_RENDERINGS = (
    "https://pubmed.ncbi.nlm.nih.gov/33933206/",
    "www.ncbi.nlm.nih.gov/pubmed/33933206",
    "PMID: 33933206 [PubMed - indexed for MEDLINE]",
    "PMID- 33933206",
    "33933206 [PubMed]",
    "PubMed PMID: 33933206.",
    "33,933,206",
)


@pytest.mark.parametrize("rendering", _PUBMED_RENDERINGS)
def test_a_pubmed_rendering_still_merges_against_a_bare_integer_twin(rendering: str) -> None:
    """Source A renders the PMID; source B writes the integer. One paper.

    The two records are given DIFFERENT titles and DIFFERENT years on purpose,
    so `normalized_title_year` cannot produce the pair and only the PMID rule
    can. A version of this test with matching title and year would have stayed
    green through the regression it exists to catch.
    """
    assert normalize_pmid(rendering) == "33933206"

    left = _candidate(0, title="Effect of X on Y", pmid=rendering, year=2021)
    right = _candidate(1, title="Effect of X on Y in adults: a trial", pmid="33933206", year=2022)

    pairs = find_pairs([left, right])
    assert [p.matched_on for p in pairs] == ["normalized_pmid"]
    assert len(build_clusters(pairs)) == 1


def test_every_pubmed_rendering_of_one_paper_collapses_into_a_single_cluster() -> None:
    """Eight sources, eight spellings of PMID 33933206, one question for a human."""
    records = [_candidate(0, title="Effect of X on Y", pmid="33933206", year=2021)] + [
        _candidate(i, title=f"Effect of X on Y ({i})", pmid=rendering, year=2021)
        for i, rendering in enumerate(_PUBMED_RENDERINGS, start=1)
    ]

    pairs = find_pairs(records)
    assert {p.matched_on for p in pairs} == {"normalized_pmid"}
    clusters = build_clusters(pairs)
    assert len(clusters) == 1
    assert clusters[0].members == tuple(_IDS[:8])


@pytest.mark.parametrize(
    "rendering",
    [
        "doi.org/10.1016/j.jclinepi.2009.06.005",
        "www.doi.org/10.1016/j.jclinepi.2009.06.005",
        "dx.doi.org/10.1016/j.jclinepi.2009.06.005",
        "https://doi.org/10.1016/j.jclinepi.2009.06.005",
        "http://dx.doi.org/10.1016/j.jclinepi.2009.06.005",
    ],
)
def test_a_resolver_url_without_a_scheme_is_still_the_doi_inside_it(rendering: str) -> None:
    """`doi.org/10.…` with no `https://` is what a de-linked spreadsheet cell holds.

    The prefix table only listed the schemed forms, so the bare-host renderings
    reached the shape gate whole and were refused — the DOI lost to the URL
    around it.
    """
    assert normalize_doi(rendering) == "10.1016/j.jclinepi.2009.06.005"

    left = _candidate(0, title="Effect of X on Y", doi=rendering, year=2009)
    right = _candidate(1, title="Effect of X on Y: a trial", doi="10.1016/j.jclinepi.2009.06.005", year=2010)

    pairs = find_pairs([left, right])
    assert [p.matched_on for p in pairs] == ["normalized_doi"]


# --------------------------------------------------------------------------
# 3b. …and the extraction above must not reopen section 2. Each strip is
#     matched here by the near-miss it is one character away from admitting.
# --------------------------------------------------------------------------


def test_the_pubmed_url_arm_does_not_admit_a_pmc_article_url() -> None:
    """`ncbi.nlm.nih.gov/pmc/…` shares a host with `ncbi.nlm.nih.gov/pubmed/…`.

    A host is only stripped when EVERYTHING after it is a PMID. PMC is a
    different namespace and the collision this whole file exists to refuse.
    """
    left = _candidate(
        0,
        title="Structural basis of protein folding",
        pmid="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3312970/",
        year=2012,
    )
    right = _candidate(1, title="Cognitive therapy for insomnia", pmid="3312970", year=2018)

    assert left.normalized_pmid is None
    assert find_pairs([left, right]) == []


def test_a_bracketed_annotation_does_not_rescue_a_pmc_accession() -> None:
    """Stripping a trailing `[…]` may only expose a candidate, never create one."""
    left = _candidate(0, title="Structural basis of protein folding", pmid="PMC3312970 [PubMed Central]", year=2012)
    right = _candidate(1, title="Cognitive therapy for insomnia", pmid="3312970", year=2018)

    assert left.normalized_pmid is None
    assert find_pairs([left, right]) == []


@pytest.mark.parametrize(
    "note",
    [
        "Comment in: Lancet. 2021 Jan 30;397(10272):341. PMID: 33357499",
        "Erratum in: N Engl J Med. 2021;384(2):199. PMID: 33357499",
    ],
)
def test_a_medline_relational_note_is_still_not_this_records_own_pmid(note: str) -> None:
    """The label now tolerates a leading `PubMed ` — and nothing else.

    `_ris_pmid`'s load-bearing rule is that a label reached through preceding
    PROSE belongs to the commentary or the erratum, not to the record holding
    the note. One anchored literal word is not prose, and the anchor is what
    keeps this refusal intact.
    """
    left = _candidate(0, title="Statins in the elderly", pmid=note, year=2014)
    right = _candidate(1, title="Vitamin D and falls", pmid="33357499", year=2020)

    assert left.normalized_pmid is None
    assert find_pairs([left, right]) == []


def test_digits_that_are_not_thousands_grouped_are_not_degrouped() -> None:
    """`33,933,206` is a formatted PMID. `12,3456` is not a number at all.

    The degrouping is strictly `\\d{1,3}(,\\d{3})+`, so it cannot decay into the
    old "concatenate every digit in the field" reading that manufactured
    `123999` out of a relational note.
    """
    left = _candidate(0, title="Statins in the elderly", pmid="12,3456", year=2014)
    right = _candidate(1, title="Vitamin D and falls", pmid="123456", year=2020)

    assert left.normalized_pmid is None
    assert find_pairs([left, right]) == []


# --------------------------------------------------------------------------
# 4. `HER2+` and `HER2-` are two cohorts. Reproduced, deliberately NOT fixed.
# --------------------------------------------------------------------------


def test_her2_plus_and_her2_minus_still_collide_and_this_is_a_known_defect() -> None:
    """A LIVE false merge that this change does not fix, pinned so it stays visible.

    `normalize_text` strips Unicode categories `Sm` and `Pd`, so `+` and `-` are
    erased and `HER2+ breast cancer` and `HER2- breast cancer` produce one key.
    With a shared year that is a merge between a positive and a negative cohort —
    ordinary oncology title syntax, not an edge case.

    It is NOT fixed here because `normalized_title` is PERSISTED on
    `review_record_versions`, whose `trg_review_record_versions_immutable` trigger
    raises `restrict_violation` on any UPDATE. Changing the function without a
    backfill would leave the same title normalizing two ways either side of the
    deploy, so a genuine duplicate straddling it would stop matching — trading
    this false merge for a missed one. See the module docstring of
    `domain.policies.normalization` for what a fix has to carry with it.

    This test asserts the CURRENT, WRONG behaviour on purpose. It turns red the
    moment someone changes `normalize_text`, which is when the backfill and the
    engine-version bump have to be on the same commit.
    """
    left = _candidate(0, title="HER2+ breast cancer outcomes", year=2020)
    right = _candidate(1, title="HER2- breast cancer outcomes", year=2020)

    pairs = find_pairs([left, right])
    assert [p.matched_on for p in pairs] == ["normalized_title_year"]
    assert len(build_clusters(pairs)) == 1


# --------------------------------------------------------------------------
# The negative control. Real identifiers must still merge real duplicates.
# --------------------------------------------------------------------------


def test_a_valid_doi_still_merges_a_real_duplicate_in_every_arriving_form() -> None:
    """One paper, four exporters, four spellings of the same DOI, one cluster."""
    records = [
        _candidate(0, title="Effect of X on Y", doi="10.1016/j.jclinepi.2009.06.005", year=2009),
        _candidate(1, title="Effect of X on Y", doi="https://doi.org/10.1016/j.jclinepi.2009.06.005", year=2009),
        _candidate(2, title="Effect of X on Y", doi="doi:10.1016/J.JCLINEPI.2009.06.005", year=2009),
        _candidate(3, title="Effect of X on Y", doi=" www.doi.org/10.1016/j.jclinepi.2009.06.005 ", year=2009),
    ]

    pairs = find_pairs(records)
    assert {p.matched_on for p in pairs} == {"normalized_doi"}
    clusters = build_clusters(pairs)
    assert len(clusters) == 1
    assert clusters[0].members == tuple(_IDS[:4])


def test_a_valid_pmid_still_merges_a_real_duplicate_in_every_arriving_form() -> None:
    """Same paper, and the label/period variants the docstring promises to accept."""
    records = [
        _candidate(0, title="Effect of X on Y", pmid="33933206", year=2021),
        _candidate(1, title="Effect of X on Y", pmid="PMID:33933206", year=2021),
        _candidate(2, title="Effect of X on Y", pmid="PMID: 33933206.", year=2021),
    ]

    pairs = find_pairs(records)
    assert {p.matched_on for p in pairs} == {"normalized_pmid"}
    clusters = build_clusters(pairs)
    assert len(clusters) == 1
    assert clusters[0].members == tuple(_IDS[:3])


def test_a_short_historical_pmid_is_still_an_identifier() -> None:
    """PMID 1 exists. A floor of seven digits would discard PubMed's first decade.

    Rejecting these would be the same class of error as accepting `0`: a bound
    chosen for convenience rather than for what the namespace actually contains.
    """
    records = [
        _candidate(0, title="Formate assay in body fluids", pmid="1", year=1975),
        _candidate(1, title="Formate assay in body fluids: application", pmid="PMID: 1", year=1975),
    ]

    pairs = find_pairs(records)
    assert [p.matched_on for p in pairs] == ["normalized_pmid"]


def test_the_engine_still_skips_a_null_key_rather_than_grouping_on_it() -> None:
    """The refusal above is only safe because `None` means "no key", not "a key".

    `_group_by` drops `None`; if it ever grouped nulls, every value this change
    now refuses would collapse into one cluster instead of none — a strictly
    worse outcome than the defect being fixed.
    """
    records = [
        _candidate(0, title="Aspirin for primary prevention", doi="NA", pmid="0", year=2011),
        _candidate(1, title="Tai chi for fibromyalgia", doi="NA", pmid="0", year=2019),
    ]

    assert all(r.normalized_doi is None and r.normalized_pmid is None for r in records)
    assert find_pairs(records) == []
