"""The gate register, the migration's closed CHECK and the snapshot service agree.

`14-…:345` names five things snapshot creation must reject, and — exactly as with
the job catalogue — two independent copies of that list exist in this service by
design:

* `alembic/versions/0021_…` — the constants rendered into
  `ck_evidence_snapshots_gate_ledger_is_honest` and the member-kind CHECK. That is
  the copy a caller cannot route around, because it is the database;
* `domain/policies/snapshot_gates` — the copy the producer reads before it writes
  a ledger, and the copy that records which gates still have no subject matter.

Neither is derived from the other, and this file is the only thing holding them
equal. Importing one into the other would be tidier and strictly worse: a shared
constant makes the CHECK and its caller agree *by construction*, a guarantee that
survives both being wrong together. Two independently typed spellings held equal
by a test cannot drift quietly, because editing either one alone turns this file
red.

The register is sharper than the catalogue's in one respect. Three of the five
gates are deliberately absent rather than stubbed, because a gate over an absent
concept cannot fail and a snapshot reporting five cleared gates would be
indistinguishable from one that five real gates examined. `OWED_BY` is what turns
that omission from an oversight into a statement, and the tests below are what
stop it becoming a lie in either direction — a gate that gains an implementation
but keeps its debt entry, or one that is dropped from both and quietly forgotten.

Unit tests: no database, no fixtures. Everything asserted here is a constant.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from application.services import snapshots as snapshot_svc
from domain.policies import job_catalog
from domain.policies.snapshot_gates import (
    IMPLEMENTED,
    OWED_BY,
    SNAPSHOT_GATES,
    evaluated_gates,
    is_fully_gated,
)

MIGRATION = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "0021_er_m021_a_snapshot_says_exactly_what_a_run_may_read.py"
)

#: A work package id as `15-…:§14` writes them, with the optional sub-package
#: letter `ER-006B` uses. `OWED_BY` values are pointers at work somebody has to
#: do; free text would let the register fill up with prose that no backlog can be
#: reconciled against.
WORK_PACKAGE = re.compile(r"^(ER|FND|E2E)-\d{3}[A-Z]?$")


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("er_m021", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_policy_name_the_same_gates() -> None:
    """Both directions, and as an ORDERED tuple.

    A gate the CHECK accepts and this module does not name is a value that can
    reach `gates_evaluated` with nothing in the application able to describe what
    ran. The reverse — a gate the policy offers and the CHECK refuses — is a
    snapshot that fails at commit, after its members were resolved and the caller
    was told the build was under way. Order is asserted too, not because the
    CHECK cares, but because the tuple is a transcription of a five-item list in
    `14-…:345` and a silent reordering is the cheapest way for a re-typing to
    lose an entry and gain another.
    """
    assert tuple(_migration().SNAPSHOT_GATES) == tuple(SNAPSHOT_GATES)


def test_every_implemented_gate_is_a_gate_the_spec_names() -> None:
    """`IMPLEMENTED` may only contain gates the closed set contains.

    Compared as whole sets so a failure prints both sides. A gate implemented
    here but absent from `SNAPSHOT_GATES` is worse than an unimplemented one: the
    producer writes it into `gates_evaluated`, the CHECK rejects the row, and
    every snapshot build fails on a value the application is certain is correct.
    It is also a gate no reader of the spec can account for.
    """
    stray = sorted(IMPLEMENTED - set(SNAPSHOT_GATES))
    assert stray == [], f"IMPLEMENTED names gates the spec does not: {stray}"


def test_the_owed_register_is_exactly_the_unimplemented_gates() -> None:
    """`set(SNAPSHOT_GATES) - IMPLEMENTED == set(OWED_BY)`, both directions.

    Each direction catches a different lie. A gate that gains an implementation
    but keeps its `OWED_BY` entry leaves the register claiming a gap that has
    been closed, which trains a reader to disbelieve the rest of the list. A gate
    dropped from both — deleted from `IMPLEMENTED` without anyone recording who
    owes it — silently stops running while the register says everything absent is
    accounted for. Emptying `OWED_BY` is the definition of "snapshot creation is
    fully gated", so both halves have to stay true for that definition to mean
    anything.
    """
    assert set(OWED_BY) == set(SNAPSHOT_GATES) - IMPLEMENTED


def test_every_owed_gate_points_at_a_work_package() -> None:
    """A recorded gap has to name who owes it.

    The honest form of the reason is an id, because the gap is closed by a work
    package landing and not by an argument. Free text would let the register
    accumulate prose that no backlog can be reconciled against, and a reader
    could not tell an acknowledged gap from a forgotten one.
    """
    malformed = sorted((gate, owner) for gate, owner in OWED_BY.items() if not WORK_PACKAGE.match(owner))
    assert malformed == [], f"OWED_BY values that are not work-package ids: {malformed}"


def test_evaluated_gates_reports_exactly_what_is_implemented() -> None:
    """What the producer writes into `evidence_snapshots.gates_evaluated`.

    This is the ledger a consumer reads to decide whether it may bind a
    snapshot. If it ever reported a gate that is not in `IMPLEMENTED`, the
    snapshot would claim an inspection that no code performed — the artifact
    looks vetted and nothing examined it. If it under-reported, a consumer would
    refuse a snapshot that was in fact checked. Sortedness is part of the claim:
    two snapshots built from the same code must compare equal by inspection.
    """
    assert evaluated_gates() == sorted(IMPLEMENTED)


def test_a_snapshot_this_service_can_build_today_is_not_fully_gated() -> None:
    """False, today, for the ledger this service actually writes.

    ⚠️ This test is EXPECTED to be rewritten the day all five gates exist — at
    that point `evaluated_gates()` covers `SNAPSHOT_GATES` and the honest
    assertion becomes True. That is a deliberate, visible edit made by the work
    package that closes the last gap.

    Asserting True *today* would be asserting a fiction: three of the five gates
    have no subject matter in this service, so a snapshot that reported itself
    fully gated would be indistinguishable from one that five real gates
    examined. The False here is the same honesty that made the ledger a column
    rather than a comment.
    """
    assert is_fully_gated(evaluated_gates()) is False


def test_is_fully_gated_is_true_when_every_gate_ran() -> None:
    """Non-vacuity control for the test above.

    A function that returned False unconditionally would satisfy the previous
    assertion forever, and the ledger would keep reading as untrustworthy on the
    day it finally became trustworthy. Handing it the full set proves the
    predicate is reading its argument, so the False above is a measurement of
    this service's coverage rather than a property of the function.
    """
    assert is_fully_gated(SNAPSHOT_GATES) is True


def test_the_migration_and_the_service_name_the_same_member_kinds() -> None:
    """`_MEMBER_SOURCES` keys against `MEMBER_ENTITY_TYPES`, both directions.

    Compared as whole sets so a failure prints both sides. A kind the database
    accepts that the service cannot resolve is a member row nothing can pin,
    because `_resolve_member` has no table to read it out of — the caller gets a
    422 for a kind the schema advertises. The reverse is worse: a kind the
    service resolves and the CHECK refuses means the members are read, the
    manifest is hashed, and the insert fails at commit with the work already
    done. Order is not asserted; these are lookup keys, not a transcription.
    """
    assert set(snapshot_svc._MEMBER_SOURCES) == set(_migration().MEMBER_ENTITY_TYPES)


def test_the_member_grammar_is_a_urn_the_registry_resolves() -> None:
    """`MEMBER_SCHEMA_REF` is a RELEASED urn, not a URL and not an invention.

    `urn:datacompany:stable-identity:1#/$defs/schema_ref` constrains the field to
    `^urn:datacompany:[a-z][a-z0-9-]*:[1-9][0-9]*$`, so the first draft's per-kind
    HTTPS urls could not validate at all — every snapshot build raised a
    `SchemaValidationError` naming five innocent sibling fields before the
    guilty one, because a failing `allOf` branch contributes no annotations and
    `unevaluatedProperties: false` then rejects everything.

    Resolution is asserted rather than the shape alone: a well-formed urn that
    names no released document is the flow-ledger defect `research_contracts`
    0.4.0 was cut to repair, and it would pass a regex happily.
    """
    from research_contracts.canonical.registry import canonical_schemas

    ref = snapshot_svc.MEMBER_SCHEMA_REF
    assert ref.startswith("urn:datacompany:"), ref
    assert ref in canonical_schemas(), (
        f"{ref} is not a released document; a member would advertise a grammar no consumer can resolve"
    )


def test_the_snapshot_job_type_is_registered_and_no_longer_owed() -> None:
    """Registered in the catalogue AND absent from `job_catalog.OWED_BY`.

    Two halves of one claim. The registration half is what lets the row exist at
    all: a type the CHECK refuses makes every snapshot build fail at enqueue.

    The `OWED_BY` half is the half that rots quietly. That register exists to say
    a missing producer is deliberate; an entry naming a type that now HAS a
    producer — this module — makes the register describe a gap that is not there,
    and a reader who finds one stale entry learns to skim the list instead of
    trusting it. The register is only useful while every line in it is still
    true, so removing the entry is part of landing the producer, not cleanup
    afterwards.
    """
    assert snapshot_svc.JOB_TYPE in job_catalog.REGISTERED_JOB_TYPES
    assert snapshot_svc.JOB_TYPE not in job_catalog.OWED_BY, (
        f"{snapshot_svc.JOB_TYPE} has a producer but is still recorded as owed by "
        f"{job_catalog.OWED_BY.get(snapshot_svc.JOB_TYPE)}"
    )
