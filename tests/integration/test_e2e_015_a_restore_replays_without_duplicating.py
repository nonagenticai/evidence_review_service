"""E2E-015 — restore, outbox replay and projection rebuild, without a duplicate effect.

`15-…` — "`E2E-015` | PostgreSQL restore, outbox replay and search/graph rebuild |
recoverability without duplicate domain effects".
`implementation-plans/90-…` — "`E2E-015`: restore, event replay, cache and
projection rebuild".
The release-report requirement it answers: "restore plus outbox replay and
projection rebuild checksum proof".

## ⛔ What this file deliberately does NOT do

**It does not rebuild a search index or a graph projection, because neither
exists in this service.** `projection_checkpoints` is a table with no writer —
`consumer_registry` says so in as many words ("Not implemented. This service has
`projection_checkpoints` — the table a projector *would* keep") and `0023`
deliberately leaves it out of the worker grants. A test that wrote checkpoint
rows itself and then read them back would be grading its own fixture, which is
the one thing worse than not testing it. The observable consequence is asserted
instead: after a restore and a full replay the checkpoint table is still empty,
so nothing in this service is silently depending on a projector that was never
built. When a projector lands, that assertion fails and this file is where the
rebuild proof belongs.

**It does not test `pg_restore`.** Postgres's own dump/restore is not this
service's code and a test of it would measure Postgres. What is at stake here is
whether THIS schema and THIS relay survive a restore — whether the append-only
tables, the partial unique index on `event_deliveries` and the outbox cursor
still hold when the same rows arrive a second time from a backup.

**The restore is therefore a LOGICAL one, done through the database connection
the suite already has**: the schema is rebuilt by the service's own migrations
and every row of every table is carried across with `COPY … TO STDOUT (FORMAT
BINARY)` / `COPY … FROM STDIN`, read inside a single `REPEATABLE READ`
transaction so the copy is one snapshot rather than a per-table smear — the
same guarantee `pg_dump` gives itself. It shells out to nothing. `pg_dump` is
not on the fleet's ARC runner images and a restore proof that skips when a
binary is missing would be a proof that skips in CI, which is where it has to
run. `_restore` asserts, per table, that the row count it loaded equals the row
count it read; a restore that silently dropped rows — the exact failure a
non-superuser copy would produce under FORCE RLS — fails there rather than
downstream as a puzzling hash mismatch.

## What IS proved, and the negative control for each

1. a restore reproduces the flow ledger's `canonical_counts_hash` byte for byte,
   and every table's row count with it;
2. **negative control for 1 and 3** — rewriting the screening history of one
   INCLUDED record in the restored copy moves `included` down by one, `excluded`
   up by one, and DIVERGES that hash. Without it, 1 and 3 would pass just as
   happily against a hash that is a constant. See `_rewrite_one_included_record`
   for why the target is chosen by the product's own `included_record_ids` and
   not by an unordered `LIMIT 1` — the first version of this control perturbed
   nothing at all under some row orderings, and said so only in a full-suite run;
3. a rebuild from zero reproduces the same hash and reports
   `matches_previous_projection`, and a rebuild over a rewritten input reports it
   FALSE rather than quietly correcting itself;
4. the relay run against a restored database re-delivers the event whose window
   the backup caught open EXACTLY once, and creates no second log row and no
   second ORIGINAL delivery for anything the backup already carried;
5. a `replay` after a restore mints no second original — it is additive by
   design, and each replayed row names the original it derives from;
6. restore plus replay produce no duplicate domain EFFECT: one export, one
   decision per reviewer per record, unchanged.

Every test takes its own pristine restore — the `restored` fixture is
FUNCTION-scoped. Four of the seven write into that database (two relay into it,
two rewrite history in it), so a module-scoped copy would make the result of
each depend on which of the others had already run. That is a real cost — a full
schema copy per test — accepted deliberately, because the alternative is a suite
whose verdict is a function of collection order.

## Why the fixtures come from `test_e2e_acceptance`

The same reason `E2E-003` gives, and the same mechanism. `E2E-002` already
builds the only state these assertions can be made against — a locked,
activated, imported, deduped, fully screened and adjudicated review with a
sealed export — and a hand-rolled second copy of that ladder would be a second
thing to keep in step with the product.

Only the FIXTURES and the header helpers are imported. Importing the test
functions too would re-collect all forty of `E2E-002`'s tests under this file's
node ids, and the isolation gate counts by node id — this file's floor would
then be satisfied by copies of another suite's tests.

## The relay is the real one

`relay.relay_pending` and `relay.replay` are called directly, against a real
database that really was restored from a backup. A mock relay would prove that a
mock is idempotent.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tests.integration import test_e2e_acceptance as _e2e002  # noqa: E402
from tests.integration.test_e2e_acceptance import LEAD, REVIEWER_B, TENANT, lead_headers  # noqa: E402
from tests.migration.test_migration_round_trip import _alembic  # noqa: E402

# The fixtures, REBOUND rather than imported by name — see `E2E-003`'s note on
# why `from … import review` is an `F811` here and an assignment is not.
adjudicated = _e2e002.adjudicated
app_client = _e2e002.app_client
dedup = _e2e002.dedup
export = _e2e002.export
review = _e2e002.review
screened = _e2e002.screened

DB_URL = os.getenv("DATABASE_URL", "")
#: The owner. `tests/conftest.py` demotes `DATABASE_URL` to the unprivileged
#: runtime role and puts the owner here, so this is the URL that can CREATE
#: DATABASE and read every row of every table for the backup.
OWNER_URL = os.getenv("MIGRATION_DATABASE_URL") or DB_URL

# ⚠️ The prefix is load-bearing. `tests/conftest.py` keys `DB_ABSENT_SKIP_PREFIX`
# on exactly this opening, and a skip reason that does not match makes the
# isolation gate misreport a database-absent run as a narrowed one.
pytestmark = pytest.mark.skipif(
    not DB_URL.startswith("postgresql"),
    reason="needs a live postgres; DATABASE_URL is not a postgresql URL",
)

TENANT_UUID = uuid.UUID(TENANT)

#: Bounded so a relay that is not making progress fails loudly instead of
#: spinning. Same reasoning as `E2E-003`'s constant of the same name.
_MAX_PASSES = 50

#: `alembic_version` is excluded from the copy: `upgrade head` has already
#: written the destination's row, and carrying the source's would collide on the
#: primary key for no gain — the two are equal by construction or the migration
#: run failed.
_NOT_COPIED = frozenset({"alembic_version"})


# ---------------------------------------------------------------------------
# Talking to the live database
# ---------------------------------------------------------------------------


def _actor(subject: str = LEAD, role: str = "lead") -> Any:
    from actor import Actor, ActorRole

    return Actor(tenant_id=TENANT_UUID, subject_id=subject, role=ActorRole(role))


def _scalar(sql: str, **params: Any) -> Any:
    """One value from the LIVE database, read through a properly bound actor scope.

    `actor_scope` binds all three GUCs the policies read; a raw connection
    binding only `app.current_tenant` matches zero rows on the blinded tables and
    would make every count below silently vacuous.
    """
    from database import actor_scope

    with actor_scope(_actor()) as session:
        return session.execute(text(sql), params).scalar()


def _relay_until_drained_live() -> None:
    from application.services import relay
    from database import actor_scope

    for _ in range(_MAX_PASSES):
        with actor_scope(_actor()) as session:
            relayed, _ = relay.relay_pending(session)
        if relayed == 0:
            return
    raise AssertionError(
        f"the relay made {_MAX_PASSES} passes against the live database without draining; no "
        "conclusion about duplication can be drawn from a relay that has not finished"
    )


# ---------------------------------------------------------------------------
# The backup and the restore
# ---------------------------------------------------------------------------


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _conninfo(url: str) -> str:
    """`postgresql+psycopg://…` → the libpq URL `psycopg.connect` wants."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _restore(source_url: str, target_url: str) -> dict[str, int]:
    """Carry every row of `source_url` into the already-migrated `target_url`.

    Returns the per-table row counts, which are asserted equal on both sides
    before this returns — a copy that silently dropped rows must fail HERE,
    naming the table, rather than downstream as an unexplained hash mismatch.

    The read side runs in one `REPEATABLE READ` transaction so every table is
    read from a single snapshot; a table-at-a-time copy of a database something
    else is still writing to is not a backup of any instant that ever existed.

    `session_replication_role = replica` on the write side is what `pg_restore
    --disable-triggers` does and for the same reason: the destination is being
    loaded in an arbitrary table order, so foreign keys cannot be checked
    row-by-row, and this schema's append-only triggers would refuse the load
    outright. It is set on the CONNECTION, so it reverts when the connection
    closes and can never leak into the live database.
    """
    counts: dict[str, int] = {}
    with psycopg.connect(_conninfo(source_url)) as src, psycopg.connect(_conninfo(target_url)) as dst:
        src.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        dst.execute("SET session_replication_role = replica")

        tables = [
            name
            for (name,) in src.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            ).fetchall()
            if name not in _NOT_COPIED
        ]
        assert tables, "the source database has no public tables; there is nothing to back up"

        for table in tables:
            (source_rows,) = src.execute(f'SELECT count(*) FROM public."{table}"').fetchone()  # nosec B608
            counts[table] = int(source_rows)
            if not source_rows:
                continue
            with src.cursor().copy(f'COPY public."{table}" TO STDOUT (FORMAT BINARY)') as reader:  # nosec B608
                payload = b"".join(reader)
            with dst.cursor().copy(f'COPY public."{table}" FROM STDIN (FORMAT BINARY)') as writer:  # nosec B608
                writer.write(payload)

        # Identity sequences are not carried by `COPY` — `pg_dump` emits a
        # `setval` for exactly this reason. Without it the restored
        # `research_domain_events.log_position` sequence restarts at 1 and the
        # first relayed event collides with a restored row on a UNIQUE index,
        # which would look like the duplication this file is meant to detect.
        for table, column in dst.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND is_identity = 'YES'"
        ).fetchall():
            dst.execute(
                f"SELECT setval(pg_get_serial_sequence('public.\"{table}\"', %s), "  # nosec B608
                f'GREATEST(coalesce((SELECT max("{column}") FROM public."{table}"), 1), 1))',
                (column,),
            )
        dst.commit()

        for table, expected in counts.items():
            (restored_rows,) = dst.execute(f'SELECT count(*) FROM public."{table}"').fetchone()  # nosec B608
            assert int(restored_rows) == expected, (
                f"the restore lost rows in {table}: backed up {expected}, restored {restored_rows}. "
                "A silent shortfall here is what a copy made under FORCE ROW LEVEL SECURITY by a "
                "role without BYPASSRLS looks like."
            )
    return counts


@pytest.fixture
def restored(review, screened, adjudicated, export) -> Iterator[dict[str, Any]]:  # noqa: ANN001
    """A real database restored from a backup of the live one, with a relay window open.

    FUNCTION-scoped on purpose. The tests below relay into this database and
    rewrite rows in it; sharing one across the module would make the result of
    each depend on `pytest-randomly`'s seed.

    Before the backup is taken, a review is created and committed WITHOUT
    relaying it. That is the instant a backup is most likely to catch in
    production and the one that makes this file's central question non-vacuous:
    the restored database holds an outbox row that is not yet in the log, so the
    relay that runs after the restore has real work to do and can be seen either
    doing it exactly once or doing it twice. Draining first and restoring a
    quiet database would let a relay that refused to do anything at all pass
    every assertion.
    """
    from sqlalchemy.exc import ProgrammingError

    from application.services import reviews as review_svc
    from database import actor_scope

    # Everything already committed reaches the log, so the only open window in
    # the backup is the one this fixture opens deliberately below.
    _relay_until_drained_live()

    with actor_scope(_actor()) as session:
        created = review_svc.create_review(
            session,
            _actor(),
            project_id="e2e015",
            external_key=f"restore-probe-{uuid.uuid4()}",
            title="A backup taken between the commit and the relay",
        )
        probe_review_id = str(created.review_id)

    probe_event_id = _scalar(
        "SELECT event_id FROM domain_outbox WHERE aggregate_id = :a AND event_type = 'review.created.v1'",
        a=probe_review_id,
    )
    assert probe_event_id is not None, "the probe event did not commit with the state it describes"
    assert _scalar("SELECT count(*) FROM research_domain_events WHERE event_id = :e", e=probe_event_id) == 0, (
        "the probe event was already relayed, so the backup does not catch the window this fixture names"
    )

    name = f"evidence_review_restored_{uuid.uuid4().hex[:12]}"
    admin = create_engine(OWNER_URL, future=True, isolation_level="AUTOCOMMIT")
    created_db = False
    try:
        with admin.connect() as conn:
            assert conn.execute(
                text("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user")
            ).scalar(), (
                "the owner role cannot CREATE DATABASE, so this environment cannot restore a backup "
                "into a database of its own — and recoverability cannot be proved without one"
            )
            assert name != conn.execute(text("SELECT current_database()")).scalar_one(), (
                "refusing to restore over the shared database"
            )
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            created_db = True

        target_url = _with_database(OWNER_URL, name)
        _alembic("upgrade", "head", target_url)
        census = _restore(OWNER_URL, target_url)

        yield {
            "url": target_url,
            "review_id": review["review_id"],
            "probe_review_id": probe_review_id,
            "probe_event_id": probe_event_id,
            "census": census,
        }
    finally:
        # The live database is left drained whatever happened above, so the
        # window this fixture opened is never inherited by another test.
        try:
            _relay_until_drained_live()
        finally:
            if created_db:
                with admin.connect() as conn:
                    try:
                        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                    except ProgrammingError:  # pragma: no cover - older servers have no FORCE
                        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            admin.dispose()


# ---------------------------------------------------------------------------
# Reading and writing the restored database
# ---------------------------------------------------------------------------


def _restored_sessions(url: str) -> Any:
    """A session factory bound to the restored database, GUCs bound per session."""
    engine = create_engine(url, future=True, connect_args={"prepare_threshold": 0})
    return engine, sessionmaker(bind=engine, future=True)


def _bound(factory: Any) -> Session:
    from actor import bind_actor

    session = factory()
    bind_actor(session, _actor())
    return session


def _flow_hash(url: str, review_id: str) -> tuple[str, dict[str, Any]]:
    """The flow ledger's canonical counts hash, derived on the restored database.

    `compute_flow(persist=False)` — the SAME function the live `GET /flow` calls,
    so the two sides of the comparison are computed by one code path over two
    databases. Deriving the hash here by any other route would be comparing this
    file's arithmetic against the product's.
    """
    from application.services import flow

    engine, factory = _restored_sessions(url)
    try:
        with _bound(factory) as session:
            body = flow.compute_flow(session, _actor(), uuid.UUID(review_id), persist=False)
            return body["canonical_counts_hash"], body["counts"]
    finally:
        engine.dispose()


def _restored_scalar(url: str, sql: str, **params: Any) -> Any:
    engine, factory = _restored_sessions(url)
    try:
        with _bound(factory) as session:
            return session.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _rewrite_one_included_record(url: str, review_id: str) -> str:
    """Rewrite history in the RESTORED copy so one INCLUDED record is excluded.

    Returns the `review_record_id` that was moved.

    ## ⚠️ Why the target is chosen by `included_record_ids` and not by SQL

    This helper used to take the first row of
    `… WHERE disposition = 'include' LIMIT 1`. That is two separate mistakes and
    the full suite caught both where running this file alone did not.

    `LIMIT 1` with no `ORDER BY` returns whatever row Postgres reaches first,
    which depends on physical order — so the tamper picked a different decision
    depending on what else had run, and the negative controls passed or failed
    with it. Worse, a single `include` decision is not the same thing as an
    included RECORD: this protocol requires two reviewers, so flipping one of a
    pair leaves the record's terminal bucket entirely unmoved, and the "rewrite"
    changed no count at all. A control that only sometimes perturbs the thing it
    is controlling for is not a control.

    So the target is the terminal bucket the product itself computes —
    `flow.included_record_ids`, the same function `ER-006` and the sealed export
    ask — the lowest id of them for determinism, and EVERY decision on that
    record is rewritten. The caller then asserts `included` actually fell.

    `session_replication_role = replica` for the same reason the load uses it:
    `screening_decisions` carries `trg_screening_decisions_append_only`, which
    refuses an UPDATE outright. This is a deliberate corruption of a throwaway
    database — the live one is never touched by it.
    """
    from application.services import flow

    engine, factory = _restored_sessions(url)
    try:
        with _bound(factory) as session:
            included = flow.included_record_ids(session, uuid.UUID(review_id))
    finally:
        engine.dispose()
    assert included, (
        "the restored copy holds no INCLUDED record for this review, so there is nothing whose "
        "exclusion could move a count and the negative control would be vacuous"
    )
    target = str(sorted(str(r) for r in included)[0])

    with psycopg.connect(_conninfo(url)) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (TENANT,))
        # `exclude` needs a reason — `ck_decision_exclusion_has_reason` — and the
        # vocabulary is the one `E2E-002`'s protocol version declares.
        result = conn.execute(
            "UPDATE screening_decisions SET disposition = 'exclude', exclusion_reason_code = 'wrong_population' "
            "WHERE review_id = %s AND review_record_id = %s",
            (review_id, target),
        )
        assert result.rowcount >= 1, (
            f"the rewrite touched {result.rowcount} rows for an included record; it cannot have changed anything"
        )
        conn.commit()
    return target


# ---------------------------------------------------------------------------
# 1. A restore reproduces the ledger
# ---------------------------------------------------------------------------


def test_a_restore_reproduces_the_flow_counts_hash_and_every_row_count(app_client, restored):
    """`01-…:510`'s checksum, held across a backup and a restore.

    The live hash is read through the API, the restored one is derived on the
    restored database, and they must be the same string. `counts` is compared
    too — not because the hash could differ while the counts agree, but because
    a mismatch reports WHICH number moved, and a bare hash comparison would only
    ever say "different".
    """
    review_id = restored["review_id"]

    live = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers())
    assert live.status_code == 200, live.text
    live_hash, live_counts = live.json()["canonical_counts_hash"], live.json()["counts"]
    assert live_hash.startswith("sha256:"), live_hash

    restored_hash, restored_counts = _flow_hash(restored["url"], review_id)

    assert restored_counts == live_counts, (
        f"the restored ledger counts differ from the live ones: {restored_counts} != {live_counts}"
    )
    assert restored_hash == live_hash, f"a restore changed the flow ledger checksum: {restored_hash} != {live_hash}"

    # The census the restore itself asserted, re-stated as a property of the
    # thing under test: the tables this file reasons about were not empty, so
    # none of the assertions above is a comparison of two zeroes.
    census = restored["census"]
    for table in ("reviews", "screening_decisions", "inclusion_exports", "domain_outbox", "research_domain_events"):
        assert census.get(table, 0) > 0, f"{table} was empty in the backup; the restore proved nothing about it"


def test_the_restored_hash_is_not_a_constant_a_rewritten_decision_diverges_it(restored):
    """⛔ THE NEGATIVE CONTROL for the two hash-reproduction tests.

    `canonical_counts_hash` over a review whose history has been rewritten must
    not be the hash over the review that was backed up. Without this, a
    `canonical_counts_hash` that returned the same constant for every input —
    or one derived from the review id rather than from its decisions — would
    satisfy every "the hash reproduced" assertion in this file perfectly.

    The rewrite is done in the restored copy, so the live database still holds
    the history the rest of the suite depends on.

    The MOVEMENT is asserted, not merely a difference. "The hash changed" would
    also be satisfied by a hash that changed for the wrong reason — a timestamp
    folded into the digest, say — so the specific number that must fall is named:
    one record left the `included` bucket, so `included` is one lower and
    `excluded` one higher.
    """
    review_id = restored["review_id"]
    before, before_counts = _flow_hash(restored["url"], review_id)
    assert before_counts["included"] > 0, "nothing is included, so excluding a record cannot move a count"

    moved = _rewrite_one_included_record(restored["url"], review_id)

    after, after_counts = _flow_hash(restored["url"], review_id)
    assert after_counts["included"] == before_counts["included"] - 1, (
        f"excluding record {moved} did not reduce `included`: {before_counts['included']} -> {after_counts['included']}"
    )
    assert after_counts["excluded"] == before_counts["excluded"] + 1, (
        f"the record left `included` without arriving in `excluded`: {before_counts} -> {after_counts}"
    )
    assert after != before, (
        "rewriting a review's screening history did not change the flow checksum, so the checksum is "
        "not a function of the history it claims to summarise and every hash comparison here is vacuous"
    )


# ---------------------------------------------------------------------------
# 2. A rebuild from zero
# ---------------------------------------------------------------------------


def test_a_rebuild_from_zero_reproduces_the_projection_hash(app_client, review, export):
    """`POST /flow:rebuild` — "recompute from zero; the hash must reproduce".

    Run twice. One rebuild proves the derivation agrees with the cache it just
    wrote, which it would even if both were wrong in the same way; the second
    is over a projection the first one persisted, and is the one that would
    catch a rebuild whose output depends on what was already stored.
    """
    review_id = review["review_id"]

    read = app_client.get(f"/api/reviews/{review_id}/flow", headers=lead_headers())
    assert read.status_code == 200, read.text
    derived_hash = read.json()["canonical_counts_hash"]

    first = app_client.post(f"/api/reviews/{review_id}/flow:rebuild", headers=lead_headers())
    assert first.status_code == 200, first.text
    assert first.json()["rebuilt"] is True
    assert first.json()["canonical_counts_hash"] == derived_hash, (
        "a rebuild from zero produced a different checksum than the read that derived it"
    )
    assert first.json()["matches_previous_projection"] is True, (
        f"the stored projection had already drifted before this test ran: {first.json()}"
    )

    second = app_client.post(f"/api/reviews/{review_id}/flow:rebuild", headers=lead_headers())
    assert second.status_code == 200, second.text
    assert second.json()["canonical_counts_hash"] == derived_hash
    assert second.json()["previous_canonical_counts_hash"] == derived_hash
    assert second.json()["matches_previous_projection"] is True


def test_a_rebuild_over_a_rewritten_input_reports_the_drift_it_corrects(restored):
    """⛔ THE NEGATIVE CONTROL for the rebuild above, on the restore's own path.

    `rebuild_flow` returns `matches_previous_projection` and the test above
    asserts it is `True`. A field that were hardcoded `True` — or computed
    against the projection the same call had just written — would pass there and
    tell an operator recovering from a bad restore that everything agreed.

    So: rebuild the restored copy once to seat a projection, rewrite an input
    underneath it, rebuild again, and require the call to REPORT the divergence
    rather than quietly persist the new number. `flow.rebuild_flow` is called
    directly because the FastAPI app is bound to the live database and this
    assertion has to be made against the restored one.
    """
    from application.services import flow

    review_id = restored["review_id"]
    engine, factory = _restored_sessions(restored["url"])
    try:
        with _bound(factory) as session:
            seated = flow.rebuild_flow(session, _actor(), uuid.UUID(review_id))
            session.commit()
        assert seated["matches_previous_projection"] is True, seated
        seated_hash = seated["canonical_counts_hash"]

        _rewrite_one_included_record(restored["url"], review_id)

        with _bound(factory) as session:
            after = flow.rebuild_flow(session, _actor(), uuid.UUID(review_id))
            session.commit()
    finally:
        engine.dispose()

    assert after["previous_canonical_counts_hash"] == seated_hash, (
        f"the rebuild compared against something other than the projection it had seated: {after}"
    )
    assert after["canonical_counts_hash"] != seated_hash, (
        "the rebuild reproduced the old checksum over rewritten history"
    )
    assert after["matches_previous_projection"] is False, (
        "the rebuild silently corrected a projection that no longer matched its inputs; an operator "
        "checking a restore would have been told the cache agreed"
    )


# ---------------------------------------------------------------------------
# 3. The relay, after a restore
# ---------------------------------------------------------------------------


def test_the_relay_after_a_restore_delivers_the_open_window_exactly_once(restored):
    """The backup caught a commit that had not been relayed. Recovery must not double it.

    This is `E2E-015`'s central question. The restored database holds, for every
    event the backup carried, both the `domain_outbox` row and the
    `research_domain_events` row — so a relay whose cursor were "everything in
    the outbox" rather than "everything in the outbox NOT already in the log"
    would re-emit the entire history on the first pass after every restore.

    The probe event is the positive control: it is in the outbox and not in the
    log, so a relay that declined to do anything at all — which would satisfy
    every "no duplicate" assertion here — fails instead.
    """
    from application.services import relay
    from domain.policies import consumer_registry

    probe_event_id, url = restored["probe_event_id"], restored["url"]
    engine, factory = _restored_sessions(url)
    try:
        with _bound(factory) as session:
            already = session.execute(
                text("SELECT count(*) FROM research_domain_events WHERE event_id = :e"), {"e": probe_event_id}
            ).scalar()
            carried = session.execute(text("SELECT count(*) FROM research_domain_events")).scalar()
        assert already == 0, "the restored copy had already relayed the probe; the window is not open"
        assert carried > 0, (
            "the restored log is empty, so this test cannot show that a relay declines to re-emit "
            "what the backup already carried"
        )

        consumers = consumer_registry.consumers_for("review.created.v1")
        assert consumers, "no consumer is registered for review.created.v1; the fan-out assertions are vacuous"

        def drain() -> int:
            total = 0
            for _ in range(_MAX_PASSES):
                with _bound(factory) as session:
                    relayed, _ = relay.relay_pending(session)
                    session.commit()
                total += relayed
                if relayed == 0:
                    return total
            raise AssertionError(f"the relay made {_MAX_PASSES} passes against the restored database without draining")

        first_pass = drain()
        assert first_pass >= 1, "the relay relayed nothing at all, so it cannot be shown to relay exactly once"

        with _bound(factory) as session:
            landed = session.execute(
                text("SELECT count(*) FROM research_domain_events WHERE event_id = :e"), {"e": probe_event_id}
            ).scalar()
            log_after_first = session.execute(text("SELECT count(*) FROM research_domain_events")).scalar()
            originals = session.execute(
                text("SELECT count(*) FROM event_deliveries WHERE event_id = :e AND replay_of_delivery_id IS NULL"),
                {"e": probe_event_id},
            ).scalar()
        assert landed == 1, f"the probe event landed {landed} times in the durable log"
        assert originals == len(consumers), f"expected one delivery per registered consumer, got {originals}"

        # AND THE RELAY RAN AGAIN — a restore is usually followed by more than
        # one attempt, and `relay_pending` is at-least-once by construction.
        assert drain() == 0, "a second relay pass after the restore found work the first should have finished"
        with _bound(factory) as session:
            log_after_second = session.execute(text("SELECT count(*) FROM research_domain_events")).scalar()
            duplicated = session.execute(
                text(
                    "SELECT count(*) FROM (SELECT event_id FROM event_deliveries "
                    "WHERE replay_of_delivery_id IS NULL GROUP BY event_id, consumer HAVING count(*) > 1) d"
                )
            ).scalar()
    finally:
        engine.dispose()

    assert log_after_second == log_after_first, (
        f"a second relay pass grew the durable log from {log_after_first} to {log_after_second}"
    )
    assert duplicated == 0, (
        f"{duplicated} (event, consumer) pairs hold more than one ORIGINAL delivery after a restore and replay"
    )


def test_a_replay_after_a_restore_mints_no_second_original_delivery(restored):
    """`replay` on a restored database is additive, and never an original.

    `relay.replay` is NOT idempotent and its own docstring refuses to pretend
    otherwise — exactly-once lives in the consumer's inbox index. So what is
    asserted is the property that actually holds and that a duplicate would
    break: the ORIGINAL delivery (`replay_of_delivery_id IS NULL`) stays at
    exactly one per consumer however many times replay runs, and every replayed
    row names the original it derives from.
    """
    from application.services import relay
    from domain.policies import consumer_registry

    url = restored["url"]
    engine, factory = _restored_sessions(url)
    try:
        with _bound(factory) as session:
            relay.relay_pending(session)
            session.commit()

        consumer = consumer_registry.consumers_for("review.created.v1")[0]
        with _bound(factory) as session:
            originals_before = session.execute(
                text("SELECT count(*) FROM event_deliveries WHERE consumer = :c AND replay_of_delivery_id IS NULL"),
                {"c": consumer},
            ).scalar()
        assert originals_before > 0, (
            "the restored copy holds no original delivery for this consumer, so a replay cannot be shown "
            "to leave the originals alone"
        )

        with _bound(factory) as session:
            replayed = relay.replay(session, consumer=consumer, from_position=0)
            session.commit()
        assert int(replayed) >= 1, "the replay recreated nothing, so it cannot show what a replay does"

        with _bound(factory) as session:
            originals_after = session.execute(
                text("SELECT count(*) FROM event_deliveries WHERE consumer = :c AND replay_of_delivery_id IS NULL"),
                {"c": consumer},
            ).scalar()
            orphaned = session.execute(
                text(
                    "SELECT count(*) FROM event_deliveries d WHERE d.consumer = :c "
                    "AND d.replay_of_delivery_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM event_deliveries o WHERE o.delivery_id = d.replay_of_delivery_id)"
                ),
                {"c": consumer},
            ).scalar()
            log_rows = session.execute(text("SELECT count(*) FROM research_domain_events")).scalar()
            distinct_events = session.execute(
                text("SELECT count(DISTINCT event_id) FROM research_domain_events")
            ).scalar()
    finally:
        engine.dispose()

    assert originals_after == originals_before, (
        f"a replay after a restore minted {originals_after - originals_before} new ORIGINAL deliveries"
    )
    assert orphaned == 0, f"{orphaned} replayed deliveries name an original that does not exist"
    assert log_rows == distinct_events, (
        f"the durable log holds {log_rows} rows for {distinct_events} distinct events; a replay changed event_id"
    )


# ---------------------------------------------------------------------------
# 4. No duplicate domain EFFECT
# ---------------------------------------------------------------------------


def test_a_restore_and_a_full_replay_leave_one_export_and_one_decision_set(restored):
    """Recoverability "without duplicate domain effects" — the effects, not the plumbing.

    The tests above count log rows and delivery rows. This one counts the things
    a person would notice: the sealed export a consumer would cite, and the
    decisions a reviewer would be answerable for. A restore followed by a full
    relay and a replay of every registered consumer must leave both exactly as
    the backup found them.

    The checkpoint assertion is the honest statement of what this service does
    NOT have. `projection_checkpoints` has no writer, so "rebuild the projection
    after a restore" has no subject here; asserting the table is empty records
    that as a measurement rather than as a silence, and turns the day a projector
    lands into a failing test in the right file.
    """
    from application.services import relay
    from domain.policies import consumer_registry

    review_id, url = restored["review_id"], restored["url"]

    exports_before = _restored_scalar(url, "SELECT count(*) FROM inclusion_exports WHERE review_id = :r", r=review_id)
    decisions_before = _restored_scalar(
        url, "SELECT count(*) FROM screening_decisions WHERE review_id = :r", r=review_id
    )
    assert exports_before == 1, f"the backup carried {exports_before} exports for this review, expected 1"
    assert decisions_before > 0, "the backup carried no screening decisions; the assertion below is vacuous"

    engine, factory = _restored_sessions(url)
    try:
        for _ in range(_MAX_PASSES):
            with _bound(factory) as session:
                relayed, _ = relay.relay_pending(session)
                session.commit()
            if relayed == 0:
                break
        else:  # pragma: no cover - a relay that never drains fails the test above first
            raise AssertionError("the relay never drained against the restored database")

        for consumer in sorted(consumer_registry.registered_names()):
            with _bound(factory) as session:
                relay.replay(session, consumer=consumer, from_position=0)
                session.commit()
    finally:
        engine.dispose()

    exports_after = _restored_scalar(url, "SELECT count(*) FROM inclusion_exports WHERE review_id = :r", r=review_id)
    decisions_after = _restored_scalar(
        url, "SELECT count(*) FROM screening_decisions WHERE review_id = :r", r=review_id
    )
    reviewer_b_decisions = _restored_scalar(
        url,
        "SELECT count(*) FROM screening_decisions WHERE review_id = :r AND reviewer_subject_id = :s",
        r=review_id,
        s=REVIEWER_B,
    )
    duplicate_reviewer_rows = _restored_scalar(
        url,
        "SELECT count(*) FROM (SELECT 1 FROM screening_decisions WHERE review_id = :r "
        "GROUP BY review_record_id, reviewer_subject_id, supersedes_decision_id HAVING count(*) > 1) d",
        r=review_id,
    )
    checkpoints = _restored_scalar(url, "SELECT count(*) FROM projection_checkpoints")

    assert exports_after == exports_before, (
        f"a restore plus a full replay sealed a second export: {exports_before} -> {exports_after}"
    )
    assert decisions_after == decisions_before, (
        f"a restore plus a full replay recorded a second decision: {decisions_before} -> {decisions_after}"
    )
    assert reviewer_b_decisions > 0, "no decision is attributed to the second reviewer; the count above is not blinded"
    assert duplicate_reviewer_rows == 0, (
        f"{duplicate_reviewer_rows} (record, reviewer) pairs hold more than one decision after a restore and replay"
    )
    assert checkpoints == 0, (
        "projection_checkpoints is no longer empty. This service has no projector — see this module's "
        "docstring — so either one has landed and this file now owes a real rebuild-checksum proof, or "
        "something is writing checkpoints that nothing reads."
    )
