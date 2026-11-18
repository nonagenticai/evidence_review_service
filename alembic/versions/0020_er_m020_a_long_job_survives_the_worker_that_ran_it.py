"""ER-M020 durable jobs: a long job survives the worker that was running it.

Revision ID: 0020_er_m020
Revises: 0019_er_m019
Create Date: 2026-09-03

Foundation `FND-006` — "durable job mixins, claimant and reaper" (`13-…:§17`),
item 8 of the `15-…:§14` backlog. It sits BEFORE the outbox item this service
shipped last (`FND-007`, item 9); the backlog was taken out of order and this
migration puts the missing floor back under it.

## What is missing, measured on `origin/main` at `44e6cd7`

Every long-running operation in this service is synchronous inside the request.
`imports.py`, `deduplication.py`, `flow.py` and `exports.py` all run to
completion on the HTTP thread — `run_in_threadpool` moves blocking SQLAlchemy
off the event loop, but it does not outlive the connection. There is no job
table, no claim, no lease, no attempt history and no cancel.

The consequences are not hypothetical:

* `import_batches` and `deduplication_runs` are RESULT rows. They are written
  when the work has already finished, so a run that dies half-way leaves no row
  at all — the operation is not merely incomplete, it is **invisible**, which is
  the failure mode `a-lost-document-is-invisible` names.
* A caller who disconnects has no handle to ask what happened, and
  `14-…` requires one: "All non-terminal import, deduplication, prioritization,
  snapshot and export jobs expose `GET /review-jobs/{job_id}` and
  `POST /review-jobs/{job_id}:cancel`."
* `ER-007` (evidence snapshots) is specified as "an idempotent durable build
  job". It cannot be built on top of nothing, which is why it is the next unit
  and why this is the one before it.

## Why the state machine is in the DATABASE and not only in Python

`13-…:§7.1` says "Terminal state is immutable" and draws a transition diagram.
A worker is by definition the thing that crashes, gets partitioned, or runs an
older image than the one that wrote the row beside it. A transition table held
only in application code is enforced exactly as well as the oldest replica in
the fleet — and the row it corrupts is the row that says whether a scientific
run happened.

So the legal transitions are a trigger (`durable_jobs_transition`), terminality
is a trigger, and every co-ordinate of the state model that can be stated as a
CHECK is one:

| Invariant | Where |
|---|---|
| the six states, and no seventh | `ck_durable_jobs_state` |
| terminal ⇔ `terminal_at` is set | `ck_durable_jobs_terminal_at` |
| `running` ⇔ a worker holds a lease | `ck_durable_jobs_running_holds_lease` |
| `waiting` ⇔ a typed reason and a `reconcile_after` | `ck_durable_jobs_waiting_declares_why` |
| `failed` ⇔ an error code | `ck_durable_jobs_failure_names_itself` |
| attempts never exceed the policy | `ck_durable_jobs_attempt_within_policy` |
| only the drawn transitions | `trg_durable_jobs_transition` |
| a terminal row never changes again | same trigger |

`cancel_requested_at` is deliberately **not** a state, exactly as
`13-…:§7.1` requires: "Cancellation intent is a timestamped flag/history entry
while external `status` remains `queued`, `running` or `waiting`;
`cancel_requested` is not an extra public state." A seventh state would make
every consumer of `status` branch on a value that says nothing about whether the
work is still running, and "a result committed first retains its terminal
outcome" would become a race between two writers of the same column instead of a
flag that a terminal commit simply outruns.

## Cross-tenant discovery is granted ADDITIVELY, to one named role

`13-…:§5.1` excludes operational tables from tenant RLS "so a cross-tenant
worker/relay can discover work". `0019` declined that exclusion for
`domain_outbox` and said why: `tests/migration/test_migration_round_trip.py`
asserts `forced_rls == tables` over the whole schema, and opening the first hole
in it for a consumer that **did not exist** is a cost with no trade. It also
recorded the way out — "a second policy `TO evidence_review_relay USING (true)`
… `FND-007` adds one policy; it does not have to take one away."

This migration is the case that argument was reserving. The claimant is not
hypothetical here: it ships in the same change (`src/application/services/jobs.py`).
So `durable_jobs` and `job_attempts` keep `FORCE ROW LEVEL SECURITY` and the
ordinary tenant policy, and gain a second, additive policy scoped to a single
restricted role, `evidence_review_worker`:

    CREATE POLICY durable_jobs_worker ON durable_jobs
      TO evidence_review_worker USING (true) WITH CHECK (true);

Everything else in the schema still refuses that role — it is granted DML on
these two tables and nothing more — so "queue discovery" is exactly as wide as
it needs to be and not one table wider. `§5.1`'s requirement that the worker
"must derive the recorded tenant, start a tenant-bound domain transaction … and
set the tenant GUC before reading or mutating domain rows" is what
`tenant_id NOT NULL` on every row is for, and
`tests/integration/test_two_workers_and_a_reaper.py` proves the rebind and the
cross-tenant refusal rather than asserting them.

The policy is created only when the role exists, the same guard `0019._grant`
uses. A database that has never been provisioned with a worker role gets a
`NOTICE` and a queue only its own tenant can see, which is a safe default rather
than a silent one.

## Payload minimality is reused, not restated

`progress`, `result_summary` and an attempt's `resource_summary` all pass
through `er_payload_is_minimal(jsonb)`, the recursive 512-character guard `0019`
installed. `13-…:§7.3` — "Progress contains bounded structured facts and never
full licensed text or unbounded arrays" — is therefore a database fact for jobs
for the same reason it is one for events, and it is the SAME function, so the
two cannot drift into two different definitions of "minimal".

Large inputs and outputs are `artifact_ref` / `run_manifest_ref` strings. The
table holds the reference and never the bytes.

## `job_attempts` is append-only ONCE CLOSED, not append-only outright

An attempt row is written when a worker claims and updated exactly once, when it
ends. `0019`'s blanket `BEFORE UPDATE OR DELETE` trigger would forbid the second
half of its own life, so `job_attempts_seal` refuses `DELETE` always and refuses
`UPDATE` only when `OLD.ended_at IS NOT NULL`. That is what "immutable
execution-attempt history" (`13-…:§5.2`) means for a row that has to be able to
finish: history is what has already happened.

## Registered job types are a closed set, and the empty ones say who owes them

`ck_durable_jobs_registered_type` pins `job_type` to the five operations
`14-…` names. Four of the five have no producer yet — this migration builds the
substrate, not the callers — and they are registered anyway, mirrored by
`src/domain/policies/job_catalog.py`, which records the work package that owes
each. A registry naming only what already exists cannot tell a reader that the
rest are owed; `tests/unit/test_the_job_catalogue_is_the_registry.py` fails in
both directions, so a producer with no registered type and a registered type
whose owing package has shipped are both loud.

The names carry the estate's regulatory denylist for the same reason
`0019.REGISTERED_EVENTS` does: nothing in this service may quietly acquire a
name that asserts regulatory or normative authority the ADR has not granted.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020_er_m020_durable_jobs"
down_revision = "0019_er_m019_domain_outbox"
branch_labels = None
depends_on = None

TENANT_GUC = "app.current_tenant"
_TENANT_COMPARAND = f"current_setting('{TENANT_GUC}', true)::uuid"
HASH_SHAPE = "^sha256:[0-9a-f]{64}$"

#: The restricted principal `13-…:§5.1` calls "a distinct restricted
#: worker/relay role". It may claim from the two operational tables below across
#: tenants and may touch nothing else in the schema.
WORKER_ROLE = "evidence_review_worker"

#: `13-…:§7.1`, verbatim from the diagram. `waiting` is externally visible;
#: `cancel_requested` is NOT here, deliberately — it is a timestamp, not a state.
STATES: tuple[str, ...] = ("queued", "running", "waiting", "succeeded", "failed", "cancelled")

#: "Terminal state is immutable" (`13-…:§7.1`). Mirrored by
#: `src/domain/policies/job_catalog.py.TERMINAL_STATES`.
TERMINAL_STATES: tuple[str, ...] = ("succeeded", "failed", "cancelled")

#: Every transition the `§7.1` diagram draws, and no other. `running -> queued`
#: is the lease-expiry/retry edge; `running -> waiting` is the dependency stall.
#: A terminal state appears only as a destination.
LEGAL_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("queued", "running"),
    ("queued", "cancelled"),
    ("running", "succeeded"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("running", "waiting"),
    ("running", "queued"),
    ("waiting", "queued"),
    ("waiting", "cancelled"),
)

#: The five operations `14-…` requires to expose `GET /review-jobs/{job_id}`
#: and `POST /review-jobs/{job_id}:cancel`. Mirrored by
#: `src/domain/policies/job_catalog.py`.
REGISTERED_JOB_TYPES: tuple[str, ...] = (
    "import",
    "deduplication",
    "prioritization",
    "evidence_snapshot",
    "export",
)

#: How an attempt ended. `lease_expired` is not a job state — the JOB goes back
#: to `queued` — but it is the honest outcome of the attempt that lost the lease,
#: and without it the history cannot distinguish a crash from a failure.
ATTEMPT_OUTCOMES: tuple[str, ...] = ("succeeded", "failed", "cancelled", "lease_expired")

#: Read by `tests/conftest.py` — see `0017.APPEND_ONLY_TABLES`. `job_attempts`
#: is sealed on close rather than on insert and is listed separately below.
APPEND_ONLY_TABLES: tuple[str, ...] = ()

#: Sealed once `ended_at` is set. The distinction from `APPEND_ONLY_TABLES` is
#: the point: a row that must be able to finish cannot be immutable from birth.
SEAL_ON_CLOSE_TABLES: tuple[str, ...] = ("job_attempts",)

#: Read by `tests/conftest.py`, which re-asserts these REVOKEs after its blanket
#: GRANT. Neither table is append-only — a job is UPDATEd on every transition —
#: but neither may ever be DELETEd, so they need their own bucket. Skipping this
#: is not cosmetic: the harness would restore `DELETE` and the no-delete trigger
#: would then be tested for the WRONG reason, answering `restrict_violation`
#: from the trigger where production answers `42501` from the grant.
DELETE_REVOKED_TABLES: tuple[str, ...] = ("durable_jobs", "job_attempts")

_TOKEN_SHAPE = r"^[a-z][a-z0-9_]*$"
_WORKER_ID_SHAPE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$"
_REGULATORY_DENYLIST = "regulatory|legal_|normative|consolidation"

#: Mirrors `0019.MINIMAL_FACT_MAX`. Applied here to free-text error detail, which
#: `er_payload_is_minimal` cannot reach because it is a column and not JSON.
MINIMAL_FACT_MAX = 512


def _transition_guard() -> str:
    """Legal transitions and terminal immutability, as one BEFORE UPDATE trigger.

    Two refusals, and the order matters. Terminality is checked FIRST so that a
    late cancel against a job that already succeeded is refused by the rule that
    explains it — `13-…:§7.3`, "cancellation that arrives after terminal commit
    returns the terminal outcome" — rather than by the transition table, which
    would report the same event as an illegal edge and send a reader looking for
    a bug in the state machine.

    A no-op UPDATE that does not change `state` is allowed through: heartbeats,
    progress and `cancel_requested_at` all take that path, and they are the
    normal traffic on this table.
    """
    legal = " OR ".join(f"(OLD.state = '{src}' AND NEW.state = '{dst}')" for src, dst in LEGAL_TRANSITIONS)
    terminal = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
    return f"""
        CREATE OR REPLACE FUNCTION durable_jobs_transition() RETURNS trigger AS $$
        BEGIN
          IF OLD.state IN ({terminal}) THEN
            RAISE EXCEPTION
              'durable_jobs % is terminal in state % and cannot be modified; '
              'a cancellation that arrives after a terminal commit returns that outcome',
              OLD.job_id, OLD.state
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.state IS DISTINCT FROM OLD.state AND NOT ({legal}) THEN
            RAISE EXCEPTION 'durable_jobs % cannot move from % to %',
              OLD.job_id, OLD.state, NEW.state
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.cancel_requested_at IS NULL AND OLD.cancel_requested_at IS NOT NULL THEN
            RAISE EXCEPTION 'durable_jobs %: cancellation intent cannot be withdrawn', OLD.job_id
              USING ERRCODE = 'restrict_violation';
          END IF;

          IF NEW.attempt < OLD.attempt THEN
            RAISE EXCEPTION 'durable_jobs %: attempt counter cannot go backwards', OLD.job_id
              USING ERRCODE = 'restrict_violation';
          END IF;

          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_durable_jobs_transition
          BEFORE UPDATE ON durable_jobs
          FOR EACH ROW EXECUTE FUNCTION durable_jobs_transition();

        CREATE OR REPLACE FUNCTION durable_jobs_no_delete() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'durable_jobs is not deletable: a job that can be erased '
                          'cannot answer whether a scientific run happened'
            USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_durable_jobs_no_delete
          BEFORE DELETE ON durable_jobs
          FOR EACH ROW EXECUTE FUNCTION durable_jobs_no_delete();
    """


def _attempt_seal() -> str:
    """An attempt is immutable once it has ended, and never deletable.

    See the module docstring: `0019`'s append-only trigger fires on every UPDATE,
    which would forbid the single legitimate update in this row's life — the one
    that closes it.
    """
    return """
        CREATE OR REPLACE FUNCTION job_attempts_seal() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'job_attempts is append-only: execution history is not erasable'
              USING ERRCODE = 'restrict_violation';
          END IF;
          IF OLD.ended_at IS NOT NULL THEN
            RAISE EXCEPTION 'job_attempts % ended at % and is sealed', OLD.attempt_id, OLD.ended_at
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_job_attempts_seal
          BEFORE UPDATE OR DELETE ON job_attempts
          FOR EACH ROW EXECUTE FUNCTION job_attempts_seal();
    """


def _tenant_rls(table: str) -> str:
    """The same policy every other table in this schema carries.

    Verbatim from `0019` rather than shared, for the reason that migration gives:
    reading one migration should tell you what it did, and an edit to a later
    policy must not silently change an earlier one's meaning on replay.
    """
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_tenant ON {table}
          USING (tenant_id = {_TENANT_COMPARAND})
          WITH CHECK (tenant_id = {_TENANT_COMPARAND});
    """


def _worker_policy(table: str) -> str:
    """Cross-tenant queue discovery, granted ADDITIVELY to one restricted role.

    `13-…:§5.1`. Postgres ORs its permissive policies, so this widens the table
    for `evidence_review_worker` and for nobody else; the tenant policy above is
    untouched and still governs the application role.
    """
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
            EXECUTE 'CREATE POLICY {table}_worker ON {table} TO {WORKER_ROLE} '
                    'USING (true) WITH CHECK (true)';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {table} TO {WORKER_ROLE}';
          ELSE
            RAISE NOTICE 'role {WORKER_ROLE} is absent; cross-tenant claim policy on '
                         '{table} skipped — the queue is visible per-tenant only.';
          END IF;
        END
        $$;
    """


def _grant(table: str, privileges: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            GRANT {privileges} ON {table} TO evidence_review_app;
          ELSE
            RAISE NOTICE 'role evidence_review_app is absent; DML grant on {table} skipped.';
          END IF;
        END
        $$;
    """


def _revoke_delete(table: str) -> str:
    return f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidence_review_app') THEN
            REVOKE DELETE ON {table} FROM evidence_review_app;
          END IF;
        END
        $$;
    """


def upgrade() -> None:
    states = ", ".join(f"'{s}'" for s in STATES)
    terminal = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
    registered = ", ".join(f"'{t}'" for t in REGISTERED_JOB_TYPES)
    outcomes = ", ".join(f"'{o}'" for o in ATTEMPT_OUTCOMES)

    op.create_table(
        "durable_jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # `§5.1`: the worker DERIVES the tenant from the row and rebinds. A queue
        # row that cannot say whose work it is holding cannot be executed safely.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        # "resource-scoped job read" (`14-…` trace table). Every job in this
        # service belongs to exactly one review, and the read endpoint authorises
        # against that review rather than against the job.
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reviews.review_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        # `§7.3`: "command and input-snapshot hashes". The command hash is what
        # makes a resubmission recognisable; the input snapshot hash is what makes
        # a *repeat* of it distinguishable from a *rerun* of it.
        sa.Column("command_hash", sa.Text(), nullable=False),
        sa.Column("input_snapshot_hash", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        # `§7.3`: "profile, pack and capability versions".
        sa.Column("profile_version", sa.Text(), nullable=True),
        sa.Column("capability_version", sa.Text(), nullable=True),
        sa.Column("pack_refs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        # `§7.3`: "correlation, causation, workflow and originating actor refs".
        # The actor is an identity REFERENCE. `§9.3` — a job "never retains a
        # human credential" — is why there is no token column here and never will be.
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workflow_ref", sa.Text(), nullable=True),
        sa.Column("originating_subject_id", sa.Text(), nullable=True),
        sa.Column("originating_actor_role", sa.Text(), nullable=True),
        # `§7.2`: "A claim records worker ID, lease expiry, attempt number".
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        # `§7.1`: intent, not a state.
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.Text(), nullable=True),
        # `§7.1` waiting: "a typed reason, dependency/provider attempt reference
        # and `reconcile_after`".
        sa.Column("waiting_reason", sa.Text(), nullable=True),
        sa.Column("waiting_dependency_ref", sa.Text(), nullable=True),
        sa.Column("reconcile_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_state", sa.Text(), nullable=True),
        # `§7.3`: bounded structured facts, and refs rather than bytes.
        sa.Column("progress", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("result_artifact_ref", sa.Text(), nullable=True),
        sa.Column("run_manifest_ref", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"state IN ({states})", name="ck_durable_jobs_state"),
        sa.CheckConstraint(f"job_type IN ({registered})", name="ck_durable_jobs_registered_type"),
        sa.CheckConstraint(f"job_type ~ '{_TOKEN_SHAPE}'", name="ck_durable_jobs_type_shape"),
        sa.CheckConstraint(
            f"job_type !~ '{_REGULATORY_DENYLIST}'",
            name="ck_durable_jobs_type_not_regulatory",
        ),
        # Terminal ⇔ stamped. Both directions: a terminal row with no timestamp
        # cannot be ordered against its siblings, and a non-terminal row that
        # claims a terminal time is a row whose own history disagrees with it.
        sa.CheckConstraint(
            f"(state IN ({terminal})) = (terminal_at IS NOT NULL)",
            name="ck_durable_jobs_terminal_at",
        ),
        # `running` ⇔ somebody holds a lease. The reverse half is what makes a
        # reaper's job decidable: a lease on a row that is not running is not a
        # lease, it is a leak.
        sa.CheckConstraint(
            "(state = 'running') = (worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_durable_jobs_running_holds_lease",
        ),
        sa.CheckConstraint(
            "(state = 'waiting') = (waiting_reason IS NOT NULL AND reconcile_after IS NOT NULL)",
            name="ck_durable_jobs_waiting_declares_why",
        ),
        # A failure that does not name itself cannot be triaged, and `succeeded`
        # carrying an error code is a row that will be believed by whichever
        # column the reader happens to look at.
        sa.CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)",
            name="ck_durable_jobs_failure_names_itself",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND attempt >= 0 AND attempt <= max_attempts",
            name="ck_durable_jobs_attempt_within_policy",
        ),
        sa.CheckConstraint(
            f"worker_id IS NULL OR worker_id ~ '{_WORKER_ID_SHAPE}'",
            name="ck_durable_jobs_worker_id_shape",
        ),
        sa.CheckConstraint(f"command_hash ~ '{HASH_SHAPE}'", name="ck_durable_jobs_command_hash_shape"),
        sa.CheckConstraint(
            f"input_snapshot_hash IS NULL OR input_snapshot_hash ~ '{HASH_SHAPE}'",
            name="ck_durable_jobs_input_hash_shape",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(progress) = 'object' AND er_payload_is_minimal(progress)",
            name="ck_durable_jobs_progress_is_minimal",
        ),
        sa.CheckConstraint(
            "result_summary IS NULL OR "
            "(jsonb_typeof(result_summary) = 'object' AND er_payload_is_minimal(result_summary))",
            name="ck_durable_jobs_result_is_minimal",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(pack_refs) = 'array' AND er_payload_is_minimal(pack_refs)",
            name="ck_durable_jobs_pack_refs_is_minimal",
        ),
        sa.CheckConstraint(
            f"error_detail IS NULL OR length(error_detail) <= {MINIMAL_FACT_MAX}",
            name="ck_durable_jobs_error_detail_is_bounded",
        ),
        # `§7.3`: cancellation is "input-hash/idempotency scoped". A cancel that
        # was never requested cannot have an author, and an author with no
        # timestamp cannot be ordered against a terminal commit.
        sa.CheckConstraint(
            "(cancel_requested_at IS NULL) = (cancel_requested_by IS NULL)",
            name="ck_durable_jobs_cancel_names_its_author",
        ),
    )

    # `13-…:§6.1` scopes idempotency per tenant. Two submissions of the same
    # command for the same review are the SAME job — this is the constraint that
    # makes `POST …/evidence-snapshots` "idempotent" (`14-…:ER-007`) rather than
    # a second run of a scientific build. Partial on `idempotency_key IS NOT NULL`
    # so a job submitted without one is still allowed, and is not silently
    # deduplicated against another job that also omitted it.
    op.create_index(
        "uq_durable_jobs_idempotency",
        "durable_jobs",
        ["tenant_id", "review_id", "job_type", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # The claim query's index, in its exact order: a worker asks for the oldest
    # runnable row of the types it can execute. `state` first because the queue
    # is overwhelmingly terminal rows after a week, and a scan that reads those
    # is a scan that gets slower every day the service succeeds.
    op.create_index(
        "ix_durable_jobs_claimable",
        "durable_jobs",
        ["state", "job_type", "queued_at"],
        postgresql_where=sa.text("state IN ('queued', 'waiting')"),
    )
    # The reaper's index. Only `running` rows have a lease, so the partial
    # predicate is the whole population it ever reads.
    op.create_index(
        "ix_durable_jobs_lease_expiry",
        "durable_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("state = 'running'"),
    )
    # The read endpoint's authorisation path: job → review → tenant.
    op.create_index("ix_durable_jobs_review", "durable_jobs", ["tenant_id", "review_id", "queued_at"])

    op.create_table(
        "job_attempts",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("durable_jobs.job_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Denormalised from the job so the worker policy and the tenant policy can
        # both be expressed on this table directly. A join-derived tenant in an
        # RLS predicate is a policy whose correctness depends on another table's
        # policy, which is how `present-to-one-side-absent-to-the-other` happens.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("capability_version", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("resource_summary", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_ordinal"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_job_attempts_ordinal_positive"),
        sa.CheckConstraint(f"outcome IS NULL OR outcome IN ({outcomes})", name="ck_job_attempts_outcome"),
        # An attempt has ended exactly when it has an outcome. Either half alone
        # describes a row that is neither running nor finished.
        sa.CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_job_attempts_ended_has_outcome"),
        sa.CheckConstraint(
            "(outcome = 'failed') = (error_code IS NOT NULL)",
            name="ck_job_attempts_failure_names_itself",
        ),
        sa.CheckConstraint(f"worker_id ~ '{_WORKER_ID_SHAPE}'", name="ck_job_attempts_worker_id_shape"),
        sa.CheckConstraint(
            f"error_detail IS NULL OR length(error_detail) <= {MINIMAL_FACT_MAX}",
            name="ck_job_attempts_error_detail_is_bounded",
        ),
        sa.CheckConstraint(
            "resource_summary IS NULL OR "
            "(jsonb_typeof(resource_summary) = 'object' AND er_payload_is_minimal(resource_summary))",
            name="ck_job_attempts_resource_summary_is_minimal",
        ),
    )
    op.create_index("ix_job_attempts_job", "job_attempts", ["job_id", "attempt_number"])

    for table in ("durable_jobs", "job_attempts"):
        op.execute(_tenant_rls(table))
        op.execute(_grant(table, "SELECT, INSERT, UPDATE"))
        op.execute(_revoke_delete(table))
        op.execute(_worker_policy(table))

    op.execute(_transition_guard())
    op.execute(_attempt_seal())


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_job_attempts_seal ON job_attempts")
    op.execute("DROP FUNCTION IF EXISTS job_attempts_seal()")
    op.execute("DROP TRIGGER IF EXISTS trg_durable_jobs_no_delete ON durable_jobs")
    op.execute("DROP FUNCTION IF EXISTS durable_jobs_no_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_durable_jobs_transition ON durable_jobs")
    op.execute("DROP FUNCTION IF EXISTS durable_jobs_transition()")
    for table in ("job_attempts", "durable_jobs"):
        op.execute(f"DROP POLICY IF EXISTS {table}_worker ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
    op.drop_index("ix_job_attempts_job", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_index("ix_durable_jobs_review", table_name="durable_jobs")
    op.drop_index("ix_durable_jobs_lease_expiry", table_name="durable_jobs")
    op.drop_index("ix_durable_jobs_claimable", table_name="durable_jobs")
    op.drop_index("uq_durable_jobs_idempotency", table_name="durable_jobs")
    op.drop_table("durable_jobs")
