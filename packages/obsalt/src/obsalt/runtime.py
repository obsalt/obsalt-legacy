"""Process runtime. The serving index is the Postgres active-revision pointer.

A memory index is used in tests and ``obsalt demo`` before containers are up.
It is not a second production storage engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from obsalt.analysis.tier1 import analyze_tier1
from obsalt.config import Settings
from obsalt.crypto.keys import hash_ingest_key, hash_secret, new_api_key, new_ingest_key
from obsalt.domain.models import AnalysisResult, CallRevision
from obsalt.ingest.receive import ReceiveService
from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import ConnectionConfig
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
    resolver: MemoryResolver
    objects: MemoryObjects
    inbox: MemoryInbox
    receive: ReceiveService
    calls: dict[tuple[str, str], CallRevision] = field(default_factory=dict)
    analysis: dict[tuple[str, str, int], list[AnalysisResult]] = field(default_factory=dict)
    api_keys: dict[str, ApiPrincipal] = field(default_factory=dict)
    ingest_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    connections: dict[str, ConnectionConfig] = field(default_factory=dict)
    rubrics: dict[str, dict] = field(default_factory=dict)
    tombstones: list[dict] = field(default_factory=list)
    rollup_generation: int = 1

    @classmethod
    def create(cls, settings: Settings | None = None, extra_plugins: list | None = None) -> Runtime:
        settings = settings or Settings()
        host = PluginHost.load(extra=extra_plugins or [])
        objects = MemoryObjects()
        inbox = MemoryInbox()
        resolver = MemoryResolver({})
        receive = ReceiveService(host=host, resolver=resolver, objects=objects, inbox=inbox)
        runtime = cls(
            settings=settings,
            host=host,
            resolver=resolver,
            objects=objects,
            inbox=inbox,
            receive=receive,
        )
        return runtime

    def bootstrap_dev_org(self, org_id: str = "demo") -> dict[str, str]:
        plaintext, _prefix, hashed = new_api_key()
        self.api_keys[hashed] = ApiPrincipal(org_id=org_id, scope="admin", kind="service", role="owner")
        # also accept the raw token via hash lookup
        ingest = new_ingest_key()
        return {"org_id": org_id, "api_key": plaintext, "api_key_hash": hashed, "ingest_key": ingest}

    def put_connection(self, cfg: ConnectionConfig, ingest_key: str) -> None:
        self.connections[cfg.connection_id] = cfg
        self.resolver.connections[(cfg.provider, ingest_key)] = cfg
        self.ingest_keys[hash_ingest_key(ingest_key)] = (cfg.provider, cfg.connection_id)

    def store_revision(self, revision: CallRevision) -> None:
        self.calls[(revision.org_id, revision.call_id)] = revision
        _exec, results = analyze_tier1(revision)
        self.analysis[(revision.org_id, revision.call_id, revision.revision)] = results

    def get_revision(self, org_id: str, call_id: str) -> CallRevision | None:
        return self.calls.get((org_id, call_id))

    def list_calls(self, org_id: str) -> list[CallRevision]:
        return [rev for (org, _cid), rev in self.calls.items() if org == org_id]

    def authenticate_api_key(self, token: str) -> ApiPrincipal | None:
        return self.api_keys.get(hash_secret(token))
