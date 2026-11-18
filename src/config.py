"""Configuration for Evidence Review 8810."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _get_env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_positive_float(name: str, default: float) -> float:
    """A float that must be greater than zero, or the default.

    Tolerant in the same direction as `_get_bool` and for the same reason: every
    caller is an observability knob whose default is the safe answer, so a typo
    degrades to today's behaviour rather than to an unbounded one. A `0` or a
    negative would be worse than a typo — a zero staleness multiple makes every
    probe fail and a zero log interval logs every tick — so both fall back too.
    """
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _get_csv(name: str, default: str) -> tuple[str, ...]:
    """A comma-separated list, each item stripped, empty items dropped.

    Unset reads `default`; set-but-empty reads as the EMPTY tuple, so an operator
    can switch a list off explicitly rather than only by deleting the variable.
    """
    value = os.getenv(name)
    raw = default if value is None else value
    return tuple(item.strip() for item in raw.split(",") if item.strip())


#: The one vocabulary both boolean readers below accept. Written once so the
#: tolerant and the strict reader can never drift into accepting different words.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return default


def _get_sealed_bool(name: str, default: bool) -> bool:
    """Like :func:`_get_bool`, but a value it cannot read is FATAL, not the default.

    Only for values that end up inside a hash-sealed artifact.

    `_get_bool` falling back is right for every other flag here, and the
    deployment manifest says why in as many words: "`_get_bool` falls back to the
    default on anything it cannot parse, so a typo here degrades to today's
    behaviour rather than to open". That argument holds because `ENABLE_AUTH`
    defaults to the SAFE answer — a typo turns authentication on.

    It does not transfer to `shadow_mode`. That field defaults to `False`, and
    `False` is the unsafe answer for the only deployment that ever sets the
    variable: a rehearsal cluster carrying `SHADOW_MODE: "shadow"` would seal
    `false` into `manifest_hash`, the export would self-verify, `inclusion_exports`
    is append-only, and nothing downstream could ever tell the artifact from a
    real one. A silent fallback is exactly how a wrong-but-plausible env value
    flips the meaning of a sealed artifact.

    So it refuses at construction: `get_settings()` is called from `create_app`,
    so an unreadable value is a pod that will not start, named in its own logs,
    rather than a run of exports that lie.
    """
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    accepted = ", ".join(sorted(_TRUE | _FALSE))
    raise ValueError(
        f"{name}={value!r} is not a boolean this service will guess at. It is sealed into the "
        f"inclusion-set export manifest, where a wrong value is uncorrectable, so it is refused "
        f"rather than defaulted. Accepted: {accepted} (case-insensitive); unset means {default}."
    )


@dataclass
class Settings:
    # Core
    version: str = field(default_factory=lambda: _get_env("EVIDENCE_REVIEW_VERSION", "0.1.0"))
    service_name: str = field(default_factory=lambda: _get_env("EVIDENCE_REVIEW_SERVICE_NAME", "evidence-review"))
    environment: str = field(default_factory=lambda: _get_env("ENVIRONMENT", "production"))
    enable_docs: bool = field(default_factory=lambda: _get_bool("ENABLE_DOCS", False))
    enable_auth: bool = field(default_factory=lambda: _get_bool("ENABLE_AUTH", True))

    # THE SEALED ONE. Whether this deployment is a rehearsal.
    #
    # It is not a flag that changes what the service DOES — nothing branches on
    # it. It is a fact the service states about itself, and `create_export` seals
    # that statement into `manifest.shadow_mode`, a required member of both
    # `urn:datacompany:inclusion-set-export:1` and `:2`. The schema's own $comment is
    # the specification: "Whether this export was produced while the deriving
    # stage was running in shadow. It is a property OF the export and belongs in
    # the sealed manifest: an export that silently changed its shadow status
    # after the fact would otherwise reproduce the same hash."
    #
    # It was a hardcoded `True` in `flow.py` with nothing behind it — no field,
    # no variable, no review state — so the first real inclusion set would have
    # sealed itself a rehearsal artifact inside `manifest_hash` and `items_hash`,
    # uncorrectable on an append-only table, and a consumer branching on the
    # member (the reason it exists) would have discarded every real export.
    #
    # DEFAULT FALSE, and production therefore declares nothing. That direction is
    # deliberate: "no configuration" has to mean the honest answer for a real run,
    # so the burden is on a rehearsal deployment to say what it is. The gitops
    # manifest needs NO corresponding env var — `clusters/strato/services/
    # evidence-review/deployment.yaml` sets nothing and gets `false`, and a
    # redundant `SHADOW_MODE: "false"` there would be one more line to keep in
    # step with this one.
    #
    # NOT a per-review column, which is the other place it could live. That would
    # be the better model if reviews could be individually rehearsed, but it needs
    # a migration and a UI affordance to set it, and neither exists; a column
    # nothing can write is a NULL that has to be defaulted here anyway.
    #
    # `_get_sealed_bool`, not `_get_bool` — see that function for why a value this
    # one cannot parse is fatal rather than defaulted.
    shadow_mode: bool = field(default_factory=lambda: _get_sealed_bool("SHADOW_MODE", False))

    # Server. 8810 is the port document 09 assigns Evidence Review; verified free
    # against every containerPort in clusters/strato/services (8801-8809 and 8900
    # are taken).
    # 0.0.0.0 is correct here and is not a hardcoded exposure: the process runs in
    # a pod whose only route in is a ClusterIP Service, and binding 127.0.0.1
    # would make the container unreachable from kubelet's probes and from the
    # Service alike. Exposure is controlled by the NetworkPolicy (10.42.0.0/16
    # only) and by there being no public tunnel, not by the bind address.
    # `# noqa: S104` is ruff's directive; bandit needs its own.
    host: str = field(
        default_factory=lambda: _get_env("EVIDENCE_REVIEW_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
    )
    port: int = field(default_factory=lambda: _get_int("EVIDENCE_REVIEW_PORT", 8810))

    # Database.
    #
    # TWO ROLES, deliberately. Migrations run as the OWNER; the application
    # connects as a NON-owner. `ALTER TABLE ... FORCE ROW LEVEL SECURITY` is what
    # makes RLS apply to the table owner too, but running the app as the owner
    # would still leave every policy one `ALTER TABLE ... DISABLE` away from a
    # bug rather than a privilege error. Reviewer blinding (ER-004.2) is enforced
    # in the database first, so the runtime role must not be able to turn it off.
    database_url: str = field(
        default_factory=lambda: _get_env(
            "DATABASE_URL",
            "postgresql+psycopg://evidence_review_app@localhost:5432/evidence_review",
        )
    )
    migration_database_url: str = field(default_factory=lambda: _get_env("MIGRATION_DATABASE_URL", ""))
    db_pool_size: int = field(default_factory=lambda: _get_int("DB_POOL_SIZE", 10))
    db_max_overflow: int = field(default_factory=lambda: _get_int("DB_MAX_OVERFLOW", 5))
    #: libpq's `connect_timeout`, in seconds. **It had no value at all, which
    #: means libpq's default: wait forever.** A connect that waits forever is
    #: not a slow path, it is a held pool slot plus a held worker thread, and
    #: with `DB_POOL_SIZE=2`/`DB_MAX_OVERFLOW=2` on the worker (1/1 in
    #: production today) two of those are the entire pool.
    #:
    #: `2` is libpq's FLOOR, not a preference: the documented behaviour is that
    #: any positive value below 2 is raised to 2, so this is the shortest bound
    #: that can be expressed. Both processes reach Postgres through
    #: the database host, which accepts in-cluster TCP in microseconds;
    #: a connect that has not completed in two seconds is a broken pool, and
    #: `pool_pre_ping` means the next checkout tries again.
    #:
    #: ⚠️ It does NOT bound name resolution. psycopg3 resolves the host in
    #: Python (`_conninfo_attempts._resolve_hostnames` -> `socket.getaddrinfo`)
    #: BEFORE it hands anything to libpq, and `getaddrinfo` takes no timeout.
    #: Measured on a host whose internal DNS resolvers are unreachable,
    #: `getaddrinfo("db.example", 5432)` raised after **30.016 s** —
    #: which is why `metrics.RelayDepthCollector` carries a wall-clock deadline
    #: as well and does not rely on this value alone.
    db_connect_timeout_seconds: int = field(default_factory=lambda: _get_int("DB_CONNECT_TIMEOUT_SECONDS", 2))
    #: `statement_timeout`, in milliseconds, applied with `SET LOCAL` as the
    #: FIRST statement of every scope in `database`. **There was no such bound
    #: anywhere outside `metrics.py`**, which means every one of the 52
    #: `actor_scope` call sites in `api/routers/` could run a statement forever.
    #:
    #: ⚠️ A ceiling is not a deadline. These 52 sites still have no wall clock,
    #: so a stalled resolver blocks them for the full 30.016 s `getaddrinfo`
    #: measurement with this value in force. See `database.actor_scope` for the
    #: residual in full, including what it does to `health.ready()`.
    #:
    #: ## Why `SET LOCAL` in the scope rather than on the engine
    #:
    #: The obvious spelling is libpq's `options=-c statement_timeout=…` in
    #: `connect_args`, one line, every connection covered. ⛔ It is the wrong one
    #: here: `options` travels in the **startup packet**, and the API reaches
    #: Postgres through PgBouncer in transaction-pooling mode. A startup
    #: parameter PgBouncer does not carry is a connection that does not open —
    #: a failed bound that presents as a total outage. `SET` on a pooled
    #: connection is worse still: transaction pooling hands that server
    #: connection to the next client with the setting still on it.
    #:
    #: `SET LOCAL` is scoped to the transaction and discarded at commit, which
    #: is the same property that makes `actor_scope`'s tenant GUC safe behind
    #: the pooler. One place, every scope, nothing leaks.
    #:
    #: ## Why 30 000 ms
    #:
    #: It is a backstop against a statement that never returns, NOT a latency
    #: target — every real statement in this service is an indexed read or a
    #: bounded batch and runs in single-digit milliseconds. The bounded paths
    #: that have a latency requirement pass their own, much tighter, value.
    #:
    #: 30 s is SQLAlchemy's `pool_timeout` default, which `app.py` already
    #: reasons about: a caller gives up waiting for a *connection* at 30 s, so a
    #: *statement* allowed to outlive that is work whose requester is already
    #: gone, still holding the slot the next one needs.
    #:
    #: ⚠️ It does NOT apply to migrations. `alembic/env.py` builds its own
    #: engine with `engine_from_config` and never calls `session_scope`, so a
    #: long `ALTER TABLE` is unaffected — checked before this value was chosen.
    db_statement_timeout_ms: int = field(default_factory=lambda: _get_int("DB_STATEMENT_TIMEOUT_MS", 30000))

    # Upstreams
    discovery_engine_url: str = field(default_factory=lambda: _get_env("DISCOVERY_ENGINE_URL", "http://localhost:8081"))
    #: This service's OWN licence (subject `svc-evidence-review`) for the
    #: discovery-engine hop, sent as `Authorization: Bearer <token>` next to the
    #: unchanged bare `X-Tenant-Id: <actor's tenant>`. That is discovery-engine's
    #: on-behalf shape (`auth/vouch.py` under observe, the `Authorization`
    #: carrier's on-behalf row under `AUTH_TOKENLESS_IDENTITY_MODE=enforce`): the
    #: licence proves WHO is asking, the plain header names the customer tenant
    #: it acts for, and the subject must be on `DISCOVERY_ENGINE_ONBEHALF_SUBJECTS`.
    #:
    #: Default EMPTY = today's request, byte for byte (no `Authorization` at all).
    #: Whitespace-only counts as empty. `repr=False` so a logged `Settings` never
    #: carries the credential.
    discovery_engine_license_token: str = field(
        default_factory=lambda: _get_env("DISCOVERY_ENGINE_LICENSE_TOKEN", "").strip(),
        repr=False,
    )
    #: The Artifact Service, which `adapters/artifact_store.py` calls. NEVER the
    #: Storage Service and never MinIO: `13-…:739-741` makes the chain
    #: domain -> Artifact -> Storage -> MinIO strict, and Storage is the only holder
    #: of MinIO credentials (`artifact_service/src/services/storage_service.py:26-31`).
    artifact_service_url: str = field(default_factory=lambda: _get_env("ARTIFACT_SERVICE_URL", "http://localhost:8082"))
    http_timeout_seconds: int = field(default_factory=lambda: _get_int("HTTP_TIMEOUT_SECONDS", 30))

    # Auth (Keycloak portal realm; a human subject is mandatory for any decision)
    keycloak_url: str = field(default_factory=lambda: _get_env("KEYCLOAK_URL", "https://auth.nonagentic.ai"))
    keycloak_realm: str = field(default_factory=lambda: _get_env("KEYCLOAK_REALM", "portal"))

    # License-token introspection — the SECOND credential shape this service is
    # driven with, and the one it could not read at all.
    #
    # A Keycloak access token is a JWT and `auth/verifier` checks its signature.
    # The fleet's licence token is not: `KAG_ONBEHALF_TOKEN`, the credential the
    # gateway actually presents to 8810, is 43 opaque characters with **zero
    # `.` separators**. `PyJWKClient.get_signing_key_from_jwt` answered it with
    # `Not enough segments` and every `GET /reviews/*` 401'd — not because the
    # credential was bad, but because an opaque licence token is a different
    # credential shape, and this service had never learned to introspect one.
    # There is nothing in it to verify locally: the only way to learn its tenant
    # and subject is to ask the licenses service.
    #
    # `clusters/strato/services/evidence-review/deployment.yaml` has set this
    # variable all along, with a comment naming the same omission in
    # discovery-engine, scientific-processor (#2235), hirag and karma. The
    # manifest was telling this service to introspect and `config.py` was not
    # reading the instruction.
    #
    # DEFAULT EMPTY, and empty FAILS CLOSED — `api/dependencies` refuses every
    # opaque credential rather than admitting one it cannot check. See the
    # decision recorded on `_actor_from_licence`. The direction matters: an
    # unconfigured introspector must never degrade into "allow", because the
    # whole point of the value is that it is the only thing that can say no.
    #
    # ⚠️ Not enforced at boot. `kag_graph_service` puts this field in a
    # `_REQUIRED_IN_PRODUCTION` tuple so a production pod with it unset
    # crash-loops instead of 401ing one request at a time; this service has no
    # such mechanism and adding one is a separate change. Until then the symptom
    # of a missing value is a per-request `ERROR` log naming the variable, which
    # is deliberately loud for exactly that reason.
    license_introspect_url: str = field(default_factory=lambda: _get_env("LICENSE_INTROSPECT_URL", ""))

    #: Licence app keys (introspection `app.key`) this service accepts on the
    #: licence rung. Rulings: tenant x app licences only, no app-to-app
    #: delegation, each hop authenticates with a licence for the TARGET app — so
    #: the end state is `evidence-review` alone.
    #:
    #: DEFAULT EMPTY = no app check (today's behaviour). Measured 2026-10-15
    #: before choosing it: zero inbound requests to the API in the retained logs
    #: (ER app logs, ztunnel 72h, kag `/reviews` proxy ~24h in Loki), and the one
    #: configured inbound path — kag's `/reviews` proxy — forwards the caller's
    #: `Authorization` verbatim and does not restrict its licence app, so the app
    #: of a live caller cannot be determined. Arm it with `evidence-review` (plus
    #: any key a caller is then observed to need) once kag-frontend sends ER
    #: licences.
    er_accepted_licence_apps: tuple[str, ...] = field(default_factory=lambda: _get_csv("ER_ACCEPTED_LICENCE_APPS", ""))

    #: Keycloak `azp` values whose user JWT may ride a licence as
    #: `X-Actor-Token` and name the human who made the decision. The tenant still
    #: comes from the licence only. EMPTY = every actor token is refused (401),
    #: never silently ignored.
    er_actor_token_azp: tuple[str, ...] = field(default_factory=lambda: _get_csv("ER_ACTOR_TOKEN_AZP", "kag-frontend"))

    # Observability
    otel_endpoint: str = field(default_factory=lambda: _get_env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))

    # The worker's heartbeat. See `worker_heartbeat.py` for why each of these
    # exists; the short version is that `evidence-review-worker` has no HTTP
    # server, no Service, no ServiceMonitor and no PodMonitor, so a file written
    # at the end of every completed tick is the ONLY thing outside the process
    # that can distinguish a working loop from a wedged one.
    #
    # ⚠️ These are read by an `exec` probe running `python -m worker_heartbeat`
    # in the worker container, so they must be set on the CONTAINER, not only on
    # the process — an env var exported in an entrypoint script would be invisible
    # to the probe and the two would silently disagree about the path.
    #
    # `/tmp` is the right default and not laziness: the image runs as `appuser`,
    # which owns nothing outside `/app` and `/tmp`; the heartbeat is per-pod state
    # that MUST NOT survive a restart, because one inherited from the previous
    # process would be a lie about this one; and nothing off-pod ever reads it,
    # since the probe `exec`s in the same container. This value is the single
    # definition — `worker_heartbeat` reads it off `Settings` and does not carry
    # a second copy, which is why it cannot drift from what the probe sees.
    worker_heartbeat_path: str = field(
        default_factory=lambda: _get_env(
            "WORKER_HEARTBEAT_PATH",
            "/tmp/evidence-review-worker/heartbeat.json",  # nosec B108
        )
    )
    #: Multiplied by the tick interval the LOOP recorded — never used as a bound
    #: on its own. `worker_heartbeat.staleness_bound` holds the argument for why
    #: a constant bound here would be a bug rather than a simplification.
    worker_heartbeat_stale_multiple: float = field(
        default_factory=lambda: _get_positive_float("WORKER_HEARTBEAT_STALE_MULTIPLE", 6.0)
    )
    #: Seconds between the worker's periodic "alive, and here is what I have
    #: done" log line. 300 rather than every tick: see `worker._Counters.due`.
    worker_heartbeat_log_seconds: float = field(
        default_factory=lambda: _get_positive_float("WORKER_HEARTBEAT_LOG_SECONDS", 300.0)
    )
    #: Where `worker.run()` serves `/metrics`. **The worker needs its own
    #: listener and cannot borrow the API's**: the six relay counters live in
    #: `worker._Counters`, in the worker's memory, and a counter exported from
    #: the API process would read `0` forever — see `metrics`'s module docstring
    #: for why that is worse than exporting nothing. `0` disables the listener,
    #: which is how a deployment that has not yet been given a Service (there is
    #: none in `evidence-review` today) opts out without a code change.
    #:
    #: 9810 rather than 8810: 8810 is the API's, document 09 assigns it, and the
    #: two processes can share a node. Verified free against every
    #: `containerPort` in `clusters/strato/services` — nothing in the estate
    #: binds 98xx.
    worker_metrics_port: int = field(default_factory=lambda: _get_int("WORKER_METRICS_PORT", 9810))
    #: `0.0.0.0` for the reason `host` gives: a pod's only route in is a
    #: ClusterIP Service and a loopback bind would make it unscrapeable.
    worker_metrics_host: str = field(
        default_factory=lambda: _get_env("WORKER_METRICS_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
    )
    log_level: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))

    @property
    def migration_url(self) -> str:
        """URL alembic runs as. Falls back to ``database_url`` for local/CI use."""
        return self.migration_database_url or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
