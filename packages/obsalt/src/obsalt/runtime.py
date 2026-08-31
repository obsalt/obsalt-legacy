"""Application wiring. Memory types are test doubles; serve connects to the compose stack."""

from __future__ import annotations

import logging
import secrets as secretsmod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from obsalt.analysis.cluster import ClickHouseHangupClusterStore, MemoryHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore, MemoryRollupStore
from obsalt.analysis.runners import MemoryEvalRunnerStore
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope, Role, RubricKind
from obsalt.domain.models import Rubric
from obsalt.otel.forward_queue import MemoryForwardQueue
from obsalt.otel.span_identity import SpanIdentityIndex
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.host import LoadedPlugin, discover_plugins
from obsalt.plugin.types import ConnectionConfig
from obsalt.search.hybrid import LocalEmbedder
from obsalt.search.index import MemorySearchIndex
from obsalt.security.secrets import hash_key
from obsalt.store.ports import (
    ApiKeyRecord,
    ConnectionResolver,
    DeletionStore,
    EvalRunnerStore,
    ForwardQueue,
    GenerationStore,
    HangupClusterStore,
    Inbox,
    KeyDirectory,
    LeaseAccelerator,
    ObjectStore,
    OrgSpendStore,
    ReviewStore,
    RevisionPointerStore,
    RevisionSink,
    RollupStore,
    RubricStore,
    SearchIndex,
    TraceAssembler,
    WebhookStore,
)
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.util import new_id, utcnow
from obsalt.worker.process import MemoryRevisionSink

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
    org_spend: dict[str, float] = field(default_factory=dict)
    rollup_generation: str = "gen-0"
    connections_plaintext: dict[str, str] = field(default_factory=dict)
    leases: LeaseAccelerator | None = None
    key_directory: KeyDirectory | None = None
    search: SearchIndex | None = None
    traces: TraceAssembler | None = None
    forward_queue: ForwardQueue | None = None
    rollups: RollupStore | None = None
    worker_id: str = "worker"
    span_identities: SpanIdentityIndex = field(default_factory=SpanIdentityIndex)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    calibration: dict[str, float] = field(default_factory=dict)
    webhook_outbox: list[dict[str, Any]] = field(default_factory=list)
    rubric_store: RubricStore | None = None
    hangup_clusters: HangupClusterStore = field(default_factory=MemoryHangupClusterStore)
    generation_store: GenerationStore | None = None
    webhook_store: WebhookStore | None = None
    review_store: ReviewStore | None = None
    deletion_store: DeletionStore | None = None
    spend_store: OrgSpendStore | None = None
    embedder: Any = None
    tier2_queue: list[tuple[str, str, str]] = field(default_factory=list)
    backups: list[dict[str, Any]] = field(default_factory=list)
    key_roles: dict[str, Role] = field(default_factory=dict)
    key_expiry: dict[str, Any] = field(default_factory=dict)
    deletion_completions: list[dict[str, Any]] = field(default_factory=list)
    eval_runner_store: EvalRunnerStore = field(default_factory=MemoryEvalRunnerStore)

    @property
    def spend_usd(self) -> float:
        """Sum of in-process org spend. Prefer org_spend_usd(state, org_id)."""
        return float(sum(self.org_spend.values()))

    @spend_usd.setter
    def spend_usd(self, value: float) -> None:
        # Legacy single-tenant tests assigned a global. Map onto the first org.
        if not self.org_spend:
            self.org_spend["local"] = float(value)
            return
        first = next(iter(self.org_spend))
        self.org_spend[first] = float(value)


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
    generation_store = MemoryGenerationStore()
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
    keys = {api_key: (org_id, frozenset(KeyScope))}
    rubrics: dict[str, Rubric] = {}
    reviews: list[dict[str, Any]] = []
    webhook_destinations: list[dict[str, Any]] = []
    webhook_outbox: list[dict[str, Any]] = []
    return AppState(
        settings=settings,
        plugins=plugins,
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys=keys,
        key_roles={api_key: Role.OWNER},
        org_spend={org_id: 0.0},
        search=MemorySearchIndex(),
        traces=MemoryTraceAssembler(),
        forward_queue=MemoryForwardQueue(),
        rollups=MemoryRollupStore(),
        span_identities=SpanIdentityIndex(),
        hangup_clusters=MemoryHangupClusterStore(),
        generation_store=generation_store,
        key_directory=MemoryKeyDirectory(keys),
        rubric_store=MemoryRubricStore(rubrics),
        rubrics=rubrics,
        review_store=MemoryReviewStore(reviews),
        reviews=reviews,
        webhook_store=MemoryWebhookStore(webhook_destinations, webhook_outbox),
        webhook_destinations=webhook_destinations,
        webhook_outbox=webhook_outbox,
        deletion_store=MemoryDeletionStore(),
        spend_store=MemoryOrgSpend(),
        embedder=LocalEmbedder(),
        rollup_generation=generation_store.get("fleet") or "gen-0",
        eval_runner_store=MemoryEvalRunnerStore(),
    )


def production_state(settings: Settings, plugins: list[LoadedPlugin] | None = None) -> AppState:
    """Connect to Postgres, ClickHouse, object storage, and Redis. No memory fallback."""
    plugins = plugins if plugins is not None else discover_plugins()
    from obsalt.search.hybrid import OnnxEmbedder
    from obsalt.store.clickhouse import ClickHouseSink
    from obsalt.store.clickhouse import apply_schema as apply_clickhouse
    from obsalt.store.forward import PostgresForwardQueue
    from obsalt.store.leases import RedisLeaseAccelerator
    from obsalt.store.objects import S3ObjectStore
    from obsalt.store.postgres import (
        PostgresDeletionStore,
        PostgresEvalRunnerStore,
        PostgresGenerationStore,
        PostgresInbox,
        PostgresKeyDirectory,
        PostgresOrgSpend,
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
    from obsalt.store.trace import PostgresTraceAssembler

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
    rubric_store = PostgresRubricStore(conn)
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
        rubric_store=rubric_store,
        rubrics=_load_rubrics(rubric_store, org_id),
        hangup_clusters=ClickHouseHangupClusterStore(sink._client),
        generation_store=generation_store,
        webhook_store=PostgresWebhookStore(conn, master_key=settings.master_key.encode()),
        eval_runner_store=PostgresEvalRunnerStore(conn, master_key=settings.master_key.encode()),
        review_store=PostgresReviewStore(conn),
        deletion_store=PostgresDeletionStore(conn, inbox),
        spend_store=PostgresOrgSpend(conn),
        embedder=OnnxEmbedder(model_path=settings.embedder_onnx_path),
        key_roles={settings.bootstrap_api_key: Role.OWNER} if settings.bootstrap_api_key else {},
    )


def _production_rollups(sink: Any) -> RollupStore:
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
    settings: dict[str, Any] | None = None,
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
    state.resolver.add(cfg, ingest_key)
    state.connections_plaintext[connection_id] = ingest_key
    return {"connection_id": connection_id, "ingest_key": ingest_key, "provider": provider}


def bump_generation(state: AppState) -> str:
    nxt = new_id()
    if state.generation_store is not None:
        state.generation_store.publish("fleet", nxt, expected=state.rollup_generation)
    state.rollup_generation = nxt
    return state.rollup_generation


class MemoryGenerationStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {"fleet": "gen-0"}

    def get(self, name: str = "fleet") -> str | None:
        return self._values.get(name)

    def publish(self, name: str, generation: str, expected: str | None = None) -> bool:
        current = self._values.get(name)
        if expected is not None and current != expected:
            return False
        self._values[name] = generation
        return True


class MemoryOrgSpend:
    """Per-org in-process spend ledger. Production uses PostgresOrgSpend."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], float] = {}

    def get(self, org_id: str, period: str | None = None) -> float:
        return float(self._values.get((org_id, period or _spend_period()), 0.0))

    def add(self, org_id: str, amount: float, period: str | None = None) -> float:
        key = (org_id, period or _spend_period())
        self._values[key] = self.get(org_id, key[1]) + float(amount)
        return self._values[key]


def _spend_period() -> str:
    return utcnow().strftime("%Y-%m")


def org_spend_usd(state: AppState, org_id: str) -> float:
    if state.spend_store is not None:
        return float(state.spend_store.get(org_id))
    return float(state.org_spend.get(org_id, 0.0))


def add_org_spend(state: AppState, org_id: str, amount: float) -> float:
    if amount <= 0:
        return org_spend_usd(state, org_id)
    if state.spend_store is not None:
        total = float(state.spend_store.add(org_id, amount))
    else:
        total = float(state.org_spend.get(org_id, 0.0)) + float(amount)
    state.org_spend[org_id] = total
    from obsalt.metrics import tier2_spend_usd

    tier2_spend_usd.labels(org_id=org_id).set(total)
    return total


class MemoryKeyDirectory:
    """Test double wrapping AppState.keys. Production uses PostgresKeyDirectory."""

    def __init__(self, plain: dict[str, tuple[str, frozenset[KeyScope]]] | None = None) -> None:
        self.plain = plain if plain is not None else {}
        self._ids: dict[str, str] = {}

    def lookup(self, plaintext: str) -> ApiKeyRecord | None:
        found = self.plain.get(plaintext)
        if found is None:
            return None
        org_id, scopes = found
        return ApiKeyRecord(
            key_id=self._ids.get(plaintext, plaintext), org_id=org_id, scopes=scopes
        )

    def insert(
        self,
        org_id: str,
        plaintext: str,
        scopes: Iterable[KeyScope],
        *,
        expires_at: datetime | None = None,
    ) -> str:
        key_id = new_id()
        self.plain[plaintext] = (org_id, frozenset(scopes))
        self._ids[plaintext] = key_id
        return key_id

    def revoke(self, key_id: str) -> None:
        for plaintext, stored_id in list(self._ids.items()):
            if stored_id == key_id:
                self.plain.pop(plaintext, None)
                self._ids.pop(plaintext, None)

    def rotate(
        self,
        org_id: str,
        old_plaintext: str,
        new_plaintext: str,
        *,
        overlap_seconds: int = 86_400,
    ) -> str:
        current = self.lookup(old_plaintext)
        if current is None or current.org_id != org_id:
            raise ValueError("unknown or expired key")
        return self.insert(org_id, new_plaintext, current.scopes)


class MemoryRubricStore:
    def __init__(self, items: dict[str, Rubric] | None = None) -> None:
        self.items = items if items is not None else {}

    def list(self, org_id: str) -> list[Rubric]:
        return [item for item in self.items.values() if item.org_id == org_id]

    def get(self, org_id: str, rubric_id: str) -> Rubric | None:
        item = self.items.get(rubric_id)
        if item is None or item.org_id != org_id:
            return None
        return item

    def insert(self, rubric: Rubric) -> Rubric:
        self.items[rubric.id] = rubric
        return rubric

    def new_version(
        self,
        previous: Rubric,
        *,
        name: str | None = None,
        description: str | None = None,
        threshold: float | None = None,
        enabled: bool | None = None,
        kind: RubricKind | None = None,
        spec: dict[str, Any] | None = None,
    ) -> Rubric:
        updated = previous.model_copy(
            update={
                "version": previous.version + 1,
                "name": name if name is not None else previous.name,
                "description": description if description is not None else previous.description,
                "threshold": threshold if threshold is not None else previous.threshold,
                "enabled": enabled if enabled is not None else previous.enabled,
                "kind": kind if kind is not None else previous.kind,
                "spec": spec if spec is not None else previous.spec,
            }
        )
        self.items[updated.id] = updated
        return updated

    def delete(self, org_id: str, rubric_id: str) -> bool:
        item = self.items.get(rubric_id)
        if item is None or item.org_id != org_id:
            return False
        del self.items[rubric_id]
        return True


class MemoryReviewStore:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items if items is not None else []

    def insert(self, item: dict[str, Any]) -> dict[str, Any]:
        rid = item.get("id") or new_id()
        stored = {**item, "id": rid}
        self.items.append(stored)
        return stored

    def list(self, org_id: str) -> list[dict[str, Any]]:
        return [item for item in self.items if item.get("org_id") == org_id]


class MemoryWebhookStore:
    def __init__(
        self,
        destinations: list[dict[str, Any]] | None = None,
        outbox: list[dict[str, Any]] | None = None,
    ) -> None:
        self.destinations = destinations if destinations is not None else []
        self.outbox = outbox if outbox is not None else []

    def list_destinations(self, org_id: str) -> list[dict[str, Any]]:
        return [
            dest
            for dest in self.destinations
            if not dest.get("org_id") or dest.get("org_id") == org_id
        ]

    def create(self, dest: dict[str, Any]) -> dict[str, Any]:
        self.destinations.append(dest)
        return dest

    def enqueue(self, item: dict[str, Any]) -> None:
        if any(existing.get("event_id") == item.get("event_id") for existing in self.outbox):
            return
        self.outbox.append(item)

    def claim(self, limit: int = 32) -> list[dict[str, Any]]:
        return [item for item in self.outbox if int(item.get("attempts") or 0) < 8][:limit]

    def mark_delivered(self, event_id: str) -> None:
        self.outbox[:] = [item for item in self.outbox if item.get("event_id") != event_id]

    def mark_failed(self, event_id: str, detail: str, attempts: int) -> None:
        keep: list[dict[str, Any]] = []
        for item in self.outbox:
            if item.get("event_id") == event_id:
                if attempts >= 8:
                    continue
                item["attempts"] = attempts
                item["last_error"] = detail
            keep.append(item)
        self.outbox[:] = keep

    def purge_for_call(self, org_id: str, call_id: str) -> int:
        keep: list[dict[str, Any]] = []
        removed = 0
        for item in self.outbox:
            payload = item.get("payload") or {}
            dest = item.get("dest") or {}
            item_org = payload.get("org_id") or dest.get("org_id") or item.get("org_id")
            item_call = payload.get("call_id") or item.get("call_id")
            if item_org == org_id and item_call == call_id:
                removed += 1
                continue
            keep.append(item)
        self.outbox[:] = keep
        return removed


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

    def backlog(self) -> int:
        return sum(1 for item in self.completed if item.get("status") != "completed")


def _load_rubrics(store: RubricStore, org_id: str) -> dict[str, Any]:
    return {item.id: item for item in store.list(org_id)}
