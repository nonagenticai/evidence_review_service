"""A freeze with one hole is not a freeze, and `__init__` was the hole.

`_FrozenDict`/`_FrozenList` override every named mutator on `dict` and `list`,
which was verified exhaustively — all eight dict mutators and all thirteen list
mutators raise. `__init__` was not in either list, and `dict.__init__` does not
construct: it UPDATES IN PLACE. So a sealed manifest could be re-seeded through
the one write method nobody thought of:

    finalized.instance.__init__({"entity_type": "FORGED"})

after which `instance` held forged content while `content_hash` went on
certifying what it used to hold — the precise failure the seal exists to make
impossible. `list.__init__` behaves the same way.

Construction now goes through `_seal()`, which populates via the unbound base
method and never touches the overrides, leaving `__init__` free to refuse like
every other write. These tests pin both halves: that the hole is closed, and
that closing it did not break the ability to build one.
"""

from __future__ import annotations

import copy
import pickle

import pytest

from application.services.run_manifest import _freeze


def _frozen():
    return _freeze(
        {
            "entity_type": "run_manifest",
            "inputs": {"input_refs": [{"kind": "document"}, {"kind": "dataset"}]},
        }
    )


def test_init_cannot_reseed_a_sealed_mapping() -> None:
    frozen = _frozen()

    with pytest.raises(TypeError):
        frozen.__init__({"entity_type": "FORGED"})

    assert frozen["entity_type"] == "run_manifest"


def test_init_cannot_reseed_a_sealed_list() -> None:
    frozen = _frozen()
    refs = frozen["inputs"]["input_refs"]

    with pytest.raises(TypeError):
        refs.__init__([{"kind": "FORGED"}])

    assert [ref["kind"] for ref in refs] == ["document", "dataset"]


def test_the_freeze_reaches_every_nesting_level() -> None:
    """A shallow freeze would leave the interesting content writable — the
    members a manifest actually attests live inside nested containers, not at
    the top level."""
    frozen = _frozen()

    with pytest.raises(TypeError):
        frozen["inputs"]["input_refs"][0]["kind"] = "FORGED"
    with pytest.raises(TypeError):
        frozen["inputs"]["input_refs"].append({"kind": "FORGED"})

    assert frozen["inputs"]["input_refs"][0]["kind"] == "document"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.__setitem__("x", 1), id="__setitem__"),
        pytest.param(lambda d: d.__delitem__("entity_type"), id="__delitem__"),
        pytest.param(lambda d: d.update({"x": 1}), id="update"),
        pytest.param(lambda d: d.setdefault("x", 1), id="setdefault"),
        pytest.param(lambda d: d.pop("entity_type"), id="pop"),
        pytest.param(lambda d: d.popitem(), id="popitem"),
        pytest.param(lambda d: d.clear(), id="clear"),
        pytest.param(lambda d: d.__ior__({"x": 1}), id="__ior__"),
        pytest.param(lambda d: d.__init__({"x": 1}), id="__init__"),
    ],
)
def test_every_mapping_write_is_refused(mutate) -> None:
    frozen = _frozen()

    with pytest.raises(TypeError):
        mutate(frozen)

    assert dict(frozen)["entity_type"] == "run_manifest"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda xs: xs.__setitem__(0, "x"), id="__setitem__"),
        pytest.param(lambda xs: xs.__delitem__(0), id="__delitem__"),
        pytest.param(lambda xs: xs.__iadd__(["x"]), id="__iadd__"),
        pytest.param(lambda xs: xs.__imul__(2), id="__imul__"),
        pytest.param(lambda xs: xs.append("x"), id="append"),
        pytest.param(lambda xs: xs.extend(["x"]), id="extend"),
        pytest.param(lambda xs: xs.insert(0, "x"), id="insert"),
        pytest.param(lambda xs: xs.remove(xs[0]), id="remove"),
        pytest.param(lambda xs: xs.pop(), id="pop"),
        pytest.param(lambda xs: xs.clear(), id="clear"),
        pytest.param(lambda xs: xs.sort(key=repr), id="sort"),
        pytest.param(lambda xs: xs.reverse(), id="reverse"),
        pytest.param(lambda xs: xs.__init__(["x"]), id="__init__"),
    ],
)
def test_every_sequence_write_is_refused(mutate) -> None:
    refs = _frozen()["inputs"]["input_refs"]

    with pytest.raises(TypeError):
        mutate(refs)

    assert len(refs) == 2


def test_the_read_path_still_behaves_like_a_dict_and_a_list() -> None:
    """The whole reason these are `dict`/`list` subclasses rather than
    `MappingProxyType`/`tuple` is that `jsonschema`'s `"object"`/`"array"` checks
    are `isinstance`. If a freeze broke that, validation of the finalized
    instance would fail with "not of type 'object'" — the opposite of the
    zero-errors promise the builder makes."""
    frozen = _frozen()

    assert isinstance(frozen, dict)
    assert isinstance(frozen["inputs"]["input_refs"], list)
    assert set(frozen) == {"entity_type", "inputs"}
    assert len(frozen["inputs"]["input_refs"]) == 2
    assert [ref["kind"] for ref in frozen["inputs"]["input_refs"]] == ["document", "dataset"]


def test_a_sealed_manifest_cannot_be_laundered_through_copy() -> None:
    """Either would otherwise hand back a WRITABLE plain container holding the
    sealed content, which is the seal defeated by a different route."""
    frozen = _frozen()

    for attempt in (lambda: copy.copy(frozen), lambda: copy.deepcopy(frozen)):
        with pytest.raises(TypeError):
            attempt()


def test_a_pickled_manifest_cannot_be_reconstituted() -> None:
    """⚠️ `pickle.dumps` SUCCEEDS, and that is not the hole it looks like.

    Measured, because assuming would have gone the other way: `dumps` walks the
    read path only, so it produces a byte string quite happily. The refusal
    lands on `loads`, which rebuilds a `dict` subclass by calling the class and
    then filling it — both of which now raise. So the property that actually
    matters holds: no route through `pickle` yields a writable object carrying
    this manifest's content. Asserting on `dumps` instead would have pinned the
    wrong boundary and would go red the moment serialisation was legitimately
    wanted for, say, a debug dump.
    """
    blob = pickle.dumps(_frozen())

    with pytest.raises(TypeError):
        pickle.loads(blob)
