"""`components_without_survivor` — the invariant, with no database in the way.

The integration test proves the endpoint refuses. This proves the RULE, which is
the part that is easy to get subtly wrong, and one of these cases is a defect I
wrote and caught before it ran:
:func:`test_a_record_can_be_survivor_of_one_cluster_and_loser_of_another`.

My first draft asked "does this component contain any record that some merge
named survivor?". For the headline defect — cluster one keeps A, cluster two
keeps B, same pair — the answer is *yes, both of them*, while both are in fact
removed. The rule has to be stated over the REMOVAL union, exactly as
`flow._collect` builds it, not over the set of chosen survivors.
"""

from __future__ import annotations

import uuid

from domain.policies.deduplication_engine import MergedGroup, components_without_survivor

A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
C = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
D = uuid.UUID("00000000-0000-0000-0000-0000000000dd")

C1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
C2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
C3 = uuid.UUID("33333333-3333-3333-3333-333333333333")


def merge(cluster_id: uuid.UUID, members: tuple[uuid.UUID, ...], survivor: uuid.UUID) -> MergedGroup:
    return MergedGroup(cluster_id=cluster_id, members=frozenset(members), survivor=survivor)


def test_no_merges_empties_nothing() -> None:
    assert components_without_survivor([]) == []


def test_one_merge_always_keeps_its_survivor() -> None:
    assert components_without_survivor([merge(C1, (A, B), A)]) == []


def test_the_same_pair_merged_twice_the_same_way_is_not_a_conflict() -> None:
    """A later run re-detects the pair and the lead confirms. Removal is `{B}`."""
    assert components_without_survivor([merge(C1, (A, B), A), merge(C2, (A, B), A)]) == []


def test_the_same_pair_merged_with_different_survivors_empties_the_component() -> None:
    """THE DEFECT. Removal is `{B} | {A}` and the study has nothing left."""
    emptied = components_without_survivor([merge(C1, (A, B), A), merge(C2, (A, B), B)])
    assert len(emptied) == 1
    component, clusters = emptied[0]
    assert component == frozenset({A, B})
    assert clusters == frozenset({C1, C2})


def test_a_record_can_be_survivor_of_one_cluster_and_loser_of_another() -> None:
    """NOT a defect, and the case that makes the per-cluster rule wrong.

    A~B keeps A; a later run finds A~C and keeps C. A is cluster one's survivor
    and is removed by cluster two — correctly, because all three are transitively
    one work and C represents it. Refusing this would refuse an ordinary re-run.
    """
    assert components_without_survivor([merge(C1, (A, B), A), merge(C2, (A, C), C)]) == []


def test_a_chain_that_removes_everyone_is_caught_across_three_clusters() -> None:
    """The transitive form of the defect: A~B keeps A, B~C keeps B, C~A keeps C.

    Every record is somebody's survivor; every record is also somebody's loser.
    Removal is the whole component. This is the case the naive rule passes.
    """
    emptied = components_without_survivor([merge(C1, (A, B), A), merge(C2, (B, C), B), merge(C3, (C, A), C)])
    assert len(emptied) == 1
    component, clusters = emptied[0]
    assert component == frozenset({A, B, C})
    assert clusters == frozenset({C1, C2, C3})


def test_an_independent_healthy_component_is_not_dragged_in() -> None:
    """One emptied pair must not implicate an unrelated merge that is fine."""
    emptied = components_without_survivor([merge(C1, (A, B), A), merge(C2, (A, B), B), merge(C3, (C, D), C)])
    assert len(emptied) == 1
    component, clusters = emptied[0]
    assert component == frozenset({A, B})
    assert C3 not in clusters


def test_the_answer_does_not_depend_on_the_order_the_merges_arrive_in() -> None:
    """Determinism, for the same reason every ordering in this module is explicit.

    `current_resolutions` is keyed off an unordered scan, so a rule that read
    differently under a different heap order would refuse a merge on one read and
    accept it on the next.
    """
    forward = [merge(C1, (A, B), A), merge(C2, (B, C), B), merge(C3, (C, A), C)]
    assert components_without_survivor(forward) == components_without_survivor(list(reversed(forward)))
