"""`uv.lock` must actually satisfy `pyproject.toml`. Nothing else checks this.

## The failure this exists for, measured

`ER-007` raised the `research-contracts` floor to `>=0.6.0` in `pyproject.toml`
and left `uv.lock` pinning `0.5.0`, because 0.6.0 had not been published yet.

I assumed `uv sync --frozen` would refuse that. **It does not.** Measured in
this worktree:

    $ uv sync --frozen
     - research-contracts==0.6.0
     + research-contracts==0.5.0
    EXIT=0

It exits 0 and installs the LOCKED version, ignoring the constraint entirely.
So CI would have installed 0.5.0, and the failure would have surfaced much
later and much further away — `unknown schema_ref
'urn:datacompany:evidence-snapshot-manifest:1'` from inside a hashing call, in a
test whose subject is evidence snapshots and whose cause is a dependency floor.

## Why the fleet guard does not cover it

`python-service.yml` runs a `lock-downgrade-guard`, and its name is close
enough to be misleading. It compares the lock against the **previous** lock to
catch a version going backwards across a merge, and it never reads
`pyproject.toml`. It is also **post-merge only**, so even the drift it does
catch is caught after landing on main.

Neither half of that helps here: the constraint and the lock were inconsistent
from the first commit, and nothing compared them.

## Why stdlib

`packaging` is not installed in this environment — checked, not assumed — so
`Requirement`/`SpecifierSet` are unavailable. Every constraint in this project
is `>=` (all 26, measured), which is comparable with a release-tuple. Anything
else raises rather than being skipped: a guard that silently ignores the
specifier it does not understand is worse than no guard, because the dependency
it cannot parse is exactly the unusual one somebody should look at.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "pyproject.toml"
_LOCK = _ROOT / "uv.lock"

#: PEP 508 name + specifier, ignoring extras and environment markers.
_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*?)\s*(?:;.*)?$")

#: The only operator this project uses. Deliberately narrow — see the module
#: docstring on why an unparsed specifier raises instead of being skipped.
_SUPPORTED = (">=",)


def _normalise(name: str) -> str:
    """PEP 503 normalisation — `research_contracts` and `research-contracts` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


#: A released version sorts ABOVE any pre-release of the same number.
_FINAL = 1
_PRERELEASE = 0


def _release(version: str) -> tuple[tuple[int, ...], int]:
    """`(release tuple, finality)`, where a pre-release sorts BELOW its release.

    ⚠️ This used to return the release tuple alone and drop pre-release and
    local segments, "which is wrong in general and right here". It was wrong
    here too, and a review proved it by execution: `_release("1.0.0rc1")` gave
    `(1, 0, 0)`, so a floor of `>=1.0.0` against a locked `1.0.0rc1` was NOT a
    violation. Had `research-contracts` been locked at `0.6.0rc1` against
    `>=0.6.0`, this file would have gone green on the exact drift it was
    written for — the failure mode `a-verification-that-cannot-go-red`
    describes, in the guard rather than in the system.

    PEP 440 ordering is a great deal more than this. Two components are enough
    for the question actually asked — "is the locked version at least the
    declared floor" — and anything richer would be reimplementing `packaging`,
    which is not installed here.
    """
    head = re.split(r"[+]", version, maxsplit=1)[0]
    finality = _PRERELEASE if re.search(r"(?i)(a|b|rc|alpha|beta|dev|pre)\d*$|-", head) else _FINAL
    numeric = re.split(r"(?i)[-_.]?(a|b|rc|alpha|beta|dev|pre)", head, maxsplit=1)[0]
    parts = []
    for chunk in numeric.split("."):
        digits = re.match(r"\d+", chunk)
        if digits is None:
            break
        parts.append(int(digits.group()))
    if not parts:
        raise ValueError(f"cannot order version {version!r}")
    return tuple(parts), finality


def _declared() -> dict[str, str]:
    """`{normalised name: STRICTEST floor}` — floors only."""
    return _declared_with_bare()[0]


def _declared_with_bare() -> tuple[dict[str, str], set[str], dict[str, str]]:
    """`({name: strictest floor}, {names with no floor}, {name: direct reference})`.

    All three from `[project].dependencies` and every dependency group. A direct
    reference (`name @ git+https://…@tag`) is not a floor: it names the exact
    source, and `test_every_direct_reference_matches_the_locked_git_source`
    checks the lock agrees with it.
    """
    data = tomllib.loads(_PYPROJECT.read_text())
    raw: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        raw += [entry for entry in group if isinstance(entry, str)]

    declared: dict[str, str] = {}
    bare: set[str] = set()
    direct: dict[str, str] = {}
    for entry in raw:
        match = _REQUIREMENT.match(entry)
        assert match, f"cannot parse requirement {entry!r}"
        spec = match.group("spec").strip()
        if not spec:
            # A bare requirement has no floor to check, but it MUST still be in
            # the lock — that is the "resolved at install time" case, which is
            # the worse of the two. Recorded with an empty floor so
            # `test_every_declared_dependency_is_in_the_lock` still sees it.
            #
            # ⚠️ This used to `continue`, in a file whose docstring says nothing
            # is skipped. It dropped the dependency from BOTH tests.
            bare.add(_normalise(match.group("name")))
            continue
        if spec.startswith("@"):
            # PEP 508 direct reference. The reference itself is the constraint;
            # it is compared against the lock's recorded git source below.
            direct[_normalise(match.group("name"))] = spec[1:].strip()
            continue
        operator = next((op for op in _SUPPORTED if spec.startswith(op)), None)
        if operator is None:
            raise AssertionError(
                f"{entry!r} uses a specifier this guard does not implement. Add it to "
                "`_SUPPORTED` and teach `_satisfies` about it — do NOT skip it, or the one "
                "dependency somebody pinned unusually becomes the one nothing checks."
            )
        name = _normalise(match.group("name"))
        floor = spec[len(operator) :].strip()
        # ⚠️ The STRICTEST floor wins, not the last one seen. This was a plain
        # assignment, and a review found `httpx` already declared twice (both
        # `>=0.28.0` today, so benign). A dev group declaring `httpx>=0.20.0`
        # beside a project floor of `>=0.28.0` would have silently kept the dev
        # floor, and a lock at 0.20.0 would have passed.
        if name in declared and _release(declared[name]) >= _release(floor):
            continue
        declared[name] = floor
    return declared, bare, direct


def _declared_names() -> set[str]:
    """Every declared distribution, floor or not — what the lock must contain."""
    floors, bare, direct = _declared_with_bare()
    return set(floors) | bare | set(direct)


def _locked_git_sources() -> dict[str, str]:
    """`{normalised name: git source URL}` for every package the lock fetched from git."""
    data = tomllib.loads(_LOCK.read_text())
    return {
        _normalise(package["name"]): package["source"]["git"]
        for package in data.get("package", [])
        if isinstance(package.get("source"), dict) and "git" in package["source"]
    }


def _locked() -> dict[str, str]:
    data = tomllib.loads(_LOCK.read_text())
    return {_normalise(p["name"]): p["version"] for p in data.get("package", []) if "version" in p}


def test_the_guard_can_actually_fail() -> None:
    """The positive control. A comparison that cannot say "no" checks nothing."""
    assert _release("0.5.0") < _release("0.6.0")
    assert _release("0.6.0") >= _release("0.6.0")
    assert _release("0.10.0") > _release("0.9.9"), "string ordering would get this wrong"
    with pytest.raises(ValueError):
        _release("not-a-version")


@pytest.mark.parametrize("prerelease", ["1.0.0rc1", "1.0.0a1", "1.0.0b2", "1.0.0.dev1", "1.0.0-rc1"])
def test_a_prerelease_does_not_satisfy_a_floor_of_its_own_release(prerelease: str) -> None:
    """`1.0.0rc1` is BELOW `1.0.0`. The first version of this guard said otherwise.

    It dropped pre-release segments, so `_release("1.0.0rc1")` was `(1, 0, 0)`
    and a floor of `>=1.0.0` was satisfied by a release candidate. Had
    `research-contracts` been locked at `0.6.0rc1` against `>=0.6.0`, this file
    would have gone green on the exact drift it exists to catch — a guard that
    cannot go red, which is worse than no guard because it is believed.
    """
    assert _release(prerelease) < _release("1.0.0"), prerelease
    assert _release("1.0.0") > _release(prerelease), prerelease


def test_a_local_version_still_satisfies_its_release() -> None:
    """`1.0.0+local` is the same release, not a pre-release. Both directions.

    Stated because the pre-release fix above is one regex away from also
    demoting a local segment, which would make every locally-built wheel look
    stale.
    """
    assert _release("1.0.0+abc") >= _release("1.0.0")
    assert not _release("1.0.0+abc") < _release("1.0.0")


def test_every_declared_floor_is_met_by_the_locked_version() -> None:
    """The check `uv sync --frozen` does not perform.

    If this fails: `uv lock` (not `--frozen`) against an index that actually has
    the version you raised the floor to. If that version is not published yet,
    the floor is premature — the branch cannot be pushed until it is, and this
    test failing IS that fact, arriving locally instead of as a confusing
    runtime error in CI.
    """
    locked = _locked()
    violations = {
        name: (floor, locked[name])
        for name, floor in _declared().items()
        if name in locked and _release(locked[name]) < _release(floor)
    }
    assert violations == {}, (
        "uv.lock does not satisfy pyproject.toml — (required floor, locked version): "
        f"{violations}. `uv sync --frozen` installs the LOCKED version and exits 0, so "
        "nothing downstream would tell you."
    )


def test_every_declared_dependency_is_in_the_lock() -> None:
    """A declared dependency missing from the lock is resolved at install time.

    Which means the version differs between machines and between runs, and the
    first symptom is a test that passes locally and fails in CI for reasons that
    have nothing to do with the diff.
    """
    locked = _locked()
    missing = sorted(name for name in _declared_names() if name not in locked)
    assert missing == [], f"declared but not locked: {missing}"


def test_every_direct_reference_matches_the_locked_git_source() -> None:
    """A `name @ git+…@tag` requirement must be the git source the lock installed.

    Direct references are the one constraint `uv sync --frozen` cannot silently
    satisfy from a different version: they name a repository and a revision. The
    lock records that source verbatim, so the two are comparable — and a stale
    reference (the tag moved, the repo renamed) is a failure here rather than an
    install-time surprise on somebody else's machine.
    """
    declared = _declared_with_bare()[2]
    locked = _locked_git_sources()
    violations = {
        name: (reference, locked.get(name, "<not a git source in uv.lock>"))
        for name, reference in declared.items()
        if _git_reference_identity(reference) != _git_reference_identity(locked.get(name, ""))
    }
    assert violations == {}, (
        f"a direct reference in pyproject.toml does not match the locked git source — (declared, locked): {violations}"
    )


def _git_reference_identity(reference: str) -> tuple[str, str]:
    """`(repository URL, revision)` from a `git+URL?rev=REF` or `git+URL@REF` form."""
    url = reference.removeprefix("git+").split("#", 1)[0]
    repository, _, query = url.partition("?")
    revision = ""
    for parameter in query.split("&"):
        key, _, value = parameter.partition("=")
        if key in ("rev", "tag", "branch") and value:
            revision = value
    if not revision and "@" in repository:
        repository, _, revision = repository.rpartition("@")
    return repository, revision
