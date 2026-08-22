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

    def list_keys(self, prefix: str = "") -> list[str]:
        return [key for key in self.blobs if key.startswith(prefix)]


class MemoryInbox:
    def __init__(self) -> None:
        self.envelopes: dict[tuple[str, str], RawEnvelope] = {}
        self.by_id: dict[str, RawEnvelope] = {}
        self.tombstones: list[tuple[str, TombstoneHints]] = []
        self.outbox: list[str] = []
        self.failures: dict[str, str] = {}
        self.leased: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.dlq: list[dict[str, str]] = []

    def tombstone(self, org_id: str, hints: TombstoneHints) -> None:
        self.tombstones.append((org_id, hints))
        # Suppress matching inbox/outbox work so deletion after receive cannot be undone.
        for envelope in list(self.envelopes.values()):
            if envelope.org_id != org_id:
                continue
            if hints.source_call_id and envelope.source_call_id == hints.source_call_id:
                envelope.state = EnvelopeState.TOMBSTONED
                if envelope.envelope_id in self.outbox:
                    self.outbox.remove(envelope.envelope_id)
                self.leased.discard(envelope.envelope_id)

    def is_tombstoned(self, org_id: str, hints: TombstoneHints) -> bool:
        for stored_org, stored in self.tombstones:
            if stored_org != org_id:
                continue
            if hints.source_call_id and stored.source_call_id == hints.source_call_id:
                return True
            if hints.caller_token and stored.caller_token == hints.caller_token:
                return True
            if stored.covers_event(hints.event_time):
                return True
        return False

    def list_dlq(self) -> list[dict[str, str]]:
        return list(self.dlq)

    def accept(self, envelope: RawEnvelope, *, tombstone_hints: TombstoneHints) -> tuple[RawEnvelope, bool]:
        if self.is_tombstoned(envelope.org_id, tombstone_hints):
            envelope.state = EnvelopeState.TOMBSTONED
            return envelope, False
        key = (envelope.org_id, envelope.delivery_key)
        existing = self.envelopes.get(key)
        if existing is not None:
            if existing.state != EnvelopeState.ASSEMBLED:
                if existing.object_key != envelope.object_key:
                    existing.object_key = envelope.object_key
                    existing.content_sha256 = envelope.content_sha256
                    existing.body = envelope.body
                self.by_id.setdefault(existing.envelope_id, existing)
                if existing.envelope_id not in self.outbox:
                    self.outbox.append(existing.envelope_id)
                return existing, False
            return existing, False
        self.envelopes[key] = envelope
        self.by_id[envelope.envelope_id] = envelope
        self.outbox.append(envelope.envelope_id)
        return envelope, True

    def outbox_depth(self) -> int:
        return len(
            [
                envelope_id
                for envelope_id in self.outbox
                if (env := self.by_id.get(envelope_id)) is not None
                and env.state not in {EnvelopeState.ASSEMBLED, EnvelopeState.TOMBSTONED}
            ]
        )

    def claim_outbox(self, limit: int = 32) -> list[RawEnvelope]:
        claimed: list[RawEnvelope] = []
        for envelope_id in list(self.outbox):
            if len(claimed) >= limit:
                break
            if envelope_id in self.leased:
                continue
            envelope = self.by_id.get(envelope_id)
            if envelope is None:
                continue
            if envelope.state in {EnvelopeState.ASSEMBLED, EnvelopeState.TOMBSTONED}:
                continue
            self.leased.add(envelope_id)
            claimed.append(envelope)
        return claimed

    def mark_assembled(self, envelope_id: str) -> None:
        envelope = self.by_id.get(envelope_id)
        if envelope is not None:
            envelope.state = EnvelopeState.ASSEMBLED
        if envelope_id in self.outbox:
            self.outbox.remove(envelope_id)
        self.leased.discard(envelope_id)

    def mark_failed(self, envelope_id: str, error: str) -> None:
        self.failures[envelope_id] = error
        envelope = self.by_id.get(envelope_id)
        if envelope is not None:
            envelope.state = EnvelopeState.FAILED
        self.leased.discard(envelope_id)
        self.attempts[envelope_id] = self.attempts.get(envelope_id, 0) + 1
        if self.attempts[envelope_id] >= 8:
            self.dlq.append({"envelope_id": envelope_id, "error": error})
            if envelope_id in self.outbox:
                self.outbox.remove(envelope_id)
            from obsalt.metrics import dlq_inserts_total

            dlq_inserts_total.inc()
            return
        if envelope_id not in self.outbox:
            self.outbox.append(envelope_id)

    def get_by_id(self, envelope_id: str) -> RawEnvelope | None:
        return self.by_id.get(envelope_id)

    def list_envelopes(self, org_id: str) -> list[RawEnvelope]:
        return [envelope for envelope in self.by_id.values() if envelope.org_id == org_id]

    def drop_outbox(self, envelope_id: str) -> None:
        if envelope_id in self.outbox:
            self.outbox.remove(envelope_id)
        self.leased.discard(envelope_id)

    def requeue(self, envelope_id: str) -> None:
        envelope = self.by_id.get(envelope_id)
        if envelope is None:
            return
        if envelope.state is EnvelopeState.TOMBSTONED:
            return
        envelope.state = EnvelopeState.QUEUED
        self.leased.discard(envelope_id)
        if envelope_id not in self.outbox:
            self.outbox.append(envelope_id)


class MemoryResolver:
    def __init__(self) -> None:
        self.connections: dict[tuple[str, str], ConnectionConfig] = {}

    def add(self, cfg: ConnectionConfig, ingest_key: str) -> None:
        self.connections[(cfg.provider, hash_key(ingest_key))] = cfg
        cfg.ingest_key_hash = hash_key(ingest_key)

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        return self.connections.get((provider, hash_key(ingest_key)))

    def delete(self, org_id: str, connection_id: str) -> bool:
        for key, cfg in list(self.connections.items()):
            if cfg.org_id == org_id and cfg.connection_id == connection_id:
                del self.connections[key]
                return True
        return False


def digest_body(body: bytes) -> str:
    return sha256_bytes(body)
