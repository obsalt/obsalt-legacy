"""Test doubles. Not a product storage backend — production is Postgres + ClickHouse + object storage."""

from __future__ import annotations

from obsalt.domain.enums import EnvelopeState
from obsalt.plugin.types import ConnectionConfig, RawEnvelope, TombstoneHints
from obsalt.security.secrets import hash_key
from obsalt.util import sha256_bytes


class MemoryObjectStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.blobs[key] = body

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def delete(self, key: str) -> None:
        self.blobs.pop(key, None)


class MemoryInbox:
    def __init__(self) -> None:
        self.envelopes: dict[tuple[str, str], RawEnvelope] = {}
        self.tombstones: list[tuple[str, TombstoneHints]] = []
        self.outbox: list[str] = []

    def tombstone(self, org_id: str, hints: TombstoneHints) -> None:
        self.tombstones.append((org_id, hints))

    def is_tombstoned(self, org_id: str, hints: TombstoneHints) -> bool:
        for stored_org, stored in self.tombstones:
            if stored_org != org_id:
                continue
            if hints.source_call_id and stored.source_call_id == hints.source_call_id:
                return True
        return False

    def accept(self, envelope: RawEnvelope, *, tombstone_hints: TombstoneHints) -> tuple[RawEnvelope, bool]:
        if self.is_tombstoned(envelope.org_id, tombstone_hints):
            envelope.state = EnvelopeState.TOMBSTONED
            return envelope, False
        key = (envelope.org_id, envelope.delivery_key)
        existing = self.envelopes.get(key)
        if existing is not None:
            if existing.state != EnvelopeState.ASSEMBLED:
                # Resume incomplete acceptance instead of blindly returning already-processed.
                if existing.object_key != envelope.object_key:
                    existing.object_key = envelope.object_key
                    existing.content_sha256 = envelope.content_sha256
                if existing.envelope_id not in self.outbox:
                    self.outbox.append(existing.envelope_id)
                return existing, False
            return existing, False
        self.envelopes[key] = envelope
        self.outbox.append(envelope.envelope_id)
        return envelope, True


class MemoryResolver:
    def __init__(self) -> None:
        self.connections: dict[tuple[str, str], ConnectionConfig] = {}

    def add(self, cfg: ConnectionConfig, ingest_key: str) -> None:
        self.connections[(cfg.provider, hash_key(ingest_key))] = cfg
        cfg.ingest_key_hash = hash_key(ingest_key)

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        return self.connections.get((provider, hash_key(ingest_key)))


def digest_body(body: bytes) -> str:
    return sha256_bytes(body)
