"""The deterministic baseline duplicate engine — `ER-003.2`.

No ML, no thresholds, no scoring model. Three exact rules over the persisted
normalized fields, applied in descending confidence:

1. **normalized DOI** — a shared DOI is the same work. Highest confidence
   available in bibliographic data.
2. **normalized PMID** — same, within PubMed's namespace.
3. **normalized title + publication year** — the fallback, and the only rule that
   can be wrong. It requires BOTH, so two different studies sharing a generic
   title in different years do not collapse; and a record with no year cannot
   match by this rule at all, because a null year matching another null year
   would cluster every untitled-year record in the corpus into one.

The engine **proposes**. It never writes a resolution: `01-…:109-110` gives
machines the authority to "calculate, rank and advise" and reserves the decision.
A cluster is a question put to a human, and `duplicate_resolutions` is the only
place an answer is recorded.

Determinism is the whole contract (`01-…:454`): the same corpus version, engine
version and config must produce the same clusters. So every ordering here is
explicit, the union-find is seeded in sorted order, and the cluster key is
derived from the sorted member ids rather than from insertion order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

ENGINE_ID = "exact-identifier-baseline"
ENGINE_VERSION = "1.0.0"

#: Which rules run, and in what order. Config rather than constants because the
#: run freezes it into `input_digest`: a later run with a different rule set is
#: visibly a different run rather than an unexplained change in output.
DEFAULT_CONFIG: dict = {
    "rules": ["normalized_doi", "normalized_pmid", "normalized_title_year"],
    "engine_id": ENGINE_ID,
    "engine_version": ENGINE_VERSION,
}


@dataclass(frozen=True)
class CandidateRecord:
    """The comparable projection of one record version."""

    review_record_id: UUID
    review_record_version_id: UUID
    normalized_doi: str | None
    normalized_pmid: str | None
    normalized_title: str | None
    publication_year: int | None
    #: The SOURCE fields behind the normalized ones. Optional because `_candidates`
    #: reads only what the matcher compares; `canonical_presentation` needs them
    #: because they, not the normalized forms, are what survivorship may choose.
    title: str | None = None
    doi: str | None = None
    pmid: str | None = None


@dataclass(frozen=True)
class Pair:
    left: UUID
    right: UUID
    matched_on: str

    @staticmethod
    def of(a: UUID, b: UUID, matched_on: str) -> Pair:
        """Ordered, so (A,B) and (B,A) are one fact — as the table requires."""
        lo, hi = sorted((a, b), key=str)
        return Pair(left=lo, right=hi, matched_on=matched_on)


@dataclass(frozen=True)
class Cluster:
    cluster_key: str
    members: tuple[UUID, ...]


def input_digest(corpus_version_id: UUID, config: dict) -> str:
    """Freeze `(corpus version, engine, config)` — `ER-003.1`.

    RFC 8785 through the published contract wheel, so this digest means the same
    thing as every other canonical hash in the fleet. A `json.dumps` here would
    be a second, quietly different definition.
    """
    from research_contracts.canonical.engine import canonical_sha256

    return canonical_sha256(
        {
            "corpus_version_id": str(corpus_version_id),
            "engine_id": config.get("engine_id", ENGINE_ID),
            "engine_version": config.get("engine_version", ENGINE_VERSION),
            "config": config,
        }
    )


def _group_by(candidates: Sequence[CandidateRecord], key) -> dict:  # noqa: ANN001
    groups: dict = {}
    for candidate in candidates:
        value = key(candidate)
        if value is None:
            continue
        groups.setdefault(value, []).append(candidate)
    return groups


def find_pairs(candidates: Sequence[CandidateRecord], config: dict | None = None) -> list[Pair]:
    """Every matching pair, with the rule that produced it.

    A pair can be found by more than one rule; each is emitted, and the caller
    keeps the highest-confidence one. Reporting only the first would lose the
    fact that two records agree on both DOI and title, which is what makes a
    merge easy to accept.
    """
    config = config or DEFAULT_CONFIG
    rules = config.get("rules", DEFAULT_CONFIG["rules"])
    ordered = sorted(candidates, key=lambda c: str(c.review_record_id))
    pairs: dict[tuple[UUID, UUID], Pair] = {}

    def emit(group: Iterable[CandidateRecord], matched_on: str) -> None:
        members = sorted(group, key=lambda c: str(c.review_record_id))
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                pair = Pair.of(left.review_record_id, right.review_record_id, matched_on)
                # First rule wins, and the rules run in descending confidence,
                # so `matched_on` always names the strongest evidence.
                pairs.setdefault((pair.left, pair.right), pair)

    if "normalized_doi" in rules:
        for group in _group_by(ordered, lambda c: c.normalized_doi).values():
            if len(group) > 1:
                emit(group, "normalized_doi")

    if "normalized_pmid" in rules:
        for group in _group_by(ordered, lambda c: c.normalized_pmid).values():
            if len(group) > 1:
                emit(group, "normalized_pmid")

    if "normalized_title_year" in rules:
        # BOTH required. A null year cannot participate — see the module
        # docstring; matching null-to-null would cluster the whole corpus.
        for group in _group_by(
            ordered,
            lambda c: (
                (c.normalized_title, c.publication_year)
                if c.normalized_title and c.publication_year is not None
                else None
            ),
        ).values():
            if len(group) > 1:
                emit(group, "normalized_title_year")

    return sorted(pairs.values(), key=lambda p: (str(p.left), str(p.right)))


def build_clusters(pairs: Sequence[Pair]) -> list[Cluster]:
    """Transitive closure of the pairs — union-find, seeded deterministically.

    Transitive because duplicate triples are real: A and B share a DOI, B and C
    share a title+year, and all three are one work. Leaving them as two pairs
    would ask the reviewer to resolve the same duplicate twice and let the two
    answers disagree.
    """
    parent: dict[UUID, UUID] = {}

    def find(x: UUID) -> UUID:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: UUID, b: UUID) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Union by the string form, so the representative of a set is a function
        # of its members rather than of the order the pairs arrived in.
        lo, hi = sorted((ra, rb), key=str)
        parent[hi] = lo

    for pair in sorted(pairs, key=lambda p: (str(p.left), str(p.right))):
        union(pair.left, pair.right)

    groups: dict[UUID, list[UUID]] = {}
    for node in sorted(parent, key=str):
        groups.setdefault(find(node), []).append(node)

    clusters: list[Cluster] = []
    for members in groups.values():
        ordered = tuple(sorted(members, key=str))
        if len(ordered) < 2:
            continue
        key = hashlib.sha256("\n".join(str(m) for m in ordered).encode("utf-8")).hexdigest()[:32]
        clusters.append(Cluster(cluster_key=key, members=ordered))

    return sorted(clusters, key=lambda c: c.cluster_key)


@dataclass(frozen=True)
class MergedGroup:
    """One cluster whose CURRENT resolution is a merge."""

    cluster_id: UUID
    members: frozenset[UUID]
    survivor: UUID


def components_without_survivor(merges: Sequence[MergedGroup]) -> list[tuple[frozenset[UUID], frozenset[UUID]]]:
    """Merge groups that would leave a real study in no bucket at all.

    A merge removes `members - {survivor}` from the current record set. `flow`
    unions those removals across every merged cluster in the review, and nothing
    checked that the union leaves anything behind. Two runs over the same pair,
    merged with different survivors — cluster 1 keeps A, cluster 2 keeps B — union
    to `{A, B}`. The study is not screened, not excluded, not in the inclusion
    set, and no row anywhere says it left. The export seals and `:verify` says
    verified.

    **The invariant is over connected components, not over single clusters.**
    "A cluster's survivor is never removed" is the tempting spelling and it is
    wrong: if A~B keeps A and a later run finds A~C and keeps C, then A — cluster
    one's survivor — is removed by cluster two, and that is CORRECT. The three
    records are transitively one work and C is the survivor of all of them. The
    per-cluster rule would refuse an ordinary re-run; the component rule refuses
    only when a work has no representative left.

    Components are taken over the union of members, mirroring `build_clusters`:
    two clusters sharing any record are one work, for the same transitive reason
    that makes a duplicate triple one cluster.

    Returns `(component_members, cluster_ids)` for each emptied component, so the
    caller can name the resolutions that conflict. Empty list means the corpus
    conserves every study. Pure: no session, no ordering assumptions, and
    deterministic — sorted seeding as everywhere else in this module.
    """
    parent: dict[UUID, UUID] = {}

    def find(x: UUID) -> UUID:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: UUID, b: UUID) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        lo, hi = sorted((ra, rb), key=str)
        parent[hi] = lo

    ordered = sorted(merges, key=lambda m: str(m.cluster_id))
    for merge in ordered:
        members = sorted(merge.members, key=str)
        for member in members[1:]:
            union(members[0], member)

    # `removed`, exactly as `flow._collect` unions it. NOT "the set of records
    # some merge named survivor" — a record can be cluster one's survivor and
    # cluster two's loser, which is the whole defect: both A and B are survivors
    # of SOMETHING while both are removed.
    removed = {member for merge in ordered for member in merge.members if member != merge.survivor}

    components: dict[UUID, set[UUID]] = {}
    clusters_of: dict[UUID, set[UUID]] = {}
    for merge in ordered:
        root = find(min(merge.members, key=str))
        components.setdefault(root, set()).update(merge.members)
        clusters_of.setdefault(root, set()).add(merge.cluster_id)

    emptied = []
    for root, members in components.items():
        if not (members - removed):
            emptied.append((frozenset(members), frozenset(clusters_of[root])))
    return sorted(emptied, key=lambda e: sorted(str(m) for m in e[0]))


def canonical_presentation(members: Sequence[CandidateRecord]) -> dict:
    """`ER-003.4` — the canonical view a reviewer resolves against.

    Field-by-field, with every distinct value and the record version it came
    from. This is what makes survivorship a *choice between named sources*
    rather than a merge that silently prefers whichever row was scanned first.
    """
    # The normalized fields are the DEDUP BASELINE — why these records clustered.
    # The source fields are what a reviewer may actually choose between: they are
    # `imports.SURVIVABLE_FIELDS`, and the normalized ones are derived and
    # recomputed by `apply_survivorship` rather than copied.
    #
    # Presenting only the normalized set made this route disagree with the
    # vocabulary survivorship accepts on three of its four fields. Nothing broke,
    # because survivorship had no effect at all and no caller sent it — but a
    # reviewer choosing "that record's title" out of `normalized_title` would now
    # be refused by the closed vocabulary, having been shown nothing else to pick.
    fields = (
        "normalized_doi",
        "normalized_pmid",
        "normalized_title",
        "title",
        "doi",
        "pmid",
        "publication_year",
    )
    presentation: dict[str, list[dict]] = {}
    for field in fields:
        seen: dict[str, dict] = {}
        for member in sorted(members, key=lambda m: str(m.review_record_id)):
            value = getattr(member, field)
            if value is None:
                continue
            entry = seen.setdefault(json.dumps(value, sort_keys=True), {"value": value, "sources": []})
            entry["sources"].append(str(member.review_record_version_id))
        presentation[field] = list(seen.values())
    return presentation
