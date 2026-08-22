"""Application wiring. Memory types are test doubles; serve connects to the compose stack."""

from __future__ import annotations

import logging
import secrets as secretsmod
from dataclasses import dataclass, field
from typing import Any

from obsalt.analysis.cluster import ClickHouseHangupClusterStore, MemoryHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore, MemoryRollupStore
from obsalt.analysis.judge import judge_from_settings
from obsalt.assemble.promote import MemoryPointerStore, RevisionPointerStore
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope
from obsalt.domain.models import Rubric
from obsalt.ingest.receive import ConnectionResolver, Inbox, ObjectStore
from obsalt.otel.forward_queue import MemoryForwardQueue
from obsalt.otel.span_identity import SpanIdentityIndex
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.host import LoadedPlugin, discover_plugins
from obsalt.plugin.types import ConnectionConfig
from obsalt.search.index import MemorySearchIndex
from obsalt.security.secrets import hash_key
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.util import new_id
from obsalt.worker.process import MemoryRevisionSink, RevisionSink

log = logging.getLogger("obsalt.runtime")


@dataclass
class AppState:
    settings: Settings
    plugins: list[LoadedPlugin]
    resolver: ConnectionResolver
    objects: ObjectStore
    inbox: Inbox
    pointers: RevisionPointerStore
    sink: RevisionSink
    keys: dict[str, tuple[str, frozenset[KeyScope]]] = field(default_factory=dict)
    rubrics: dict[str, Rubric] = field(default_factory=dict)
    destinations: list[dict[str, str]] = field(default_factory=list)
    webhook_destinations: list[dict[str, Any]] = field(default_factory=list)
    spend_usd: float = 0.0
    rollup_generation: str = "gen-0"
    connections_plaintext: dict[str, str] = field(default_factory=dict)
    leases: Any = None
    key_directory: Any = None
    search: Any = None
    traces: Any = None
    forward_queue: Any = None
    rollups: Any = None
    worker_id: str = "worker"
    span_identities: Any = field(default_factory=SpanIdentityIndex)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    webhook_outbox: list[dict[str, Any]] = field(default_factory=list)
    judge: Any = None
    rubric_store: Any = None
    hangup_clusters: Any = field(default_factory=MemoryHangupClusterStore)
    generation_store: Any = None
    webhook_store: Any = None
    review_store: Any = None
    deletion_store: Any = None
    key_expiry: dict[str, Any] = field(default_factory=dict)
    deletion_completions: list[dict[str, Any]] = field(default_factory=list)


def in_memory_state(
    settings: Settings | None = None,
    plugins: list[LoadedPlugin] | None = None,
    *,
    org_id: str = "dev",
    api_key: str = "dev-key",
    ingest_key: str = "dev",
) -> AppState:
    settings = settings or Settings(trace_grace_seconds=0)
    plugins = plugins if plugins is not None else discover_plugins()
    resolver = MemoryResolver()
    for plugin in plugins:
        caps = {c.value for c in plugin.capabilities}
        if "webhook_source" not in caps:
            continue
        secrets_map = {name: "dev-secret" for name in plugin.manifest.secret_fields} or {
            "hmac_secret": "dev-secret"
        }
        if plugin.name == "vapi":
            secrets_map["legacy_secret"] = "dev-secret"
        if plugin.name == "retell":
            secrets_map["api_key"] = "dev-secret"
        cfg = ConnectionConfig(
            org_id=org_id,
            provider=plugin.name,
            connection_id=f"{plugin.name}-dev",
            ingest_key_hash="",
            secrets=secrets_map,
            settings={"auth_mode": "legacy_secret"} if plugin.name == "vapi" else {},
        )
        resolver.add(cfg, ingest_key)
    return AppState(
        settings=settings,
        plugins=plugins,
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={api_key: (org_id, frozenset(KeyScope))},
        rollup_generation=new_id(),
        search=MemorySearchIndex(),
        traces=MemoryTraceAssembler(),
        forward_queue=MemoryForwardQueue(),
        rollups=MemoryRollupStore(),
        span_identities=SpanIdentityIndex(),
        judge=judge_from_settings(settings),
        hangup_clusters=MemoryHangupClusterStore(),
        generation_store=MemoryGenerationStore(),
        deletion_store=MemoryDeletionStore(),
    )


def production_state(settings: Settings, plugins: list[LoadedPlugin] | None = None) -> AppState:
    """Connect to Postgres, ClickHouse, object storage, and Redis. No memory fallback."""
    plugins = plugins if plugins is not None else discover_plugins()
    from obsalt.store.clickhouse import ClickHouseSink
    from obsalt.store.clickhouse import apply_schema as apply_clickhouse
    from obsalt.store.forward_pg import PostgresForwardQueue
    from obsalt.store.leases import RedisLeaseAccelerator
    from obsalt.store.objects import S3ObjectStore
    from obsalt.store.postgres import (
        PostgresDeletionStore,
        PostgresGenerationStore,
        PostgresInbox,
        PostgresKeyDirectory,
        PostgresPointerStore,
        PostgresResolver,
        PostgresReviewStore,
        PostgresRubricStore,
        PostgresSearchDocuments,
        PostgresWebhookStore,
        apply_schema,
        connect,
        ping,
    )
    from obsalt.store.trace_pg import PostgresTraceAssembler

    conn = connect(settings.postgres_dsn)
    ping(conn)
    apply_schema(conn)
    inbox = PostgresInbox(conn)
    resolver = PostgresResolver(conn, master_key=settings.master_key.encode())
    pointers = PostgresPointerStore(conn)
    objects = S3ObjectStore(settings)
    sink = ClickHouseSink(settings)
    sink.ping()
    try:
        apply_clickhouse(sink._client)
    except Exception:
        log.warning("clickhouse schema apply skipped; compose init may already have created tables")
    objects.ensure_bucket()

    leases = None
    try:
        leases = RedisLeaseAccelerator.from_url(settings.redis_url)
        leases.ping()
    except Exception:
        log.warning("redis unavailable; postgres outbox remains the source of truth")
        leases = None

    key_directory = PostgresKeyDirectory(conn)
    org_id = settings.bootstrap_org_id
    with conn.transaction():
        conn.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (org_id, org_id),
        )
    keys: dict[str, tuple[str, frozenset[KeyScope]]] = {}
    if settings.bootstrap_api_key:
        if key_directory.lookup(settings.bootstrap_api_key) is None:
            key_directory.insert(org_id, settings.bootstrap_api_key, list(KeyScope))
        keys[settings.bootstrap_api_key] = (org_id, frozenset(KeyScope))

    generation_store = PostgresGenerationStore(conn)
    return AppState(
        settings=settings,
        plugins=plugins,
        resolver=resolver,
        objects=objects,
        inbox=inbox,
        pointers=pointers,
        sink=sink,
        keys=keys,
        rollup_generation=generation_store.get("fleet") or "boot",
        leases=leases,
        key_directory=key_directory,
        search=PostgresSearchDocuments(conn, onnx_path=settings.embedder_onnx_path),
        traces=PostgresTraceAssembler(conn),
        forward_queue=PostgresForwardQueue(conn, objects),
        rollups=_production_rollups(sink),
        span_identities=_production_span_index(conn),
        judge=judge_from_settings(settings),
        rubric_store=PostgresRubricStore(conn),
        hangup_clusters=ClickHouseHangupClusterStore(sink._client),
        generation_store=generation_store,
        webhook_store=PostgresWebhookStore(conn, master_key=settings.master_key.encode()),
        review_store=PostgresReviewStore(conn),
        deletion_store=PostgresDeletionStore(conn, inbox),
    )


def _production_rollups(sink: Any) -> MemoryRollupStore:
    client = getattr(sink, "_client", None)
    if client is None:
        return MemoryRollupStore()
    return ClickHouseRollupStore(client)


def _production_span_index(conn: Any) -> SpanIdentityIndex:
    from obsalt.otel.span_identity import PostgresSpanIdentityIndex

    return PostgresSpanIdentityIndex(conn)


def create_connection(
    state: AppState,
    *,
    org_id: str,
    provider: str,
    secrets: dict[str, str],
    settings: dict | None = None,
) -> dict[str, str]:
    ingest_key = secretsmod.token_urlsafe(24)
    connection_id = new_id()
    cfg = ConnectionConfig(
        org_id=org_id,
        provider=provider,
        connection_id=connection_id,
        ingest_key_hash=hash_key(ingest_key),
        secrets=secrets,
        settings=settings or {},
    )
    adder = getattr(state.resolver, "add", None)
    if adder is None:
        raise RuntimeError("resolver cannot create connections")
    adder(cfg, ingest_key)
    state.connections_plaintext[connection_id] = ingest_key
    return {"connection_id": connection_id, "ingest_key": ingest_key, "provider": provider}


def bump_generation(state: AppState) -> str:
    nxt = new_id()
    store = getattr(state, "generation_store", None)
    if store is not None and hasattr(store, "publish"):
        store.publish("fleet", nxt, expected=state.rollup_generation)
    state.rollup_generation = nxt
    return state.rollup_generation


class MemoryGenerationStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {"fleet": "gen-0"}

    def get(self, name: str = "fleet") -> str | None:
        return self._values.get(name)

    def publish(self, name: str, generation: str, expected: str | None = None) -> bool:
        current = self._values.get(name)
        if expected is not None and current != expected and current is not None:
            # First publish after boot may not match the in-process token.
            if current != "gen-0" and current != "boot":
                return False
        self._values[name] = generation
        return True


class MemoryDeletionStore:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []

    def complete(
        self,
        org_id: str,
        *,
        call_ids: Any = None,
        source_call_id: str | None = None,
        caller_token: str | None = None,
    ) -> int:
        from obsalt.util import utcnow

        item = {
            "org_id": org_id,
            "call_ids": list(call_ids or []),
            "source_call_id": source_call_id,
            "caller_token": caller_token,
            "completed_at": utcnow().isoformat(),
            "status": "completed",
        }
        self.completed.append(item)
        return 1
