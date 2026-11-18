# Evidence Review 8810

The system of record for **human judgement** in an evidence review: a
protocol-locked review, reversible deduplication, two blinded reviewers,
append-only decisions, conflict adjudication, a derived flow ledger, and an
inclusion-set export sealed with the canonical hash.

Everything else the platform already does — federated acquisition with PRISMA
identification counts (`discovery-engine`), ingest, the graph, ask-scoped-to-a-
document-set, meta-analysis and the forest plot — exists and is live. This
service is the piece between "we found papers" and "we can defend which ones we
included".

**Scope: `ER-001..ER-005` + the `OUT-001` contract.** Specification:
`DataCompany/implementation-plans/01-evidence-review-8810.md`, exit gate
`DataCompany/11-delivery-plan-and-acceptance.md:171`.

## Deliberately not here

| Not built | Why |
|---|---|
| ~~`research_output_service` (8811) as a repo~~ | ⚠️ **RETRACTED 2026-09-02 — it exists and is LIVE.** This row said `ER-005.5` made the flow/decision-ledger *"a contract on 8810, not a service"*. That reading was of `ER-005`, and `OUT-001` is a different package: `8811` shipped on 2026-09-02, runs 1/1 in the `research-output` namespace, and compiles this service's flow ledger into hash-pinned HTML/JSON/CSV. What `ER-005.5` actually requires is that the LEDGER is published from here — which it still is, and `8811` consumes it rather than deriving it. |
| `FND-003..FND-015` | ⚠️ **CORRECTED 2026-09-02.** This row claimed *"the jobs and outbox tables exist"*. Measured against the production `evidence_review` database: 26 tables, and `domain_outbox`, `durable_jobs` and `job_attempts` are **not** among them — only `operation_idempotency` (`0001`) is. No migration ever created the other three. No relay runs, which `01-…:349-350` permits; but the tables are absent, not idle. This is why `ER-006` emits none of its five allocated events: there is nowhere to write them, and creating the outbox is `FND-003`. |
| XLSX import (`ER-002.2`) | Its only named consumers are Catchii/Cachii workbooks, blocked on `AHTA-DISC-003/004`. RIS, BibTeX, CSV and canonical JSON only. |
| Automatic exclusion (`ER-008` ranking) | Out of scope entirely, so `01-…:609` holds *by construction*: there is no code path that writes a decision without an authenticated human subject. |
| `ER-006`'s proposal lane (`ER-006.3`) | `INT-KARMA`'s immutable `ProposalBundle` contract does not exist, so a `proposal_decisions` table would have no writer. The human-authored half of `ER-006` IS built — see below. |
| An observation backed by an exact `EvidenceLocation` | `16-…:§7.2` gives `EvidenceLocation` to Ingestion/Documents, and `8810` has no client that can verify a `PinnedVersionRef` into it. `DM-AT-25` requires an unverifiable pinned ref to fail closed, so the arm is declared in the schema and refused by `ck_observation_located_arm_unverifiable`. The expert-assertion arm of `§3.7` is the one that works today. |
| Appraisals (`ER-006.6`, `ER-M019`) | Deferred with the rest of `ER-006`'s instrument lane. RoB 2 / ROBINS-I / GRADE domain keys and judgement scales are controlled vocabularies, and the terminology binding they need is `AHTA-DISC-008` discovery-blocked. A free-text `judgement` column is a closed vocabulary with nothing in it. |
| Any canonical regulatory surface | `ADR-DATACOMPANY-REGULATORY-MODEL` is unaccepted and the pre-ADR denylist is enforced in CI. A regulatory PDF imports as an ordinary record with `record_type='report'` — that is the `DM-AT-18` positive control, not an exception to it. |

## Three invariants, and where each is enforced

**1. A machine cannot decide.** `src/actor.py`. Screening automation ranks and
proposes; inclusion and exclusion are attributable human judgements
(`12-…:121-124`). A caller presenting a tenant but no subject is refused at that
seam, so "no automatic exclusion path exists" is a property of the code rather
than a claim about it.

**2. Reviewer B cannot see reviewer A.** Six layers, and any one alone leaks
(`ER-004.2`, `01-…:471-472` — repositories, list/detail queries, *cursors,
counts, errors, events and exports*):

| Layer | Mechanism |
|---|---|
| L1 | Postgres RLS with `FORCE ROW LEVEL SECURITY`, app connecting as a **non-owner** role |
| L2 | One read function; a test greps the source and fails on any raw select elsewhere |
| L3 | The queue returns the caller's own assignments only — never "1 of 2 done", which is a decision leak |
| L4 | Cursors run over own assignments, not over records, whose gaps reveal the other reviewer |
| L5 | `404` and never `403` (a 403 confirms existence); events carry no disposition or reviewer; logs are redacted |
| L6 | A caller who still owes a decision reads no AGGREGATE of the work they are blinded from — `flow.require_no_owed_decisions` |

Governed unblinding is the only door, and it writes an audit row first.

L6 is a caller property, not a role: `require_review_lead` grants on
`reviews.created_by_subject_id`, so "lead only" was never a blinding boundary
and the creator of a review may also be screening it. It covers every route that
derives the ledger — `GET /flow`, `flow:rebuild`, `flow:audit`, `POST /exports`
and the three export READS, since a seal taken earlier by a lead who owed
nothing carries the same counts and cannot be deleted (`inclusion_exports` is
append-only). It lifts the moment that caller records the verdicts they owe; a
lead who is not screening never meets it.

**3. History is append-only.** Enforced by `BEFORE UPDATE OR DELETE … RAISE
EXCEPTION` triggers, in the database, not in the application. There is no
`is_current` column anywhere: "current" is the tail of a supersession chain, made
linear and deterministic by partial unique indexes. A merge is undone by a *new*
split row; source records are never mutated or deleted.

## `ER-006` — what a review accepts, and who says so

Screening decides *which reports are in*. `ER-006` records *what those reports
say*, so a review stops being a filter and becomes evidence. Eleven tables, five
routes, migrations `0017` and `0018`.

**A study is not a report.** `ResearchObject` groups the reports of one study — a
registration, a primary paper, two secondary analyses — which is the distinction
`flow_derivation` says it cannot make, and the reason "studies included in
review" has never been derivable here. Exactly one linked report is `primary`;
a study with two has no citation and a study with none cannot be cited.

**Only included reports.** A research object may only be built from a record the
review actually included. Inclusion is derived, never stored, so the check calls
`flow.included_record_ids` — the same `derive()` the PRISMA ledger and the sealed
export call. It is deliberately NOT a PL/pgSQL trigger: that would be a second
implementation of supersession, agreement, adjudication and stage ordering, in a
language with no tests against generated histories.

**Recording is a reviewer's work; accepting is the lead's.** `POST /acceptances`
is the only route behind the lead gate. Acceptance is not a column on a version —
it is an append-only row over an exact version, carrying that version's content
hash as a *witness*, so "accepted" means "accepted this content" even after the
decision travels into an export and leaves the version table behind. A withdrawal
is a new row citing the one it withdraws; re-accepting continues the chain.

**Nothing here can be rewritten.** All eleven tables refuse `UPDATE` and `DELETE`
by trigger, and `tests/security/test_accepted_evidence_cannot_be_rewritten.py`
proves the refusals are not vacuous by disarming each guard inside a rolled-back
transaction and showing the tamper would otherwise land.

### Routes

| Route | Who |
|---|---|
| `POST/GET /api/reviews/{id}/research-objects` | any reviewer on the review |
| `POST /api/reviews/{id}/research-objects/{id}:revise` | any reviewer |
| `POST /api/reviews/{id}/research-objects/{id}/extractions` | any reviewer |
| `POST /api/reviews/{id}/assertions` | any reviewer |
| `POST/GET /api/reviews/{id}/acceptances` | **lead only** |

No `PUT`, no `DELETE`: a correction is a new version plus a supersession.

## Ports, database, roles

- Port **8810** (document 09). Reached only through the `kag` gateway at
  `/reviews` — no public tunnel.
- Database `evidence_review` on Postgres 18, reached through a connection
  pooler in front of the primary.
- **Two roles.** `evidence_review` owns the schema and runs migrations;
  `evidence_review_app` runs the service. `FORCE ROW LEVEL SECURITY` binds
  policies to the owner too, but only a non-owner is unable to
  `ALTER TABLE … DISABLE ROW LEVEL SECURITY` at all — and blinding is those
  policies.
- psycopg v3, `prepare_threshold=0`. Every GUC is set with
  `set_config(…, is_local => true)`, never `SET`: PgBouncer reuses server
  connections, and a session-scoped actor GUC is a blinding breach that is
  intermittent and invisible.

## API

Routes are **unversioned**: `/api/<resource>`, never `/api/v1/`.

**Licence + `X-Actor-Token`.** A caller presenting a tenant's `evidence-review`
licence on `Authorization` may name the human who acted with a Keycloak access
token (`typ` Bearer, `azp` on `ER_ACTOR_TOKEN_AZP`) in `X-Actor-Token`; the
tenant always comes from the licence. The header is ignored beside a JWT, in
header mode, and beside a licence on `X-Research-Context`. ⚠️ ER does **not**
check that the actor is a member of the licence's tenant: the trust boundary is
the licence holder — only the tenant's app (kag-frontend) can mint that licence,
and it enforces membership before attaching `X-Actor-Token`.

### Evidence snapshots (`ER-007`)

`POST /api/reviews/{id}/evidence-snapshots` builds an immutable, content-hashed
manifest of the exact evidence versions one run may read.
`GET …/evidence-snapshots/{id}` returns it with the manifest **reproduced from
its member rows**, refusing with `409` if the recomputed hash differs from the
sealed one — `14-…:364`'s byte-identical-reconstruction criterion is checked on
every read rather than only in a test.

The manifest grammar is released:
`urn:datacompany:evidence-snapshot-manifest:1` (`research_contracts` ≥ 0.6.0), and
the hash is taken under that document's own hash profile, not with a plain
canonical hash. That matters: each member carries a `resolved_at` annotated
`x-datacompany-hash-excluded`, so an unprofiled hash would give two snapshots of
identical evidence, taken a second apart, different `content_hash` values — and
re-running a published analysis would look like tampering.

Three limits, stated rather than discovered:

* **`201`, not `202`.** A durable job IS created and is readable at
  `GET /api/review-jobs/{job_id}`, so the idempotency and the trace-table
  endpoints are real — but it is executed inline. A worker that picks the job up
  later has to be able to find its input, and `13-…:§7.3` puts an input that
  size in an artifact store, which is `FND-009` and does not exist.
* **Two of five gates run.** `14-…:345` names five things a snapshot must
  reject. `unresolved_conflicts` and `uncommitted_protocol` exist here;
  `unresolved_critical_proposals`, `denied_rights` and `stale_mandatory_evidence`
  have no subject matter in this service at all. `evidence_snapshots.gates_evaluated`
  records which ones actually ran per snapshot, and the API returns
  `fully_gated` (always `false` today) plus `gates_not_run`. A consumer reads
  the ledger; it never assumes five. `domain/policies/snapshot_gates.OWED_BY`
  names the package that owes each.
* **Four member kinds, not six.** `14-…:344` lists six. Assertion versions are
  not in `ACCEPTANCE_SUBJECTS`, so none has ever been accepted and pinning one
  would pin an unaccepted input; appraisal and terminology versions have no
  table here. The released manifest already admits all six — the gap is this
  producer's, not the grammar's.

### Durable jobs (`FND-006`) — ⛔ read this before using `/api/review-jobs`

`GET /api/review-jobs/{job_id}` and `POST /api/review-jobs/{job_id}:cancel`
exist and work. `durable_jobs` and `job_attempts` are live, the state machine is
enforced by triggers and CHECKs in `0020`, and `application/services/jobs.py`
provides `enqueue`, `claim_next`, `heartbeat`, `wait_for`, `reconcile_waiting`,
`finish` and `reap_expired_leases` with a two-worker suite behind them.

**Nothing runs them on a loop, and nothing enqueues yet.** There is no worker
process and no route that creates a job: `FND-006` is the substrate, and the five
registered job types are all still synchronous in-request today. Each producer
package converts its own operation and lands its own worker entrypoint —
`domain/policies/job_catalog.OWED_BY` names which package owes which type
(`ER-002` import, `ER-003` deduplication, `ER-007` evidence snapshot, `ER-008`
prioritization, `ER-009` export). So a job read through these routes is durable,
attributable and cancellable; it is **not** necessarily progressing.

Two limits recorded rather than papered over:

* `reconcile_waiting` returns a parked job to `queued` but does **not**
  reauthorize it, which `13-…:§7.1` asks for. Authorization here runs off an
  `Actor` built from a verified credential, a job stores an identity *reference*
  and never a credential (`§9.3`), and there is no delegation-grant mechanism
  yet (`FND-004`). A job parked before a right was revoked therefore resumes
  without noticing. The function says so in its own docstring.
* The additive `*_worker` RLS policies that grant cross-tenant queue discovery
  are created by `0020` and `0022` **only where `evidence_review_worker` already
  exists** — they `RAISE NOTICE` and continue when it does not, so creating the
  role afterwards does not retroactively create them. `0023` creates them
  idempotently for exactly that case.

  **This used to say "no environment provisions it today, so the queue is
  currently per-tenant everywhere". That is false.** Measured on a production
  primary (database `evidence_review`):
  `evidence_review_worker` exists and holds `LOGIN`, and seven `*_worker`
  policies are live — `domain_outbox`, `research_domain_events`,
  `event_deliveries`, `domain_inbox`, `durable_jobs`, `job_attempts` and
  `artifact_records` — each `TO evidence_review_worker USING (true)`. Cross-tenant
  discovery is provisioned there, not absent.

  The limit worth recording is a different one: **nothing binds a process to a
  role.** The API and the worker read the same `DATABASE_URL`, and only the
  worker checks what it got — `worker.preflight` refuses to start unless
  `current_user` is `evidence_review_worker` and every policy in
  `CROSS_TENANT_TABLES` is present. The API asserts nothing about `current_user`,
  so pointing it at the worker role would serve ordinary API requests under
  `USING (true)` on those seven tables, and no check anywhere would notice.

### The worker's heartbeat — how to tell a working loop from a wedged one

`evidence-review-worker` runs `worker.run()`: relay + lease reaper, every 5s. Its
observability gap was total and was measured, not assumed:

* no HTTP server, therefore **no liveness probe**;
* **no `ServiceMonitor`, no `PodMonitor`, no `Service`** in the `evidence-review`
  namespace (`kubectl -n evidence-review get servicemonitor,podmonitor` →
  *No resources found*), therefore no metric;
* its entire output in nine hours was two lines — `created SQLAlchemy engine` and
  `worker started: …` — because the loop logged only a tick that moved something,
  and all four relay tables hold zero rows;
* `run()` swallows a tick failure and keeps looping, so a stalled loop emits
  nothing and restarts nothing.

So **"working, queue empty" and "hung" were observationally identical.** They are
not any more:

* every **completed** tick writes `WORKER_HEARTBEAT_PATH`
  (default `/tmp/evidence-review-worker/heartbeat.json`) — after the work, never
  before, because a heartbeat written above `tick()` advances at full cadence
  through a query blocked on a lock forever;
* `python -m worker_heartbeat` exits `0` if that file is fresher than
  `WORKER_HEARTBEAT_STALE_MULTIPLE` (default `6`) × **the tick interval the loop
  itself recorded**, floored at `worker_heartbeat.MIN_STALENESS_SECONDS` (20s).
  At the default 5s cadence the bound is **30s**; at `WORKER_INTERVAL_SECONDS=60`
  it is 360s. ⛔ A constant bound would kill a 60s worker on its first tick;
* the loop logs `heartbeat: ticks=… relayed=… deliveries=… poisoned=… reaped=…
  errors=…` every `WORKER_HEARTBEAT_LOG_SECONDS` (default **300**), plus once on
  the first completed tick, so *alive and idle* is legible in the log stream and
  distinguishable from silence. 300s is 288 lines a day; one line per tick would
  be 17,280 of `relayed=0`, which is the reason the previous version logged
  nothing at all.

`worker.py --healthcheck` does the same check, and is there because `worker.py`
is the file an operator has in their hand — but it imports SQLAlchemy, the ORM
and every application service on the way in. **Wire the module, not the flag:**

```yaml
# clusters/strato/services/evidence-review/worker-deployment.yaml
livenessProbe:
  exec:
    command: ["python", "-m", "worker_heartbeat"]
  # ⚠️ No readinessProbe and no startupProbe: this workload has no Service and
  # serves no traffic, so "ready" has no meaning here. `initialDelaySeconds`
  # covers the start-up window instead.
  initialDelaySeconds: 60   # preflight (six policy checks + eleven privilege
                            # checks) plus the first tick, with room to spare.
  periodSeconds: 15
  timeoutSeconds: 10        # the probe imports stdlib + config only.
  failureThreshold: 4       # 30s bound + 4 x 15s => ~90s of NO COMPLETED TICK
                            # before a restart. Deliberately generous: a tick
                            # that raises does not advance the heartbeat, so a
                            # database blip must not be able to bounce the pod.
```

⚠️ `WORKER_HEARTBEAT_PATH` and friends must be set on the **container**, not
exported from an entrypoint script — the probe `exec`s a fresh process and reads
the container environment, so anything the entrypoint invents is invisible to it
and the two would silently disagree about the path. The default needs no env var.

`/tmp` is the right default: the image runs as `appuser`, the heartbeat is
per-pod state that must not survive a restart (one inherited from the previous
process would be a lie about this one), and nothing off-pod reads it.

### Prometheus — and why there are TWO endpoints, exporting different things

⚠️ The bullet above — *"no `Service`, no `ServiceMonitor`, no `PodMonitor`,
therefore no metric"* — described the whole service until this landed. The
heartbeat closed **liveness**; a stalled or poisoned **relay** was still a silent
loss of domain events, because `up{namespace="evidence-review"}` was EMPTY in the
estate's Prometheus and every series carrying that namespace came from
kube-state-metrics watching from outside.

| | API (`:8810/metrics`) | worker (`:9810/metrics`) |
|---|---|---|
| `evidence_review_build_info{component=…}` | ✅ `api` | ✅ `worker` |
| `evidence_review_relay_depth_observable{reason=…}` | ✅ always | ✅ always |
| `…_relay_outbox_unrelayed_rows` / `…_deliveries_pending_rows` / `…_deliveries_poisoned_rows` | ⛔ **absent** | ✅ |
| `…_worker_ticks_total`, `…_relay_events_relayed_total`, `…_relay_deliveries_created_total`, `…_relay_deliveries_poisoned_total`, `…_worker_leases_reaped_total`, `…_worker_tick_errors_total` | ⛔ **absent** | ✅ |
| `…_worker_heartbeat_status` / `…_age_seconds` / `…_staleness_bound_seconds` | ⛔ absent (no heartbeat file) | ✅ |

**⛔ Every ⛔ in that table is a deliberate ABSENCE, never a zero, and that is the
one design decision worth reading before changing anything here.**

* **The counters** are `worker._Counters`, an object in the *worker's* memory.
  The API is a separate Deployment; a `Counter` registered there reads `0` for
  the life of the pod — a flat line indistinguishable from a healthy idle relay,
  which would stay flat through a relay that died a week ago. So they are
  registered only when `build_registry(counters=…)` is given a tally, which only
  `worker.run()` does.
* **The depths** are a database fact the API's *role* may not observe.
  `domain_outbox`, `research_domain_events` and `event_deliveries` carry `FORCE
  ROW LEVEL SECURITY` with a `current_setting('app.current_tenant', true)::uuid`
  policy; a scrape has no request, hence no actor, hence no tenant GUC, so the
  comparand is `NULL` and `count(*)` is `0`. Measured on a database holding 1,703
  un-relayed rows: as `evidence_review_app`,
  `evidence_review_relay_depth_observable{reason="row_level_security"} 0` and no
  depth sample at all; on a connection that can see, `{reason="observable"} 1`
  and `evidence_review_relay_outbox_unrelayed_rows 1703`. `RelayDepthCollector`
  asks Postgres that question on **every scrape** rather than trusting a role
  name, so the same code is correct in both processes — which matters because
  nothing binds a process to a role here (see the note above).

`/metrics` on the API is **exempt from license-authz by the same mechanism as
`/health`**: there is no auth middleware and no router-level `dependencies=[…]`
in this service, so a route is authenticated iff its signature asks for an
`Actor`. `metrics()` does not. `tests/api/test_metrics_is_exempt_from_license_authz.py`
pins it with a positive control — the same uncredentialed client must get `401`
from `/api/reviews` first, or the 200 proves nothing.

`WORKER_METRICS_PORT=0` disables the worker's listener; a bind failure is logged
and never fatal, because a worker that refuses to relay over an observability
port has turned a missing dashboard into an outage.

⚠️ **Still owed, and deliberately not in this change:** the `evidence-review`
namespace has no worker `Service` and no `ServiceMonitor`/`PodMonitor` on either
workload, so nothing is scraping these yet. Both are `gitops-production` changes.

## Local development

Bring up a dedicated Postgres and create **both** roles. The blinding suite is
only meaningful against the non-owner role: as the owner, `FORCE ROW LEVEL
SECURITY` is still bypassable by `ALTER TABLE … DISABLE ROW LEVEL SECURITY`, so
a suite run as the owner can pass against no enforcement at all.

```bash
# 5446, not 5432 — see .env.example. Check the port is free first; several
# agents share this host and each one's container is somebody else's data.
docker run -d --rm --name er-local-pg -p 5446:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=evidence_review postgres:18

docker exec -i er-local-pg psql -U postgres -d evidence_review <<'SQL'
CREATE ROLE evidence_review     LOGIN PASSWORD 'local-owner';
CREATE ROLE evidence_review_app LOGIN PASSWORD 'local-app';
ALTER DATABASE evidence_review OWNER TO evidence_review;
GRANT USAGE ON SCHEMA public TO evidence_review_app;
ALTER SCHEMA public OWNER TO evidence_review;
SQL
```

```bash
cp .env.example .env      # then fill in the two passwords set above
uv sync
uv run alembic upgrade head
PYTHONPATH=src uv run uvicorn app:create_app --factory --host 127.0.0.1 --port 8810
```

There is no `main.py` and no module-level `app` — the application is built by the
factory `create_app` in `src/app.py`, and `src` is not a package root on
`sys.path` by default. `uvicorn main:app` fails with `ModuleNotFoundError`.

`research-contracts` resolves from its public source at the tag named in `pyproject.toml`.

## Tests

```bash
uv run pytest
```

Blinding tests carry `@pytest.mark.blinding` and each must fail when its layer is
removed. A blinding suite that stays green with a layer deleted is not a blinding
suite.

### The isolation gate

Twenty-eight test files open with `skipif(not DATABASE_URL.startswith("postgresql"))`.
Each is right on its own; together they were a hole, because the guard was
conditioned on the very precondition whose absence it existed to catch. Measured
on `c036c3a`:

```
$ env -u DATABASE_URL -u MIGRATION_DATABASE_URL uv run pytest -q
661 passed, 220 skipped, 1 warning in 35.74s     → EXIT 0
```

Every tenant-isolation, blinding, RLS and E2E-002 assertion became a skip and
pytest reported success. A session hook in `tests/conftest.py` now counts what
actually ran and exits **6** — a code pytest itself never uses — when the
load-bearing suites did not. The same command is now:

```
rls-enforcement             8     0      6
reviewer-blinding          27     0     20
e2e-002-acceptance         40     0     30
review-scoping             19     0     14
GATE INTEGRITY FAILURE …                         → EXIT 6
```

Three rules, of which exactly one is waivable:

1. every load-bearing test that was **collected** must have **run** — including
   a `pytest.skip()` from inside a test body, which is the same false green one
   frame lower;
2. **no** test anywhere may be skipped for want of a database;
3. each selection must have run at least its floor.

`ERS_NO_ISOLATION_COVERAGE=1` waives rule 3 and only rule 3. That is the
unit-only lane — `ERS_NO_ISOLATION_COVERAGE=1 uv run pytest tests/unit`, which is
what the `pytest-unit` pre-push hook runs. It cannot green a full run without a
database, because rules 1 and 2 still fire; and whenever it is set, the run says
so in its own output.

## Licensing

This service verifies caller licences against the platform licence service; it
does not mint licences and ships no licence server of its own. Nothing in the
application, the migrations or the test suite requires a licence key to run:
`ENABLE_AUTH` defaults to off in tests, and enabling auth only turns on token
verification against a verifier the deployment configures.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
