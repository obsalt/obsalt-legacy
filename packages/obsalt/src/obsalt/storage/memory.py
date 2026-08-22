"""Test doubles for receive/inbox. Not a production backend and not a second store.

Production uses Postgres + ClickHouse + object storage. These classes exist so
crash-point and auth tests can run without Docker.
"""

from __future__ import annotations

from typing import Any

from obsalt.crypto.keys import hash_ingest_key
from obsalt.ingest.receive import Tombstoned
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope


class MemoryObjects:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.meta: dict[str, dict[str, str]] = {}

    def put(self, key: str, body: bytes, *, headers: dict[str, str]) -> None:
        self.blobs[key] = body
        self.meta[key] = dict(headers)

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def delete(self, key: str) -> None:
        self.blobs.pop(key, None)
        self.meta.pop(key, None)


class MemoryResolver:
    def __init__(self, connections: dict[tuple[str, str], ConnectionConfig]) -> None:
        self.connections = connections

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        hit = self.connections.get((provider, ingest_key))
        if hit is not None:
            return hit
        return self.connections.get((provider, hash_ingest_key(ingest_key)))


class MemoryInbox:
    def __init__(self) -> None:
        self.envelopes: dict[str, RawEnvelope] = {}
        self.by_delivery: dict[tuple[str, str], str] = {}
        self.tombstones: list[dict[str, Any]] = []
        self.outbox: list[str] = []
        self.assembled: dict[str, int] = {}

    def add_tombstone(self, **hints: Any) -> None:
        self.tombstones.append(hints)

    def is_tombstoned(self, hints: dict[str, Any]) -> bool:
        for stone in self.tombstones:
            if all(hints.get(k) == v for k, v in stone.items() if v is not None):
                return True
        return False

    def accept(
        self,
        envelope: RawEnvelope,
        *,
        delivery_key: str,
        tombstone_hints: dict[str, Any],
    ) -> tuple[str, bool]:
        if self.is_tombstoned(tombstone_hints):
            raise Tombstoned()
        key = (envelope.org_id, delivery_key)
        existing = self.by_delivery.get(key)
        if existing:
            return existing, False
        self.envelopes[envelope.envelope_id] = envelope
        self.by_delivery[key] = envelope.envelope_id
        self.outbox.append(envelope.envelope_id)
        return envelope.envelope_id, True

    def get(self, envelope_id: str) -> RawEnvelope | None:
        return self.envelopes.get(envelope_id)

    def claim(self, limit: int = 32) -> list[str]:
        claimed = self.outbox[:limit]
        self.outbox = self.outbox[limit:]
        return claimed

    def mark_assembled(self, envelope_id: str, revision: int) -> None:
        self.assembled[envelope_id] = revision
        if envelope_id in self.outbox:
            self.outbox = [item for item in self.outbox if item != envelope_id]
