"""The pre-ADR regulatory denylist, enforced on every inbound body.

`ER-002.7` (`01-…:415-417`) and `DM-AT-18`. Until `ADR-DATACOMPANY-REGULATORY-MODEL`
is dual-accepted, a canonical regulatory activation must be **refused**, and a
regulatory PDF must still import as an ordinary record
(``record_type='report'``) — that pairing is the positive control, and a rule
that blocked both would pass the negative test while failing the programme.

Two things this module is careful about:

**Rejection, never silent removal.** Dropping the field would import the record
successfully while the caller believes it was typed as a regulatory instrument.
A silent success is worse than a failure here, because nothing downstream can
tell the two apart.

**Normalized matching.** ``work_type``, ``workType``, ``WorkType``,
``x_work_type`` and ``work type`` are the same token wearing different clothes.
Matching the literal string only would leave the guard trivially bypassable by a
caller using camelCase, which is what a JavaScript client sends by default.

The list is loaded from ``research_contracts``'s
``config/pre-adr-regulatory-denylist.json`` when that file ships with the
installed wheel, so this service does not become a second, drifting copy of the
authority. The in-tree tuple is the fallback and is asserted equal to the
contract file whenever the file is present.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Fallback vocabulary, from `01-…:415-417` plus the guarded discriminator.
#: `work_type` is the one that matters most: it is the *activation* switch of the
#: unaccepted model, so a body carrying it at any depth is asking for a canonical
#: regulatory entity regardless of what value it carries.
FALLBACK_FORBIDDEN_KEYS: tuple[str, ...] = (
    "work_type",
    "regulatory_instrument",
    "regulatory_provision",
    "regulatory_effect",
    "legal_event",
    "legal_status",
    "regulatory_applicability",
    "regulatory_change",
    "normative_citation",
    "consolidation_snapshot",
    "recommendation_regulatory_link",
)


#: An ``x-``/``x_`` extension prefix, which this estate uses as a real
#: convention — `research_contracts` ships
#: ``schemas/meta/datacompany-extension-vocabulary.v1.schema.json``. A separator is
#: REQUIRED after the ``x`` so that an ordinary key beginning with the letter
#: (``xref``, ``xml_id``) is never mistaken for an extension.
_EXTENSION_PREFIX = re.compile(r"^x[-_. ]", re.IGNORECASE)


def _norm(token: str) -> str:
    """Fold a key to its comparable form: lowercase alphanumerics only.

    ``workType`` -> ``worktype``; ``x-work-type`` -> ``xworktype``. The camelCase
    split is handled by inserting a boundary before an interior capital first,
    so ``workType`` and ``work_type`` land on the same string.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", token)
    return _NON_ALNUM.sub("", spaced.lower())


def _comparable(token: str) -> set[str]:
    """Every spelling of ``token`` that means the same guarded key.

    ## The gap this closes, measured 2026-09-02

    This module's own docstring has always promised that *"``work_type``,
    ``workType``, ``WorkType``, ``x_work_type`` and ``work type`` are the same
    token wearing different clothes."* Four of those five were true.
    ``x_work_type`` and ``x-work-type`` were **not**::

        find_forbidden_keys({"value": {"work_type":   "regulatory_instrument"}})
        -> ['$.value.work_type']
        find_forbidden_keys({"value": {"x-work-type": "regulatory_instrument"}})
        -> []          # 201 Created

    `_norm` folds ``x-work-type`` to ``xworktype`` — the docstring says so in as
    many words — and ``xworktype`` is not in the forbidden set, so membership
    missed it. The documentation described the intent; the code implemented four
    fifths of it.

    That is not a theoretical spelling. ``x-`` is this estate's extension prefix,
    with a released schema naming it, so it is the FIRST thing a client reaches
    for when a field is not in the base vocabulary — which makes it the most
    likely spelling of an accidental pre-ADR regulatory activation, not the least.
    `DM-AT-18` and `ER-V013` both require that activation to fail, and it did not.

    Found by an `ER-006` route test that asserted the guard on a body it had no
    reason to believe was special.
    """
    normalized = _norm(token)
    forms = {normalized}
    if _EXTENSION_PREFIX.match(token):
        forms.add(_norm(token[2:]))
    return forms


@lru_cache(maxsize=1)
def forbidden_keys() -> tuple[str, ...]:
    """The denylist, preferring the contract file over the in-tree fallback."""
    try:
        import research_contracts

        config = Path(research_contracts.__file__).resolve().parent / "config" / "pre-adr-regulatory-denylist.json"
        if config.is_file():
            data = json.loads(config.read_text())
            keys = data.get("forbidden_keys") or data.get("denied_tokens") or []
            if keys:
                return tuple(str(k) for k in keys)
    except Exception:  # noqa: BLE001 - the fallback is the point; a missing file is not an error
        pass
    return FALLBACK_FORBIDDEN_KEYS


@lru_cache(maxsize=1)
def _normalized_forbidden() -> frozenset[str]:
    return frozenset(_norm(k) for k in forbidden_keys())


def find_forbidden_keys(payload: Any, _path: str = "$") -> list[str]:
    """Return every JSON path at which a guarded key appears. Empty means clean.

    Walks the whole body — "at any depth" is the requirement, and a top-level-only
    check is satisfied by nesting the field one level down.

    Values are deliberately NOT inspected. A record whose *title* contains the
    words "legal event" is an ordinary bibliographic record; only a *key* asserts
    a canonical type.

    ## Iterative, because this function was the thrower

    This was written as a recursive walk and gave out just past the interpreter's
    1,000-frame limit. `RecursionError` derives from `RuntimeError`, so it went
    straight past every `ValueError` handler between here and the edge and left
    as a bare `500 text/plain` — no problem+json body at all for the gateway to
    forward. `import_parser._decode_json` already records the measurement that
    established this was the thrower rather than `json.loads`, whose C scanner
    absorbs ~114,000 levels.

    The parser's `MAX_JSON_DEPTH` closed that for record payloads it parses, but
    NOT for the two callers that reach this function with a body the parser never
    sees: `routers/imports.py:73` guards the raw JSON envelope before any parser
    runs — measured on `393ae61` as a `500 text/plain` for a 1,200-deep envelope
    — and `routers/reviews.py::_guard` guards a protocol body, which has no
    parser at all.

    So the recursion is removed rather than bounded. A bound HERE would be the
    wrong instrument in a way that is easy to miss: `imports.guard_denylist`
    calls this on the parsed row LIST, which sits one or two wrappers below the
    payload root the parser measured, so any bound equal to the parser's would
    start refusing records the parser had just accepted. Depth belongs where the
    destination is known — `import_parser._audit_json` for stored records,
    `api.body_guard` for request bodies that go straight to JSONB and RFC 8785
    canonicalisation. This function's job is only to never be the thing that
    crashes.

    Traversal order is preserved exactly: depth-first, a key's own hit emitted
    before descending into its value, children in document order. That is why the
    stack carries the key alongside the node and pushes children reversed —
    ordering is part of this function's contract, since `forbidden_paths` in the
    refusal body is what tells a caller where to look.

    ## What removing the recursion took away, and what has to replace it

    ⚠️ **This walk is UNBOUNDED and does not terminate on its own.** The
    `RecursionError` was not only a bug, it was also — accidentally — the thing
    that STOPPED the walk, and deleting a terminator obliges the caller to supply
    a deliberate one. Two consequences, both measured:

    * **Cost.** Running to completion means building an O(depth) path string per
      node, so a deep, wide body costs O(nodes x depth). An unbounded caller
      turned a cheap crash into an amplifier: a 371 KiB import envelope at depth
      40,000 with 150,000 leaves refused in 2.89s and 3.28 GB of RSS, against a
      pod limit of 512Mi. It used to be a `500` in 0.01s at a flat 140 MB. Every
      caller therefore bounds depth FIRST — `api.body_guard.guard_body` does both
      in one call, and `routers/imports.py` does it explicitly because it is the
      one caller that guards itself.
    * **Cycles.** A self-referencing structure now walks forever where it
      previously raised in microseconds. Not reachable over HTTP — JSON has no
      cycles and every caller here is fed by `json.loads` — and a depth bound in
      front terminates it regardless. It matters if this walk is ever pointed at
      a Python structure that did not come from a JSON decoder.
    """
    hits: list[str] = []
    forbidden = _normalized_forbidden()
    # (node, path-so-far, the key this node was reached through — or None for
    # the root and for list elements, which are not keys and cannot be guarded).
    stack: list[tuple[Any, str, str | None]] = [(payload, _path, None)]
    while stack:
        node, path, key = stack.pop()
        if key is not None and _comparable(key) & forbidden:
            hits.append(path)
        if isinstance(node, dict):
            for child_key, value in reversed(list(node.items())):
                stack.append((value, f"{path}.{child_key}", str(child_key)))
        elif isinstance(node, list):
            for index in range(len(node) - 1, -1, -1):
                stack.append((node[index], f"{path}[{index}]", None))
    return hits
