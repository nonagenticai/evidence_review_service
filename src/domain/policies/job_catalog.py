"""The registry of durable job types, and the state machine they move through.

`13-…:§7` specifies a durable job subsystem; `14-…` names the five operations
that must expose `GET /review-jobs/{job_id}` and `POST /review-jobs/{job_id}:cancel`
— "All non-terminal import, deduplication, prioritization, snapshot and export
jobs". `ER-M020` created the tables. This module is the register of what may go
in them, and it is a sibling of `event_catalog.py` in both shape and intent.

## Why four of the five have no producer yet

`FND-006` builds the substrate: the table, the claim, the lease, the reaper, the
cancel and the two endpoints. It does not convert the five existing synchronous
operations onto it — each of those is its own work package (`ER-002`, `ER-003`,
`ER-007`, `ER-008`, `ER-009`), and rewriting five call sites inside a foundation
change would make the foundation unreviewable and the conversions untestable.

Listing only what already has a producer would produce a catalogue that looked
complete. `OWED_BY` records each unbuilt type against the package that owes it,
so the gap is a statement somebody made rather than a thing nobody noticed —
the same reason `event_catalog.NOT_YET` exists.

`tests/unit/test_the_job_catalogue_is_the_registry.py` holds this module and the
migration's closed CHECK equal in **both** directions: a type the database
accepts and this module does not name is drift, and so is the reverse. It also
fails when a type listed in `OWED_BY` acquires a producer without being removed
from it, because a catalogue that keeps calling shipped work "owed" trains its
reader to skip the list.

## ⛔ A registered type is not a running worker

Nothing in this service claims these rows on a schedule. `jobs.claim_next` and
`jobs.reap_expired_leases` are library functions with a two-worker test behind
them; the process that calls them on a loop is deployment, and it does not exist
yet. Enqueuing a job today means the row is durable, readable and cancellable —
not that anything will pick it up. Every producer package must land its worker
entrypoint with its conversion, and `README.md` says so beside the endpoint.
"""

from __future__ import annotations

from typing import Final

#: The five operations `14-…` requires to expose the review-job endpoints.
#: Mirrors `REGISTERED_JOB_TYPES` in `alembic/versions/0020_…`, which is the
#: closed CHECK the database enforces. Deliberately NOT imported from there:
#: two independent spellings held equal by a test is what catches an edit to
#: one of them, where a single shared constant would make the CHECK and its
#: caller agree by construction even when both are wrong.
REGISTERED_JOB_TYPES: Final[tuple[str, ...]] = (
    "import",
    "deduplication",
    "prioritization",
    "evidence_snapshot",
    "export",
)

#: The job types with no producer in this service today, against the work
#: package that owes each. Emptying this dict is the definition of "the durable
#: job subsystem is fully adopted"; shrinking it is what each conversion does.
#:
#: `import` and `deduplication` run synchronously today (`imports.py`,
#: `deduplication.py`) and write result-only rows; converting them is `ER-002`
#: and `ER-003`. `prioritization` has no implementation at all.
#:
#: `evidence_snapshot` WAS here and is not any more: `ER-007` landed
#: `application/services/snapshots.py`, which enqueues one. That is what
#: shrinking this dict means, and
#: `tests/unit/test_the_snapshot_gate_ledger_is_the_registry.py` fires in the
#: other direction too — a type with a producer that is still listed as owed
#: trains a reader to skip the list, so leaving it here would have been worse
#: than never having written it.
OWED_BY: Final[dict[str, str]] = {
    "import": "ER-002",
    "deduplication": "ER-003",
    "prioritization": "ER-008",
    "export": "ER-009",
}

#: `13-…:§7.1`, verbatim from the diagram. `waiting` is a durable, externally
#: visible state. `cancel_requested` is NOT here — it is a timestamp on the row,
#: and the document is explicit that it "is not an extra public state".
STATES: Final[tuple[str, ...]] = (
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
)

#: "Terminal state is immutable" (`13-…:§7.1`). The database enforces this with
#: a trigger; this tuple is what the application checks before it bothers to try.
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled"})

#: Every transition the `§7.1` diagram draws, and no other. Mirrors
#: `LEGAL_TRANSITIONS` in `0020`, again as an independent spelling.
LEGAL_TRANSITIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "waiting"),
        ("running", "queued"),
        ("waiting", "queued"),
        ("waiting", "cancelled"),
    }
)

#: How an attempt ended. `lease_expired` is not a job state — the JOB returns to
#: `queued` — but it is the honest outcome of the attempt that lost its lease,
#: and without it the history cannot tell a crash apart from a failure.
ATTEMPT_OUTCOMES: Final[tuple[str, ...]] = ("succeeded", "failed", "cancelled", "lease_expired")


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def may_move(from_state: str, to_state: str) -> bool:
    """Is this a transition `§7.1` draws?

    A move to the state a job is already in is NOT a transition and is not
    listed: heartbeats, progress writes and a cancellation request all leave
    `state` alone, and treating those as self-edges would make the table read as
    though `succeeded -> succeeded` were legal, which is exactly the thing the
    terminal-immutability rule forbids.
    """
    return (from_state, to_state) in LEGAL_TRANSITIONS
