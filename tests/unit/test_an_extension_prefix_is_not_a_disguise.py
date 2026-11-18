"""`x-work-type` was not `work_type`, and the denylist said it was — `DM-AT-18`.

`domain/policies/denylist` has always documented that *"`work_type`, `workType`,
`WorkType`, `x_work_type` and `work type` are the same token wearing different
clothes."* Measured on `897d506`, four of those five were true::

    find_forbidden_keys({"value": {"work_type":   "regulatory_instrument"}})
    -> ['$.value.work_type']
    find_forbidden_keys({"value": {"x-work-type": "regulatory_instrument"}})
    -> []

`_norm` folds `x-work-type` to `xworktype` — its docstring says exactly that —
and `xworktype` is not in the forbidden set, so the membership test missed it.
The prose described the intent and the code implemented four fifths of it.

## Why this is the spelling that mattered, not an exotic one

`x-` is this estate's extension prefix. `research_contracts` ships
`schemas/meta/datacompany-extension-vocabulary.v1.schema.json` for it, so it is the
first thing a client reaches for when a field is not in the base vocabulary —
which makes an `x-` prefixed key the MOST likely spelling of an accidental
pre-ADR regulatory activation, not the least. `16-…:§17` calls the gate *"an
executable deny gate, not merely a deployment flag"*, and a gate with a
documented bypass is a deployment flag.

Found by an `ER-006` route test that had no reason to believe the body was
special: it asserted the guard on every route that takes free-form JSON, and one
of the probes happened to use the extension spelling.

## The negative controls are the load-bearing half

Stripping a leading `x` unconditionally would refuse `xref` and `xml_id` —
ordinary keys that begin with the letter. The prefix is only stripped when a
SEPARATOR follows it, so `xworktype` written solid is deliberately NOT matched:
it is not the extension convention, and inventing a match for it would trade a
documented gap for an undocumented false positive.
"""

from __future__ import annotations

import pytest

from domain.policies.denylist import find_forbidden_keys

#: Every spelling that must be refused. `work_type` is the one that matters most
#: — the denylist calls it *"the activation switch of the unaccepted model"* —
#: so it is spelled here every way a real client would produce it.
DISGUISES = [
    "work_type",
    "workType",
    "WorkType",
    "work type",
    "x_work_type",
    "x-work-type",
    "X-Work-Type",
    "x.work.type",
    "regulatory_instrument",
    "x-regulatory-provision",
    "X_LEGAL_EVENT",
]

#: Keys that merely begin with the letter, and must stay clean. Without these the
#: fix above could be "strip every leading x", which refuses ordinary payloads.
INNOCENT = ["xref", "xml_id", "x_axis", "xworktype", "worktypes", "network_type"]


@pytest.mark.parametrize("key", DISGUISES)
def test_a_guarded_key_is_refused_however_it_is_spelled(key: str) -> None:
    assert find_forbidden_keys({"value": {key: "regulatory_instrument"}}) == [f"$.value.{key}"], (
        f"{key!r} reached the body clean. The denylist's docstring promises this spelling is "
        "the same token, and DM-AT-18 requires the activation to fail."
    )


@pytest.mark.parametrize("key", INNOCENT)
def test_a_key_that_merely_starts_with_x_is_not_guarded(key: str) -> None:
    assert find_forbidden_keys({"value": {key: 1}}) == [], (
        f"{key!r} was refused. Stripping a leading 'x' without requiring a separator turns a "
        "documented gap into an undocumented false positive."
    )


def test_the_disguise_is_caught_at_depth_and_reports_its_path() -> None:
    """The requirement is "at any depth" — a top-level check is satisfied by nesting."""
    body = {"a": [{"b": {"x-work-type": "regulatory_instrument"}}]}
    assert find_forbidden_keys(body) == ["$.a[0].b.x-work-type"]
