"""Shared pytest configuration.

Two things live here, and they are the same argument stated twice:

* `_provision_runtime_role` makes the isolation suites *mean* something, by
  demoting the connection to a non-owner role;
* the isolation gate at the bottom of this file makes them *run*, by failing the
  session when they did not.

## Why this file provisions a database role

Every isolation guarantee in this service — tenant RLS and the five blinding
layers — is enforced by Postgres against a **non-owner, non-superuser** runtime
role. A superuser bypasses row-level security entirely, so a suite run as one
does not merely test less: it reports green while the policies do nothing at
all.

CI hands us a single superuser (`ci_test`) and runs `alembic upgrade head` as it,
which is correct — migrations need ownership. But if the tests then connect as
that same role, `test_an_unbound_tenant_sees_nothing` sees every row, and the
blinding suite passes for the wrong reason.

So before any engine is built, this module:

* keeps the inherited connection as ``MIGRATION_DATABASE_URL`` — the owner;
* creates `evidence_review_app` (`NOSUPERUSER`, non-owner) if the connected role
  is privileged enough to do so, and grants it `CONNECT`, `USAGE` and DML;
* repoints ``DATABASE_URL`` at that role.

The local dev database is set up the same way, so the two environments agree.
When the connected role cannot create a role — a locked-down database — the URL
is left alone and `tests/integration/test_rls_foundation.py` is the thing that
notices, because it asserts the runtime role *cannot* disable a policy and a
superuser can.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("ENVIRONMENT", "test")

APP_ROLE = "evidence_review_app"
APP_PASSWORD = "app_test_password"  # nosec B105 - a local/CI role, never a production credential


def _swap_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _privileges_revoked_by_0015() -> dict[str, str]:
    """`{table: "UPDATE, DELETE"}` read off migration `0015` itself.

    Loaded by path because the module name starts with a digit and so cannot be
    imported by `from alembic.versions...` — `importlib.util.spec_from_file_location`
    is the only way to reach it, and reaching it is the point: the alternative is
    a hand-copied list that drifts toward granting too much and takes the
    privilege half of the guard with it, silently, in the one environment where
    it is checked.

    Returns empty on any failure. A harness that cannot read the authority must
    not invent one — the tests that assert `42501` then fail, which is the
    correct outcome and names itself.
    """
    import importlib.util

    path = Path(__file__).parent.parent / "alembic" / "versions" / "0015_er_m015_the_ledger_inputs_are_append_only.py"
    try:
        spec = importlib.util.spec_from_file_location("_ers_migration_0015", path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        full = tuple(module.FULLY_GUARDED) + tuple(module.NEWLY_GUARDED_FULL)
        return {t: "UPDATE, DELETE" for t in full} | {t: "DELETE" for t in module.DELETE_ONLY}
    except Exception as exc:  # noqa: BLE001 - see the docstring: silence here is the failure mode
        print(
            f"conftest: could not read the REVOKE list from {path.name}: {type(exc).__name__}: {exc}. "
            "The blanket GRANT will stand, so the runtime role keeps UPDATE/DELETE on the ledger "
            "inputs and test_the_runtime_role_is_refused_by_the_grant will fail with 23001 rather "
            "than 42501. That is an environment problem, not a product one.",
            file=sys.stderr,
        )
        return {}


#: Migrations that export an `APPEND_ONLY_TABLES` tuple (revoking
#: `UPDATE, DELETE`) and/or a `DELETE_REVOKED_TABLES` tuple (revoking `DELETE`
#: only).
#:
#: The second shape arrived with `0020`. `durable_jobs` and `job_attempts` are
#: not append-only — a job row is UPDATEd on every state change and an attempt
#: row is UPDATEd once, when it closes — but neither may ever be DELETEd, so a
#: single "append-only" bucket could not describe them without either granting
#: back the DELETE or taking away the UPDATE the subsystem runs on.
#:
#: `0015` predates the convention and keeps its own three-constant shape above.
#: Everything since declares one tuple, so this list is the only thing a new
#: append-only migration has to join — and joining it is not optional: the
#: blanket GRANT below restores whatever the migrations revoked, so a package
#: that skips this keeps `UPDATE`/`DELETE` on the runtime role and its append-only
#: tests pass for the WRONG reason (`23001` from the trigger rather than `42501`
#: from the grant). CI caught that on `ER-006`'s first run, across all 11 tables.
_APPEND_ONLY_MIGRATIONS: tuple[str, ...] = (
    "0017_er_m017_a_research_object_has_identity.py",
    "0018_er_m018_accepted_evidence_names_its_basis.py",
    "0019_er_m019_domain_outbox_commits_with_the_state.py",
    "0020_er_m020_a_long_job_survives_the_worker_that_ran_it.py",
    "0021_er_m021_a_snapshot_says_exactly_what_a_run_may_read.py",
    "0022_er_m022_an_event_can_finally_leave_the_service.py",
    "0025_er_m025_an_artifact_ref_is_never_a_storage_key.py",
)


def _privileges_revoked_by_append_only_migrations() -> dict[str, str]:
    """`{table: "UPDATE, DELETE"}` read off each migration's own constant.

    Imported, never retyped, for the reason `0015`'s block below gives: a
    hand-maintained second copy drifts silently in the direction of granting too
    much, and it does so in the one environment where the privilege half of the
    guard is checked.
    """
    import importlib.util

    versions = Path(__file__).parent.parent / "alembic" / "versions"
    revoked: dict[str, str] = {}
    for filename in _APPEND_ONLY_MIGRATIONS:
        path = versions / filename
        try:
            spec = importlib.util.spec_from_file_location(f"_ers_migration_{filename[:4]}", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            revoked |= {table: "UPDATE, DELETE" for table in getattr(module, "APPEND_ONLY_TABLES", ())}
            revoked |= {table: "DELETE" for table in getattr(module, "DELETE_REVOKED_TABLES", ())}
        except Exception as exc:  # noqa: BLE001 - loud, not fatal; see the sibling above
            print(
                f"conftest: could not read the revoke declarations from {filename}: "
                f"{type(exc).__name__}: {exc}. The blanket GRANT will stand for its tables.",
                file=sys.stderr,
            )
    return revoked


def _provision_runtime_role() -> None:
    owner_url = os.environ.get("DATABASE_URL", "")
    if not owner_url.startswith("postgresql"):
        return

    # `DATABASE_URL` may already point at the app role (local dev), in which case
    # `MIGRATION_DATABASE_URL` is set and there is nothing to do.
    if os.environ.get("MIGRATION_DATABASE_URL"):
        return

    try:
        from sqlalchemy import create_engine, text
    except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency
        return

    engine = create_engine(owner_url, future=True, connect_args={"prepare_threshold": 0})
    try:
        with engine.connect() as conn:
            privileged = conn.execute(
                text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
            ).scalar()
            if not privileged:
                # LOUD. This return used to be silent, and the consequence is not
                # "a bit less coverage": the suite then runs as whatever role
                # `DATABASE_URL` names, and a superuser BYPASSES row-level
                # security entirely. `tests/security/` reports on policies that
                # are doing nothing.
                #
                # Measured: 80 pass with the runtime role provisioned; 7 FAIL as
                # the superuser (4 in `test_rls_foundation`, 3 in
                # `test_blinding_layers`). So the backstop does fire — but those
                # seven reds read as a product regression unless something says
                # otherwise, and the person reading them is usually not the
                # person who changed the database URL.
                print(
                    f"conftest: NOT provisioning {APP_ROLE} — the role in DATABASE_URL is neither "
                    "superuser nor createrole, so it cannot create one. The suite will run as that "
                    "role. If it is a SUPERUSER, row-level security is bypassed and every test in "
                    "tests/security/ is measuring nothing; expect test_rls_foundation.py to fail, "
                    "and read that as an environment problem, not a product one.",
                    file=sys.stderr,
                )
                return
            database = conn.execute(text("SELECT current_database()")).scalar_one()

        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                        CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'
                          NOSUPERUSER NOCREATEDB NOCREATEROLE;
                      ELSE
                        ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'
                          NOSUPERUSER NOCREATEDB NOCREATEROLE;
                      END IF;
                    END
                    $$;
                    """  # nosec B608 - constant identifiers, no caller input
                )
            )
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {APP_ROLE}'))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            # DML only, and explicitly no ownership: the runtime role must remain
            # structurally unable to `ALTER TABLE ... DISABLE ROW LEVEL SECURITY`.
            conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
                )
            )
            # ...EXCEPT where a migration deliberately took a privilege away.
            #
            # The blanket GRANT above runs AFTER `alembic upgrade head`, so it
            # silently restored `UPDATE, DELETE ON review_stages` that `0011`
            # revokes — in every test environment. The privilege half of that
            # guard was therefore never in force while the suite ran, and a
            # regression that dropped the REVOKE would have gone unnoticed here.
            #
            # Re-asserted rather than removed from the blanket grant: this list
            # is a harness convenience and the migration is the authority, so the
            # harness gives way to it. Guarded on the table existing, because
            # this runs before the schema does on a first-ever run.
            #
            # `UPDATE` ONLY, since `0013`. `0011` revoked both, and `0013` gives
            # `DELETE` back — a re-lock inside the correction window IS a delete,
            # performed by this role — with `review_stages_append_only()` made
            # conditional so the guarantee rests on the trigger instead of the
            # grant. Re-revoking `DELETE` here would leave the harness enforcing a
            # privilege the schema no longer takes away, and the correction-window
            # tests would fail as `InsufficientPrivilege` while the product was
            # correct. `UPDATE` is still revoked, exactly as `0011` left it.
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                      IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'review_stages') THEN
                        REVOKE UPDATE ON review_stages FROM """
                    + APP_ROLE
                    + """;
                      END IF;
                    END $$;
                    """  # nosec B608 - constant identifier
                )
            )

            # ...and the same for `0015`, which revokes on SIXTEEN tables.
            #
            # `review_stages` above is not a special case, it was the only case
            # anybody had hit. The blanket GRANT restores whatever ANY migration
            # revoked, so every new REVOKE silently loses its privilege half in
            # this environment until it is re-asserted here.
            #
            # Measured before this block existed: the runtime role's UPDATE on
            # `import_rows` was refused with `23001` (the trigger) instead of
            # `42501` (the grant), on all three newly-guarded tables.
            # `test_the_runtime_role_is_refused_by_the_grant` says what that
            # means -- "the trigger caught it and the GRANT was never revoked,
            # which is half the guard missing". The trigger stops the owner too,
            # so the half that was still working is the half that was never the
            # point of the REVOKE.
            #
            # IMPORTED, not retyped: the migration is the authority, and a second
            # hand-maintained copy of a sixteen-table list is a copy that drifts
            # silently in the direction of granting too much. `0011`/`0013` are
            # left as the literal above because their story is a one-table
            # exception with a documented `DELETE` carve-out.
            revoked = _privileges_revoked_by_0015() | _privileges_revoked_by_append_only_migrations()
            for table, privileges in sorted(revoked.items()):
                conn.execute(
                    text(
                        "DO $$ BEGIN "
                        f"IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = '{table}') THEN "
                        f"REVOKE {privileges} ON {table} FROM {APP_ROLE}; "
                        "END IF; END $$;"  # nosec B608 - identifiers from the migration module
                    )
                )
    except Exception as exc:  # noqa: BLE001 - a provisioning failure must be loud, not fatal
        # Not fatal, deliberately: a suite that refuses to start because it could
        # not provision a role is worse than one that starts and tells you the
        # policies are unenforced. But it must SAY which of the two happened —
        # the failure this print exists for is somebody reading seven red
        # blinding tests and looking for the bug in the blinding code.
        print(
            f"conftest: could NOT provision {APP_ROLE}: {type(exc).__name__}: {exc}\n"
            "conftest: the suite will run as the role in DATABASE_URL. If that is a superuser, "
            "row-level security is bypassed and tests/security/ certifies nothing; "
            "test_rls_foundation.py failing is the EXPECTED symptom of that, not a product defect.",
            file=sys.stderr,
        )
        return
    finally:
        engine.dispose()

    os.environ["MIGRATION_DATABASE_URL"] = owner_url
    os.environ["DATABASE_URL"] = _swap_credentials(owner_url, APP_ROLE, APP_PASSWORD)


_provision_runtime_role()


# ==============================================================================
# The isolation gate
# ==============================================================================
# Twenty-eight test files in this repository open with the same three lines:
#
#     pytestmark = pytest.mark.skipif(
#         not DB_URL.startswith("postgresql"),
#         reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
#     )
#
# Each is individually correct — a test that needs a database cannot run without
# one. Collectively they are a hole, because the guard is conditioned on the very
# precondition whose absence is the failure it exists to catch. Measured on
# c036c3a with the database taken away:
#
#     $ env -u DATABASE_URL -u MIGRATION_DATABASE_URL uv run pytest -q
#     661 passed, 220 skipped, 1 warning in 35.74s   → EXIT 0
#
# Every tenant-isolation, blinding, RLS and E2E-002 assertion in the repository
# became a skip and pytest reported success. A skip is not a pass, but an exit
# code cannot tell them apart and neither can a green check mark. The whole
# enforcement rested on one line — `has_postgres: true` in a 27-line CI wrapper.
# Flip it, or let the database sidecar fail soft, and CI is green with zero
# isolation coverage. NOTHING asserted that the integration tests had run.
#
# This gate asserts it. It is a session hook rather than a test on purpose: a
# test carrying the same `skipif` would reproduce the defect one level up, and a
# test carrying no `skipif` would error rather than report, burying the verdict
# in a traceback. Hooks in the root conftest cannot be deselected by `-k`, `-m`,
# a path argument or a `--deselect`, which is the property that matters.
#
# The exit-code discipline is the fleet's, from `research-e2e/README.md`
# § "Exit codes": a vacuous run is a failure, never a pass. 6 is used rather
# than research-e2e's 2 because pytest reserves 0-5 (2 is INTERRUPTED,
# 5 is NO_TESTS_COLLECTED) and a gate verdict must not be mistakable for any of
# them — least of all for 1, an ordinary test failure. 6 means exactly one
# thing: the suites that carry this service's isolation guarantees did not run.

#: The shared opening of every database-precondition skip reason in this
#: repository. Matching on the reason rather than on a file list is deliberate:
#: a new file that copies the idiom is covered the day it is written, and no
#: list has to be maintained in step with `tests/`.
DB_ABSENT_SKIP_PREFIX = "needs a live postgres"

#: Waives the floors — and ONLY the floors — for a deliberately narrowed run.
#: See `_gate_verdict` for what it cannot waive.
NO_ISOLATION_COVERAGE_ENV = "ERS_NO_ISOLATION_COVERAGE"

#: Not 0-5: those are pytest's own. See the note above.
EXIT_GATE_INTEGRITY = 6

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class _Selection:
    """A group of tests whose absence would make a green run a lie.

    `floor`s are floors, not expected values — the same convention
    `tests/migration/test_migration_round_trip.py` uses for its schema counts.
    Measured on c036c3a against a live database: rls-enforcement 8,
    reviewer-blinding 27, e2e-002-acceptance 40, review-scoping 19. Each floor
    sits below its count so that adding or deleting a single test does not touch
    this file, and far enough above zero that a half-collected selection cannot
    clear it.
    """

    name: str
    floor: int
    files: tuple[str, ...] = ()
    marker: str | None = None

    def contains(self, item: pytest.Item) -> bool:
        if self.marker is not None and item.get_closest_marker(self.marker) is not None:
            return True
        return any(item.nodeid == f or item.nodeid.startswith(f + "::") for f in self.files)


LOAD_BEARING: tuple[_Selection, ...] = (
    _Selection(
        name="rls-enforcement",
        floor=6,
        files=(
            "tests/integration/test_rls_foundation.py",
            "tests/integration/test_the_rls_backstop_fires.py",
        ),
    ),
    # By marker, because `pyproject.toml` already declares `blinding` as the
    # thing that "must fail when its layer is removed" — the marker is the
    # existing name for this selection and a second, path-shaped definition of
    # it would be a second answer to the same question.
    # ⚠️ RAISED 20 -> 34 on 2026-09-06. The marker selected 29 tests when the
    # floor was 20, so nine could have been deleted with the gate still green;
    # `test_the_ledger_is_blinded_from_a_reader_who_still_owes.py` then added
    # five for the `GET /flow` leak. MEASURED at 34 (`pytest -m blinding
    # --collect-only`), and the floor equals the count for the same reason
    # `e2e-003-no-duplicates` does: a blinding test is a distinct disclosure
    # this service refuses, and there is no spare one to lose.
    # 34 -> 37: the `/exports` bypass added three. The ledger read was gated and
    # the SEAL was not, so `POST /exports` recomputed the same derivation and
    # hash-sealed the disclosure permanently — worse than the read it replaced,
    # because an append-only export stays legible after the caller decides.
    _Selection(name="reviewer-blinding", floor=37, marker="blinding"),
    _Selection(
        name="e2e-002-acceptance",
        floor=30,
        files=("tests/integration/test_e2e_acceptance.py",),
    ),
    # `E2E-003`'s determinate core — the cardinality-one half of `15-…:649`
    # that is provable against a live Postgres alone. Its floor is 6 and equals
    # the number of tests in the file rather than sitting below it, because
    # every one of the six is a distinct property of the acceptance criterion
    # (one job, one job per key, one decision, one export, one delivery after a
    # crash, none after a rollback) and there is no such thing as a spare one to
    # lose. A seventh test added later must raise this number deliberately.
    _Selection(
        name="e2e-003-no-duplicates",
        floor=6,
        files=("tests/integration/test_e2e_003_no_duplicate_survives_a_restart.py",),
    ),
    # `E2E-015` — restore, outbox replay and projection rebuild. Its floor is 7
    # and equals the number of tests in the file, for the same reason
    # `e2e-003-no-duplicates`' floor does: each one is a distinct property of the
    # acceptance criterion (the restored checksum, the checksum's own
    # non-vacuity, the rebuild, the rebuild's drift report, the relay after a
    # restore, the replay after a restore, and the absence of a duplicate domain
    # EFFECT) and there is no spare one to lose.
    #
    # ⚠️ Two of the seven are NEGATIVE CONTROLS, and they are the reason this
    # selection is gated rather than merely present. The three "the checksum
    # reproduced" assertions would all pass against a `canonical_counts_hash`
    # that was a constant; only `..._is_not_a_constant_...` and
    # `..._reports_the_drift_it_corrects` can fail in that world. Deleting
    # either would leave five green tests certifying nothing, which is exactly
    # the shape this gate exists to refuse. An eighth test added later must
    # raise this number deliberately.
    _Selection(
        name="e2e-015-restore-replay-rebuild",
        floor=7,
        files=("tests/integration/test_e2e_015_a_restore_replays_without_duplicating.py",),
    ),
    # The relay's failure paths. `poisoned`/`POISON_REASONS` shipped in `0022`
    # as columns, CHECKs and docstrings with ZERO writers, and were reported
    # fixed once BEFORE they were reachable — three separately demonstrated
    # failures each still produced no poisoned row. These tests are what makes
    # the difference between the path existing and the path working, so they
    # are gated rather than merely present.
    _Selection(
        name="event-relay-poison",
        floor=33,
        files=(
            "tests/integration/test_a_poisoned_delivery_actually_lands.py",
            "tests/integration/test_a_replay_is_never_an_original.py",
            "tests/unit/test_a_consumer_name_is_validated_where_it_is_registered.py",
        ),
    ),
    # The worker's own preconditions. Its preflight passed while the relay
    # delivered nothing — it checked `pg_policies` and never the GRANTs beside
    # them — and its lease reaper trusted the pod's wall clock, so a skewed
    # replica could reap a live lease and hand a running job to a second pod.
    # Both are properties of a process nothing else observes: there is no
    # liveness probe and no ServiceMonitor on this workload, so a test is
    # currently the only thing that can notice either regression.
    _Selection(
        name="worker-preconditions",
        floor=20,
        files=(
            "tests/integration/test_the_preflight_checks_grants_not_only_policies.py",
            "tests/integration/test_a_skewed_worker_clock_cannot_reap_a_live_lease.py",
            "tests/unit/test_the_worker_reports_a_poisoned_delivery.py",
        ),
    ),
    _Selection(
        name="review-scoping",
        floor=14,
        files=(
            "tests/security/test_cross_review_scoping.py",
            "tests/security/test_review_authority.py",
        ),
    ),
)


class _GateState:
    """What the session actually did, as opposed to what it was asked to do."""

    def __init__(self) -> None:
        self.collected: dict[str, list[str]] = {s.name: [] for s in LOAD_BEARING}
        #: Node IDs whose body ran. A test whose `call` phase reported `skipped`
        #: is NOT in here: `pytest.skip()` from inside a test body is the same
        #: false green as a `skipif`, one stack frame lower.
        self.executed: set[str] = set()
        self.db_absent_skips: list[str] = []


_GATE = _GateState()


def _skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        raw = str(longrepr[2])
    else:
        raw = str(longrepr)
    return raw.removeprefix("Skipped: ").strip()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    for item in items:
        for selection in LOAD_BEARING:
            if selection.contains(item):
                _GATE.collected[selection.name].append(item.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" and not report.skipped:
        _GATE.executed.add(report.nodeid)
    if report.skipped and _skip_reason(report).startswith(DB_ABSENT_SKIP_PREFIX):
        _GATE.db_absent_skips.append(report.nodeid)


def _gate_verdict() -> tuple[list[tuple[_Selection, int, int]], list[str], bool]:
    """Three rules, of which the opt-out can waive exactly one.

    1. Every load-bearing test that was COLLECTED must have RUN. Unconditional.
       This is what makes the opt-out safe: it cannot be used to launder a full
       run into a green one, because a full run without a database collects
       these tests and skips them.
    2. NO test anywhere may be skipped for want of a database. Unconditional,
       and deliberately not restricted to the load-bearing selections — the
       failure being caught is "the database was absent", which is a property of
       the session, not of any one file.
    3. Each selection must have run at least `floor` tests. Waivable, because
       this is the only rule a legitimate unit-only run cannot satisfy.
    """
    opted_out = os.environ.get(NO_ISOLATION_COVERAGE_ENV, "").strip().lower() in _TRUTHY
    rows: list[tuple[_Selection, int, int]] = []
    failures: list[str] = []

    for selection in LOAD_BEARING:
        collected = _GATE.collected[selection.name]
        ran = sum(1 for nodeid in collected if nodeid in _GATE.executed)
        rows.append((selection, len(collected), ran))
        if ran < len(collected):
            failures.append(
                f"{selection.name}: {len(collected) - ran} of {len(collected)} collected tests did not run. "
                "They were collected and then skipped, which pytest counts as success and this gate does not."
            )
        elif not opted_out and ran < selection.floor:
            failures.append(
                f"{selection.name}: {ran} tests ran, below the floor of {selection.floor}. "
                "This selection was narrowed out of the session, so nothing in it was exercised."
            )

    if _GATE.db_absent_skips:
        failures.append(
            f"{len(_GATE.db_absent_skips)} tests were skipped for want of a database "
            f'(skip reason "{DB_ABSENT_SKIP_PREFIX}…"). Every isolation, blinding and RLS guarantee '
            "in this service is enforced by Postgres, so without one they are unmeasured, not met. "
            "Bring a database up — README.md § Local development."
        )

    return rows, failures, opted_out


def _gate_lines() -> list[str]:
    rows, failures, opted_out = _gate_verdict()
    width = max(len(s.name) for s in LOAD_BEARING)
    lines = [f"{'selection'.ljust(width)}  collected   ran  floor"]
    lines += [f"{s.name.ljust(width)}  {collected:>9}  {ran:>4}  {s.floor:>5}" for s, collected, ran in rows]
    lines.append("")

    if opted_out:
        lines += [
            f"{NO_ISOLATION_COVERAGE_ENV} is set: the floors above are WAIVED. This run",
            "certifies NOTHING about tenant isolation, reviewer blinding or RLS enforcement.",
            "",
        ]

    if not failures:
        total = sum(ran for _, _, ran in rows)
        lines.append(f"OK — {total} tests ran across {len(rows)} load-bearing selections, 0 database-absence skips.")
        return lines

    lines += [
        "GATE INTEGRITY FAILURE — the suites that carry this service's isolation",
        "guarantees did not run, so this session proves nothing about them.",
        "",
    ]
    lines += [f"  * {failure}" for failure in failures]
    lines += [
        "",
        f"Exiting {EXIT_GATE_INTEGRITY} (gate integrity failure), not 0. A skip is not a pass.",
    ]
    # Stated as what the opt-out CANNOT do as well as what it can, because the
    # first thing anybody reading this will try is setting it on the run they
    # just made — and on a full run without a database that changes nothing.
    if not opted_out:
        lines += [
            "",
            "A run without the isolation suites is supported only as an explicit unit-only",
            f"lane: `{NO_ISOLATION_COVERAGE_ENV}=1 uv run pytest tests/unit`. That variable waives",
            "the floors and nothing else — a test that was collected and then skipped still",
            "fails this gate, so it cannot turn a full database-less run green.",
        ]
    return lines


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:  # noqa: ANN001, ARG001
    if config.option.collectonly:
        return
    terminalreporter.write_sep("=", "isolation coverage gate")
    for line in _gate_lines():
        terminalreporter.write_line(line)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.option.collectonly:
        return
    _rows, failures, _opted_out = _gate_verdict()
    # `pytest_terminal_summary` above is what prints the verdict, and it runs
    # strictly after this hook — the terminal reporter wraps
    # `pytest_sessionfinish`, so its post-yield half is last and `trylast` here
    # is not enough to get behind it.
    #
    # This branch is unreachable TODAY: `-p no:terminal` is the only way to lose
    # that hook, and it also removes `-v`, which `pyproject.toml` puts in
    # `addopts` — so pytest exits 4 on an unrecognised argument before a single
    # test runs. It is kept anyway, because the property this gate exists to
    # protect is "the verdict is never silent", and that property must not rest
    # on an entry in `addopts` that somebody may reasonably delete.
    if session.config.pluginmanager.get_plugin("terminalreporter") is None:  # pragma: no cover
        print("\n".join(["", "=== isolation coverage gate ===", *_gate_lines()]), file=sys.stderr)
    # Only over a green run. An exit code that already says "something is wrong"
    # is more specific than this one and must not be overwritten by it.
    if failures and exitstatus == 0:
        session.exitstatus = EXIT_GATE_INTEGRITY
