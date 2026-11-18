"""The five gates a snapshot must pass, and the two this service can actually run.

`ER-007` (`14-…:345`) requires snapshot creation to reject five things:
"unresolved critical proposals, conflicts, stale mandatory evidence, denied
rights and an uncommitted profile/protocol." This module is the register of
those five, and it is a sibling of `job_catalog.py` and `event_catalog.py` in
both shape and intent.

## Why only two of the five are implementable

Measured, not assumed. `unresolved_conflicts` and `uncommitted_protocol` have
subject matter here: there are conflict rows and there is a committed protocol
version. The other three have none at all. There is no `proposal_decisions`
table and no `ProposalBundle` contract — `event_catalog.NOT_YET` records that
same absence against `review.proposal.decided.v1`. There is no rights or licence
model anywhere in this service. And nothing gives mandatory evidence a
freshness horizon, so nothing can go stale against one.

## ⛔ Why the missing three are not written as passing no-ops

A gate over an absent concept cannot fail. Writing the three as no-op passes
would make every snapshot report five gates cleared, which is indistinguishable
from a snapshot that five real gates examined — the failure
`a-control-whose-premise-is-unmet-manufactures-evidence` names: the check
passes, the artifact looks fully vetted, and nothing was inspected. A reviewer
reading such a snapshot would have no way to discover the difference, because
the evidence the control produced is the same evidence a working control
produces.

Omitting them is weaker and honest. `OWED_BY` records each one against the work
package that owes it, so the gap is a statement somebody made rather than a
thing nobody noticed.

## The ledger is what a consumer reads

`evidence_snapshots.gates_evaluated` records, per snapshot, exactly which gates
ran. A consumer deciding whether it may bind a snapshot reads that array instead
of assuming five, so the answer stays correct on the day `ER-006B` lands a third
gate and every snapshot written before it keeps saying two. That is why the
migration made the ledger a COLUMN and not a comment.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: The five gates `14-…:345` names, in the document's order. Mirrors
#: `SNAPSHOT_GATES` in `alembic/versions/0021_…`, which is the closed set
#: `ck_evidence_snapshots_gate_ledger_is_honest` enforces. Deliberately NOT
#: imported from there: two independent spellings held equal by a test is what
#: catches an edit to one of them, where a single shared constant would make the
#: CHECK and its caller agree by construction even when both are wrong.
SNAPSHOT_GATES: Final[tuple[str, ...]] = (
    "unresolved_conflicts",
    "uncommitted_protocol",
    "unresolved_critical_proposals",
    "denied_rights",
    "stale_mandatory_evidence",
)

#: The gates that have subject matter in this service today, and are therefore
#: the only ones a producer may write into the ledger.
IMPLEMENTED: Final[frozenset[str]] = frozenset({"unresolved_conflicts", "uncommitted_protocol"})

#: The gates with no subject matter here, against the work package that owes
#: each. Emptying this dict is the definition of "snapshot creation is fully
#: gated"; shrinking it is what each package does. Each entry names the concrete
#: thing whose absence makes the gate unimplementable, so the claim can be
#: re-measured rather than believed.
OWED_BY: Final[dict[str, str]] = {
    #: `ER-006B`. There is no `proposal_decisions` table and no `ProposalBundle`
    #: contract, so no proposal has a criticality or a resolution state to read.
    #: `event_catalog.NOT_YET` records the same absence for
    #: `review.proposal.decided.v1`.
    "unresolved_critical_proposals": "ER-006B",
    #: `FND-011`. There is no rights, licence or embargo model in this service —
    #: no table, no column, no external client — so "denied" has no referent.
    "denied_rights": "FND-011",
    #: `ER-008`. Nothing marks evidence mandatory and nothing gives it a
    #: freshness horizon, so no row can be stale against anything.
    "stale_mandatory_evidence": "ER-008",
}


def evaluated_gates() -> list[str]:
    """What a producer writes into `evidence_snapshots.gates_evaluated`.

    Sorted, because the ledger is a set and a stable spelling makes two
    snapshots built from the same code compare equal by inspection.
    """
    return sorted(IMPLEMENTED)


def is_fully_gated(gates: Sequence[str]) -> bool:
    """Did every gate `14-…:345` names actually run for this snapshot?

    A downstream consumer deciding whether it may bind a snapshot calls this. It
    returns False for every snapshot this service can produce today, and that is
    the honest answer: three of the five gates did not run, and a consumer that
    needs all five must know that rather than infer it from a snapshot that
    simply did not complain.
    """
    return set(SNAPSHOT_GATES) <= set(gates)
