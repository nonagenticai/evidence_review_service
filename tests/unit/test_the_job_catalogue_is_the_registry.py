"""The job catalogue, the migration's closed CHECK and the state machine agree.

`13-…:§7` specifies a durable job subsystem, and two copies of what may go into
it exist in this service by design:

* `alembic/versions/0020_…` — the constants the migration renders into
  `ck_durable_jobs_type`, `ck_durable_jobs_state` and the transition trigger.
  That is the copy a caller cannot route around, because it is the database;
* `domain/policies/job_catalog` — the copy the application reads before it
  bothers to attempt a move, and the copy that records which types still have
  no producer.

Neither is derived from the other, and this file is the only thing that keeps
them equal. Importing one into the other would be tidier and strictly worse: a
single shared constant makes the CHECK and the code that calls it agree *by
construction*, which is a guarantee that survives both of them being wrong
together. The database would then accept precisely the mistake the application
makes, and nothing anywhere would notice. Two independently typed spellings held
equal by a test have the opposite property — they cannot drift quietly, because
editing either one alone turns this file red.

The state machine gets the same treatment for a sharper reason. A worker is by
definition the process that crashes or runs an older image than the row beside
it; `13-…:§7.1`'s transition table is enforced in a trigger precisely so it does
not depend on the fleet agreeing. The Python mirror exists to give a fast, local
answer — and a mirror is only useful while it is still a mirror.

Unit tests: no database, no fixtures. Everything asserted here is a constant.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from domain.policies.job_catalog import (
    ATTEMPT_OUTCOMES,
    LEGAL_TRANSITIONS,
    OWED_BY,
    REGISTERED_JOB_TYPES,
    STATES,
    TERMINAL_STATES,
    is_terminal,
    may_move,
)

MIGRATION = (
    Path(__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "0020_er_m020_a_long_job_survives_the_worker_that_ran_it.py"
)

#: A work package id as `15-…:§14` writes them. `OWED_BY` values are pointers at
#: work somebody has to do; a free-text excuse would let the register fill up
#: with prose that no backlog can be reconciled against.
WORK_PACKAGE = re.compile(r"^(ER|FND|E2E)-\d{3}$")


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("er_m020", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_policy_name_the_same_job_types() -> None:
    """Both directions, and as an ORDERED tuple.

    A type the database accepts and this module does not name is a row that can
    exist with nothing in the application able to describe it. The reverse — a
    type the catalogue offers and the CHECK refuses — is an enqueue that fails
    at commit, after the caller has been told the work was accepted. Order is
    asserted too, not because the CHECK cares, but because the tuple is a
    transcription of a five-item list in `14-…` and a silent reordering is the
    cheapest way for a re-typing to lose an entry and gain another.
    """
    assert tuple(_migration().REGISTERED_JOB_TYPES) == tuple(REGISTERED_JOB_TYPES)


def test_the_two_copies_of_the_state_vocabulary_agree() -> None:
    """`STATES`, `TERMINAL_STATES` and `ATTEMPT_OUTCOMES`, in both modules.

    If this goes red, one side has learned a state or an outcome the other
    cannot spell. The expensive direction is the application growing a state the
    CHECK refuses: every write of it is rejected at the database, so the job
    stops moving and the failure surfaces at a commit far from the edit.

    Compared as whole collections so a failure prints both sides. The two
    modules deliberately use different container types for terminality — the
    migration renders a tuple into SQL, the policy answers membership questions
    — so this compares the members, which is the claim being made.
    """
    migration = _migration()
    assert tuple(migration.STATES) == tuple(STATES)
    assert set(migration.TERMINAL_STATES) == set(TERMINAL_STATES)
    assert tuple(migration.ATTEMPT_OUTCOMES) == tuple(ATTEMPT_OUTCOMES)


def test_the_two_transition_tables_draw_the_same_diagram() -> None:
    """The edges themselves, as sets.

    The migration lists them in the order they are rendered into the trigger;
    the policy holds a frozenset. Order carries no meaning for a transition
    table, so this is the one constant compared without it — but the membership
    is exact in both directions. An edge in the trigger and not here means the
    application refuses a move the database would have allowed (a job that stops
    for no stated reason); an edge here and not in the trigger means the
    application attempts a move the database rejects at commit.
    """
    assert {tuple(edge) for edge in _migration().LEGAL_TRANSITIONS} == set(LEGAL_TRANSITIONS)


def test_no_transition_leaves_a_terminal_state() -> None:
    """Terminal state is immutable (`13-…:§7.1`), read off the table itself.

    The trigger enforces this at the database, but the transition table is the
    thing a reader consults to understand the machine. An edge out of
    `succeeded` would contradict the rule while looking perfectly ordinary, and
    it would do so in the one place where the contradiction reads as permission.
    """
    escapes = sorted(edge for edge in LEGAL_TRANSITIONS if edge[0] in TERMINAL_STATES)
    assert escapes == [], f"transitions originate from a terminal state: {escapes}"


def test_every_endpoint_of_every_transition_is_a_known_state() -> None:
    """Non-vacuity control for the two checks above.

    Holding two tables equal proves they agree; it does not prove either one
    talks about the states this service has. A typo — `runnning`, or a
    `cancel_requested` that crept in as though it were a state — produces an
    edge that can never fire and a machine with an unreachable corner, and every
    equality assertion above stays green.
    """
    known = set(STATES)
    unknown = sorted({name for edge in LEGAL_TRANSITIONS for name in edge} - known)
    assert unknown == [], f"transition endpoints that are not states: {unknown}"
    assert set(TERMINAL_STATES) <= known, f"terminal states not in STATES: {sorted(set(TERMINAL_STATES) - known)}"


def test_the_owed_register_names_only_registered_job_types() -> None:
    """`OWED_BY` is a statement about types that exist, not a wish list.

    An entry for a type nothing can enqueue is a debt against work nobody
    scheduled, and it makes the register — whose whole purpose is to say the gap
    is deliberate — describe a gap that is not there. Emptying `OWED_BY` is the
    definition of the subsystem being fully adopted, so an entry that can never
    be discharged means that definition can never be met.
    """
    stray = sorted(set(OWED_BY) - set(REGISTERED_JOB_TYPES))
    assert stray == [], f"OWED_BY names types the catalogue does not register: {stray}"


def test_every_owed_type_points_at_a_work_package() -> None:
    """A recorded gap has to name who owes it.

    `event_catalog.NOT_YET` demands a written reason; here the honest form of
    the reason is an id, because the gap is closed by a work package landing and
    not by an argument. Free text would let the register accumulate prose that
    no backlog can be reconciled against, and a reader could not tell an
    acknowledged gap from a forgotten one.
    """
    malformed = sorted((job_type, owner) for job_type, owner in OWED_BY.items() if not WORK_PACKAGE.match(owner))
    assert malformed == [], f"OWED_BY values that are not work-package ids: {malformed}"


def test_is_terminal_answers_for_every_state() -> None:
    """The predicate and the tuple are the same claim, stated twice.

    `is_terminal` is what the application consults before it tries a move at
    all. If it and `TERMINAL_STATES` ever part company, the cheap local check
    starts disagreeing with the trigger that actually decides, and the symptom
    is an attempted transition that the database refuses for a reason the
    application had already convinced itself did not apply.
    """
    answered = {state: is_terminal(state) for state in STATES}
    expected = {state: state in TERMINAL_STATES for state in STATES}
    assert answered == expected


@pytest.mark.parametrize("state", STATES)
def test_may_move_rejects_a_self_edge(state: str) -> None:
    """A move to the state a job is already in is not a transition.

    Heartbeats, progress writes and a cancellation request all leave `state`
    alone — `cancel_requested` is a timestamp, not a seventh state. If
    `may_move` accepted self-edges the table would read as though
    `succeeded -> succeeded` were legal, which is the precise thing terminal
    immutability forbids, and a reader would take the machine's own helper as
    permission to write a terminal row again.
    """
    assert may_move(state, state) is False, f"{state} -> {state} accepted as a transition"


def test_may_move_accepts_the_drawn_edges_and_nothing_else() -> None:
    """The whole cross product, so the negative half cannot be vacuous.

    Checking only that the nine drawn edges are accepted would pass for a
    function that returns `True` unconditionally. This enumerates every ordered
    pair of states and compares the accepted set against the table, so an edge
    the diagram does not draw — `queued -> succeeded`, skipping the attempt that
    would have recorded that anything ran — fails here rather than at a trigger
    in production.
    """
    accepted = {(a, b) for a in STATES for b in STATES if may_move(a, b)}
    assert accepted == set(LEGAL_TRANSITIONS)
