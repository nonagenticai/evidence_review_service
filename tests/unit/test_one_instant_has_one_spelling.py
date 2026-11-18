"""Every instant this service renders goes through `instants.utc_z`. Ratcheted.

## What this exists to stop

`urn:datacompany:stable-identity:1`'s `timestamp` primitive admits ONLY the trailing
`Z` form. `datetime.isoformat()` renders a UTC offset as `+00:00`, and renders
whatever offset the value actually carries — which for a `timestamptz` column is
the session's `TimeZone`, a server setting rather than a guarantee.

So `.isoformat()` on a database instant produces a string whose spelling depends
on where the process is running, and which the released grammar may refuse.

That is not hypothetical. Measured on `POST /api/review-jobs/{id}:cancel`: one
call returned `…T06:31:59.570999+02:00` and an immediately following identical
call returned `…T04:31:59.570999+00:00` for THE SAME INSTANT — because the first
read the value this process had just written and the second read it back from
Postgres. A client that caches, diffs or de-duplicates on such a field sees a
change that did not happen.

It was then fixed twice, in two directions, and a third router shipped with
both spellings in ONE response body. `src/instants.py` records that history;
this file is what stops it recurring.

## Why a RATCHET and not a clean assertion

Nineteen call sites predate `instants.py`, spread across eight modules owned by
five other work packages. Eighteen of them render onto a wire and carry the
defect above; changing them changes those routes' output, which is a decision
for the packages that own them and not a side effect of `ER-007`.

So the baseline below is exact and floored: a NEW call site, or a new file, or
an increase in any file's count, fails immediately. A decrease never fails. The
list can only shrink, and the day it is empty this file becomes a plain
assertion.

⚠️ `a-ratchet-must-floor-on-offenders-found`: a ratchet whose detector finds
nothing passes forever while the thing it guards rots. `test_the_detector_can_actually_fail`
is the positive control, and it is not optional.

## Why AST and not grep

Measured: `grep -c "isoformat()"` reported call sites in `flow.py` that are all
DOCSTRING and COMMENT text — including, with particular irony, the comments
explaining why not to call it. An ad-hoc regex and a real parser disagreed about
the same question, which is the failure
`do-not-hand-roll-what-a-repo-tool-answers` describes.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#: The one module allowed to call `.isoformat()`, because it IS the renderer.
#:
#: ⚠️ A PATH, not a basename. This was `path.name == "instants.py"`, which
#: exempted any future `src/**/instants.py` anywhere in the tree — an exemption
#: that widens itself as the package grows is not an exemption, it is a hole
#: with a name.
_CANONICAL_RENDERER = "instants.py"

#: Measured on 2026-09-03, by the detector below. Every entry is a wire-rendered
#: instant carrying the offset defect, EXCEPT `screening.py:812` — see
#: `HASH_INPUT_SITES`.
#:
#: Owning packages, so a reader knows who may change each: `imports` and
#: `records` are `ER-002`; `deduplication` is `ER-003`; `screening`,
#: `blinded_reads` and the adjudication/unblinding renderers are `ER-004`;
#: `flow` and `flow_audit` are `ER-005`; `evidence` is `ER-006`.
BASELINE: dict[str, int] = {
    "api/routers/imports.py": 2,
    "api/routers/reviews.py": 2,
    "application/services/blinded_reads.py": 1,
    "application/services/deduplication.py": 2,
    "application/services/evidence.py": 1,
    "application/services/flow.py": 1,
    "application/services/flow_audit.py": 2,
    "application/services/imports.py": 2,
    "application/services/reviews.py": 1,
    "application/services/screening.py": 5,
}

#: ⚠️ Recorded separately because it is a DIFFERENT and arguably worse defect.
#: `screening.py:812` builds a hash input from `assigned_at.isoformat()`. That
#: digest therefore depends on the session's `TimeZone`: the same assignment
#: hashed on a server set to UTC and one set to Europe/Amsterdam produces two
#: different values, and nothing compares them so nothing would notice. It is
#: not a rendering bug that a client sees; it is a stability bug in a value
#: something is presumably keyed on. Owner: `ER-004`.
HASH_INPUT_SITES = {("application/services/screening.py", 812)}


#: Every spelling of "render this instant myself" the detector knows.
#: `strftime` is here because it is the obvious next reach once `.isoformat()`
#: is refused, and it produces whatever the format string says with no timezone
#: normalisation at all — the same defect, less visibly.
_SELF_RENDERERS = frozenset({"isoformat", "strftime"})


def _isoformat_calls(path: Path) -> list[int]:
    """Line numbers where a module renders an instant itself.

    Catches four spellings, because a review demonstrated the first version
    caught only one:

    * ``dt.isoformat()`` — the direct call;
    * ``dt.isoformat`` — the bound method, taken and called elsewhere;
    * ``getattr(dt, "isoformat")`` — the dynamic reach, which no attribute
      node matches;
    * ``dt.strftime(...)`` — the obvious next move once the first is refused.

    Attribute ACCESS is flagged as well as calls: `f = dt.isoformat; f()`
    renders exactly the same string, and a detector that only sees `Call`
    nodes reports the file clean.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _SELF_RENDERERS:
            hits.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _SELF_RENDERERS
        ):
            hits.add(node.lineno)
    return sorted(hits)


def _measured() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC).as_posix() == _CANONICAL_RENDERER:
            continue
        calls = _isoformat_calls(path)
        if calls:
            found[str(path.relative_to(_SRC))] = len(calls)
    return found


def test_the_detector_can_actually_fail() -> None:
    """The positive control. Without it the whole file is a green no-op.

    A ratchet whose detector matches nothing passes forever while the thing it
    guards rots — and it looks exactly like a ratchet that is working. So the
    detector is pointed at a snippet that definitely contains a call, and at one
    that definitely does not but MENTIONS the word, which is the case a grep
    would get wrong.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        offender = Path(tmp) / "offender.py"
        offender.write_text("import datetime\nx = datetime.datetime.now().isoformat()\n")
        assert _isoformat_calls(offender) == [2]

        innocent = Path(tmp) / "innocent.py"
        innocent.write_text('"""Never call .isoformat() here."""\n# also not .isoformat()\nx = 1\n')
        assert _isoformat_calls(innocent) == [], (
            "the detector counted a docstring or comment — this is exactly why it is AST and not grep"
        )


def test_no_file_outside_the_baseline_renders_an_instant_itself() -> None:
    """A NEW call site fails here, in a second, instead of in review or in production.

    If this is failing on code you just wrote: you wanted `instants.utc_z`. It
    renders UTC with a trailing `Z`, which is the only form the released
    `timestamp` primitive admits, and it does not depend on the server's
    timezone setting.
    """
    unexpected = {name: n for name, n in _measured().items() if name not in BASELINE}
    assert unexpected == {}, (
        f"{unexpected} call `.isoformat()` and are not in the baseline. "
        "Use `instants.utc_z` — see src/instants.py for what this defect cost."
    )


def test_no_baselined_file_has_grown() -> None:
    """The ratchet. Counts may fall, never rise.

    A file already in the baseline is allowed its existing offenders — they are
    another package's wire format to change — but it may not acquire more, which
    is how a "temporary" exception becomes permanent.
    """
    measured = _measured()
    grown = {name: (BASELINE[name], measured[name]) for name in BASELINE if measured.get(name, 0) > BASELINE[name]}
    assert grown == {}, f"baseline (expected, actual) exceeded: {grown}"


def test_the_baseline_names_only_files_that_exist() -> None:
    """A baseline entry for a deleted or cleaned file is a stale exception.

    It would silently permit a future file of the same name to carry offenders,
    which is how an exception list stops meaning anything.
    """
    missing = [name for name in BASELINE if not (_SRC / name).exists()]
    assert missing == [], f"baseline names files that no longer exist: {missing}"


def test_the_hash_input_site_is_still_exactly_where_it_was() -> None:
    """`screening.py:812` hashes a locale-dependent string. Pinned, not forgotten.

    This is not the rendering defect; it is worse in one respect. The digest of
    an assignment depends on the session `TimeZone`, so the same row hashed on
    two differently-configured servers yields two different values — and nothing
    compares them across servers, so nothing would ever report it.

    Pinned by LINE so that if the code moves, somebody has to look at it again
    rather than let the note drift away from the thing it describes.
    """
    for name, line in HASH_INPUT_SITES:
        calls = _isoformat_calls(_SRC / name)
        assert line in calls, (
            f"{name}:{line} no longer calls `.isoformat()`. If it was fixed, delete this "
            "entry and shrink the baseline; if it merely moved, re-read it and update the line."
        )
