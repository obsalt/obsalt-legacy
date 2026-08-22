"""Process runtime. The serving index is the active-revision pointer.

A memory index is used in tests and ``obsalt demo`` before containers are up.
It is not a second production storage engine. ``obsalt serve`` opens the
committed Postgres + ClickHouse + object-storage spine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from obsalt.analysis.tier1 import analyze_tier1
from obsalt.assemble.promotion import PromotionResult, cas_pointer
from obsalt.config import Settings
from obsalt.crypto.keys import hash_ingest_key, hash_secret, new_api_key, new_ingest_key
from obsalt.domain.identity import content_hash
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.ingest.receive import ReceiveService
from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import ConnectionConfig, RedactedDocument
from obsalt.search.embedder import NgramEmbedder
from obsalt.storage.memory import MemoryInbox, MemoryObjects, MemoryResolver


@dataclass
class ApiPrincipal:
    org_id: str
    scope: str
    kind: str  # service | session
    role: str = "analyst"


@dataclass
class Runtime:
    settings: Settings
    host: PluginHost
    resolver: Any
    objects: Any
    inbox: Any
    receive: ReceiveService
    calls: dict[tuple[str, str], CallRevision] = field(default_factory=dict)
    revisions: dict[tuple[str, str, int], CallRevision] = field(default_factory=dict)
    active: dict[tuple[str, str], int] = field(default_factory=dict)
    analysis: dict[tuple[str, str, int], list[AnalysisResult]] = field(default_factory=dict)
    executions: dict[tuple[str, str, int, str], AnalysisExecution] = field(default_factory=dict)
    evidence: dict[tuple[str, str], str] = field(default_factory=dict)
    search_vectors: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    api_keys: dict[str, ApiPrincipal] = field(default_factory=dict)
    ingest_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    connections: dict[str, ConnectionConfig] = field(default_factory=dict)
    rubrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    tombstones: list[dict[str, Any]] = field(default_factory=list)
    spend_usd: dict[str, float] = field(default_factory=dict)
    rollup_generation: int = 1
    durable: Any = None

    @classmethod
    def create(cls, settings: Settings | None = None, extra_plugins: list[Any] | None = None) -> Runtime:
        settings = settings or Settings()
        host = PluginHost.load(extra=extra_plugins or [])
        objects = MemoryObjects()
        inbox = MemoryInbox()
        resolver = MemoryResolver({})
        receive = ReceiveService(host=host, resolver=resolver, objects=objects, inbox=inbox)
        return cls(
            settings=settings,
            host=host,
            resolver=resolver,
            objects=objects,
            inbox=inbox,
            receive=receive,
        )

    @classmethod
    def create_durable(cls, settings: Settings, extra_plugins: list[Any] | None = None) -> Runtime:
        from obsalt.storage.durable import open_durable

        backend = open_durable(settings)
        host = PluginHost.load(extra=extra_plugins or [])
        receive = ReceiveService(host=host, resolver=backend, objects=backend, inbox=backend)
        runtime = cls(
            settings=settings,
            host=host,
            resolver=backend,  # type: ignore[arg-type]
            objects=backend,  # type: ignore[arg-type]
            inbox=backend,  # type: ignore[arg-type]
            receive=receive,
            durable=backend,
        )
        runtime.hydrate()
        return runtime

    def hydrate(self) -> None:
        if self.durable is None:
            return
        for revision in self.durable.list_active_revisions():
            key = (revision.org_id, revision.call_id)
            self.revisions[(revision.org_id, revision.call_id, revision.revision)] = revision
            self.active[key] = revision.revision
            self.calls[key] = revision

    def bootstrap_dev_org(self, org_id: str = "demo") -> dict[str, str]:
        plaintext, prefix, hashed = new_api_key()
        self.api_keys[hashed] = ApiPrincipal(org_id=org_id, scope="admin", kind="service", role="owner")
        ingest = new_ingest_key()
        return {
            "org_id": org_id,
            "api_key": plaintext,
            "api_key_hash": hashed,
            "api_key_prefix": prefix,
            "ingest_key": ingest,
        }

    def put_connection(self, cfg: ConnectionConfig, ingest_key: str) -> None:
        self.connections[cfg.connection_id] = cfg
        self.resolver.connections[(cfg.provider, ingest_key)] = cfg
        digest = hash_ingest_key(ingest_key)
        self.resolver.connections[(cfg.provider, digest)] = cfg
        self.ingest_keys[digest] = (cfg.provider, cfg.connection_id)
        if self.durable is not None:
            self.durable.persist_connection(cfg, digest)

    def put_blobs(self, org_id: str, blobs: dict[str, str]) -> None:
        for ref, body in blobs.items():
            self.evidence[(org_id, ref)] = body
            if self.durable is not None:
                self.durable.put_evidence(org_id, ref, body.encode("utf-8"))

    def get_blob(self, org_id: str, ref: str) -> str | None:
        hit = self.evidence.get((org_id, ref))
        if hit is not None:
            return hit
        if self.durable is not None:
            raw = self.durable.get_evidence(org_id, ref)
            if raw is not None:
                text = bytes(raw).decode("utf-8")
                self.evidence[(org_id, ref)] = text
                return text
        return None

    def store_revision(
        self, revision: CallRevision, *, expected: int | None = None
    ) -> tuple[CallRevision, PromotionResult]:
        key = (revision.org_id, revision.call_id)
        current = self.active.get(key)
        result = cas_pointer(current, expected, revision.revision)
        if not result.promoted:
            rebased = (current or 0) + 1
            revision = revision.model_copy(update={"revision": rebased})
            result = cas_pointer(current, current, rebased)
            if result.promoted:
                result = PromotionResult(
                    promoted=True,
                    active_revision=rebased,
                    rebased=True,
                )
        if result.promoted and self.durable is not None:
            self.durable.write_and_cas(revision, expected=expected)
        self.revisions[(revision.org_id, revision.call_id, revision.revision)] = revision
        if result.promoted:
            self.active[key] = revision.revision
            self.calls[key] = revision
            self._after_promote(revision)
        return revision, result

    def _after_promote(self, revision: CallRevision) -> None:
        execution, results = analyze_tier1(revision)
        self.analysis[(revision.org_id, revision.call_id, revision.revision)] = results
        self.executions[(revision.org_id, revision.call_id, revision.revision, "tier1")] = execution
        for turn in revision.turns:
            if turn.text:
                ref = turn.text_ref or content_hash(turn.text)
                self.evidence[(revision.org_id, ref)] = turn.text
        text = " ".join(turn.text or "" for turn in revision.turns)
        if text:
            vec = (
                NgramEmbedder()
                .embed_sync([RedactedDocument(document_id=revision.call_id, text=text)])[0]
                .values
            )
            self.search_vectors[(revision.org_id, revision.call_id)] = vec
        if self.durable is None:
            self.rollup_generation += 1

    def get_revision(self, org_id: str, call_id: str, revision: int | None = None) -> CallRevision | None:
        if revision is None:
            pointer = self.active.get((org_id, call_id))
            if pointer is None:
                return self.calls.get((org_id, call_id))
            return self.revisions.get((org_id, call_id, pointer))
        return self.revisions.get((org_id, call_id, revision))

    def list_calls(self, org_id: str) -> list[CallRevision]:
        return [rev for (org, _cid), rev in self.calls.items() if org == org_id]

    def authenticate_api_key(self, token: str) -> ApiPrincipal | None:
        return self.api_keys.get(hash_secret(token))

    def budget_remaining(self, org_id: str) -> bool:
        cap = self.settings.llm_monthly_cap_usd
        if cap <= 0:
            return True
        return self.spend_usd.get(org_id, 0.0) < cap

    def process_envelope(self, envelope_id: str, raw: bytes | None = None) -> CallRevision | None:
        envelope = self.inbox.get(envelope_id)
        if envelope is None:
            return None
        body = raw if raw is not None else self.objects.get(envelope.object_key)
        loaded = envelope.model_copy(update={"body": body})
        try:
            plugin = self.host.webhook(envelope.provider)
        except (KeyError, TypeError):
            return None
        blobs: dict[str, str] = {}
        try:
            revision = decode_loaded(self, loaded, plugin, blobs)
        except Exception:
            return None
        existing = self.get_revision(revision.org_id, revision.call_id)
        if existing:
            revision = revision.model_copy(update={"revision": existing.revision + 1})
            revision, _ = self.store_revision(revision, expected=existing.revision)
        else:
            revision, _ = self.store_revision(revision, expected=None)
        self.put_blobs(revision.org_id, blobs)
        self.inbox.mark_assembled(envelope_id, revision.revision)
        return revision

    def drain_outbox(self, limit: int = 32) -> int:
        claimed = self.inbox.claim(limit)
        done = 0
        for envelope_id in claimed:
            if self.process_envelope(envelope_id) is not None:
                done += 1
        return done

    def replay_org(self, org_id: str, *, provider: str | None = None) -> int:
        count = 0
        for envelope_id, envelope in list(self.inbox.envelopes.items()):
            if envelope.org_id != org_id:
                continue
            if provider and envelope.provider != provider:
                continue
            if self.process_envelope(envelope_id) is not None:
                count += 1
        return count


def decode_loaded(runtime: Runtime, envelope: Any, plugin: object, blobs: dict[str, str]) -> CallRevision:
    from obsalt.workers.decode import decode_envelope

    declaration = None
    try:
        declaration = runtime.host.get(envelope.provider).fidelity
    except KeyError:
        declaration = getattr(plugin, "fidelity", None)
    return decode_envelope(
        envelope,
        plugin=plugin,
        declaration=declaration,
        blobs=blobs,
    )
