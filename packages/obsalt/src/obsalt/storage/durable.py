"""The production store: Postgres + ClickHouse + object storage.

This is not a pluggable-backend layer. There is no Postgres-only mode.
Memory classes remain test doubles only.
"""

from __future__ import annotations

from typing import Any

from obsalt.config import Settings
from obsalt.domain.models import CallRevision
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope
from obsalt.storage.clickhouse import ClickHouseFacts
from obsalt.storage.objects import S3Objects
from obsalt.storage.postgres import PostgresStore


class DurableBackend:
    def __init__(self, pg: PostgresStore, objects: S3Objects, ch: ClickHouseFacts) -> None:
        self.pg = pg
        self.objects = objects
        self.ch = ch
        self.connections: dict[tuple[str, str], ConnectionConfig] = {}

    @property
    def envelopes(self) -> dict[str, RawEnvelope]:
        return self.pg.envelopes

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        hit = self.connections.get((provider, ingest_key))
        if hit is not None:
            return hit
        return self.pg.resolve(provider, ingest_key)

    def put(self, key: str, body: bytes, *, headers: dict[str, str]) -> None:
        self.objects.put(key, body, headers=headers)

    def delete(self, key: str) -> None:
        self.objects.delete(key)

    def get(self, key: str) -> Any:
        envelope = self.pg.get(key)
        if envelope is not None:
            return envelope
        return self.objects.get(key)

    def accept(
        self,
        envelope: RawEnvelope,
        *,
        delivery_key: str,
        tombstone_hints: dict[str, Any],
    ) -> tuple[str, bool]:
        return self.pg.accept(envelope, delivery_key=delivery_key, tombstone_hints=tombstone_hints)

    def is_tombstoned(self, hints: dict[str, Any]) -> bool:
        return self.pg.is_tombstoned(hints)

    def add_tombstone(self, **hints: Any) -> None:
        self.pg.add_tombstone(**hints)

    def claim(self, limit: int = 32) -> list[str]:
        return self.pg.claim(limit)

    def mark_assembled(self, envelope_id: str, revision: int) -> None:
        self.pg.mark_assembled(envelope_id, revision)

    def persist_connection(self, cfg: ConnectionConfig, key_hash: str) -> None:
        self.pg.persist_connection(cfg, key_hash)

    def put_evidence(self, org_id: str, ref: str, body: bytes) -> None:
        self.objects.put(f"evidence/{org_id}/{ref}", body, headers={})

    def get_evidence(self, org_id: str, ref: str) -> bytes | None:
        try:
            return self.objects.get(f"evidence/{org_id}/{ref}")
        except Exception:
            return None

    def upsert_search(
        self, org_id: str, call_id: str, revision: int, transcript: str, embedding: list[float]
    ) -> None:
        self.pg.upsert_search_document(org_id, call_id, revision, transcript, embedding)

    def write_and_cas(self, revision: CallRevision, *, expected: int | None) -> None:
        self.ch.write_revision(revision)
        self.ch.verify(revision)
        if not self.pg.cas_active(revision, expected=expected):
            raise RuntimeError("active-revision CAS failed after ClickHouse write")

    def list_active_revisions(self) -> list[CallRevision]:
        out: list[CallRevision] = []
        for org_id, call_id, revision in self.pg.list_active_pointers():
            loaded = self.ch.get_revision(org_id, call_id, revision)
            if loaded is not None:
                out.append(loaded)
        return out


def open_durable(settings: Settings) -> DurableBackend:
    pg = PostgresStore(settings.postgres_dsn, master_key=settings.master_key)
    objects = S3Objects(settings)
    ch = ClickHouseFacts(settings.clickhouse_url, settings.clickhouse_db)
    return DurableBackend(pg, objects, ch)
