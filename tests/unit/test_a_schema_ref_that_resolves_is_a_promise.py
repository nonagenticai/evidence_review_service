"""Two of ERS's four schema refs name documents that do not exist.

This is the inverse of a normal contract test: it asserts that two refs
**cannot** be resolved, and two **can**. It exists so the day somebody authors
`review-protocol` or `review-record`, this file goes RED and tells them what
else has to move — rather than the schema quietly appearing in the registry
while ERS keeps hashing an undeclared object and telling API clients it
validated one.

`review-flow-ledger` WAS in the negative list and moved on 2026-09-02, which is
this file working exactly as designed. `research-contracts` 0.4.0 released
`urn:datacompany:review-flow-ledger:1` — authored from `flow_derivation.derive`'s own
output rather than from a specification — this suite went red, and
`LEDGER_SCHEMA_REF` followed it. That is the "author-then-switch" order the
constants' docstrings prescribe, performed rather than described.

⚠️ When this goes red again: do NOT delete the case. Author-then-switch is the
order — change the constant to its URN and give the body something that actually
validates against it, then move the ref into `_RESOLVES` here.

Note what "resolves" does and does not claim. The export ref is SEALED: a
manifest is hashed through `compute_content_hash` against it. The ledger ref is
not, and cannot be — a flow ledger is a projection recomputed on every read and
nothing signs it. What its resolution buys is that a consumer can validate what
it was handed, and that
`tests/integration/test_the_flow_body_satisfies_its_released_contract.py` fails
the moment the body and the document disagree. Both are promises; they are
different promises, and this file holds them to the same standard of being
resolvable at all.
"""

import pytest
from research_contracts.canonical.registry import canonical_schemas

from application.services.flow import EXPORT_SCHEMA_REF, LEDGER_SCHEMA_REF
from application.services.imports import RECORD_SCHEMA_REF
from application.services.reviews import PROTOCOL_SCHEMA_REF

#: Refs that MUST resolve. The export is the artefact a third party verifies;
#: the ledger is the contract `research_output_service` binds an output to, and
#: a compilation refuses its input outright if it cannot resolve this.
_RESOLVES = [EXPORT_SCHEMA_REF, LEDGER_SCHEMA_REF]

#: Refs that are documentation pointers today. A URL spelling is deliberate:
#: it announces "not something to resolve". See each constant's docstring.
_DOES_NOT_RESOLVE = [PROTOCOL_SCHEMA_REF, RECORD_SCHEMA_REF]


def _registry_ids() -> set[str]:
    return set(canonical_schemas().keys())


@pytest.mark.parametrize("ref", _RESOLVES)
def test_a_ref_this_service_advertises_resolves_in_the_published_wheel(ref: str) -> None:
    """A ref ERS puts on the wire must be registered, or the promise is empty.

    Non-vacuity control for the negatives below: if `canonical_schemas()` ever
    returned empty — a packaging regression that force-include of `schemas/`
    would cause — every negative case would pass for the wrong reason and this
    one would fail.
    """
    assert ref in _registry_ids(), (
        f"{ref!r} is advertised on the wire by this service and no longer resolves. "
        f"For the export ref that breaks third-party verification; for the ledger ref "
        f"it makes every research_output_service compilation refuse its input."
    )


@pytest.mark.parametrize("ref", _DOES_NOT_RESOLVE)
def test_a_ref_nothing_validates_against_is_not_in_the_registry(ref: str) -> None:
    """RED means somebody authored the schema. Read this module's docstring."""
    assert ref not in _registry_ids(), (
        f"{ref!r} now RESOLVES. That is good news and this test is the "
        f"checklist: switch the constant to its URN and hash the payload "
        f"through compute_content_hash, then move it to _RESOLVES above. "
        f"Leaving it here means ERS advertises a schema it still does not "
        f"validate against."
    )


def test_no_ref_is_spelled_as_a_urn_it_cannot_back() -> None:
    """A URN is the shape a consumer resolves; a URL announces a doc pointer.

    Guards the specific mistake the constants' docstrings argue against —
    minting `urn:datacompany:*` for a document the registry does not have, which
    reads as resolvable to every consumer and to the seal path alike.
    """
    ids = _registry_ids()
    for ref in _DOES_NOT_RESOLVE:
        assert not ref.startswith("urn:"), (
            f"{ref!r} is spelled as a URN but is not in the registry ({len(ids)} $ids). Author the schema first."
        )
