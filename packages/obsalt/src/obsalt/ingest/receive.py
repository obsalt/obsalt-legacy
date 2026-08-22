"""Webhook receive pipeline. Order is non-negotiable (§6.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from obsalt.crypto.primitives import require_singleton
from obsalt.domain.enums import EnvelopeState, ObservationalEventKind, VerifyOutcome
from obsalt.ingest.headers import RawHeaders
from obsalt.plugin.contract import WebhookSource
from obsalt.plugin.types import (
    ConnectionConfig,
    RawEnvelope,
    TombstoneHints,
    VerifyResult,
    WebhookResponse,
)
from obsalt.util import new_id, sha256_bytes, utcnow

log = logging.getLogger("obsalt.ingest")

DEFAULT_COMPRESSED_LIMIT = 1_000_000
DEFAULT_EXPANDED_LIMIT = 8_000_000


class ObjectStore(Protocol):
    def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class Inbox(Protocol):
    def accept(
        self,
        envelope: RawEnvelope,
        *,
        tombstone_hints: TombstoneHints,
    ) -> tuple[RawEnvelope, bool]:
        """Insert or resume the envelope, delivery-key dedupe, and outbox in one transaction.

        Returns (envelope, created). Duplicate requests resume incomplete acceptance.
        """

    def is_tombstoned(self, org_id: str, hints: TombstoneHints) -> bool: ...

    def tombstone(self, org_id: str, hints: TombstoneHints) -> None: ...

    def claim_outbox(self, limit: int = 32) -> list[RawEnvelope]: ...

    def outbox_depth(self) -> int: ...

    def mark_assembled(self, envelope_id: str) -> None: ...

    def mark_failed(self, envelope_id: str, error: str) -> None: ...

    def get_by_id(self, envelope_id: str) -> RawEnvelope | None: ...

    def list_envelopes(self, org_id: str) -> list[RawEnvelope]: ...

    def requeue(self, envelope_id: str) -> None: ...


class ConnectionResolver(Protocol):
    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None: ...


@dataclass
class ReceiveLimits:
    compressed_bytes: int = DEFAULT_COMPRESSED_LIMIT
    expanded_bytes: int = DEFAULT_EXPANDED_LIMIT


@dataclass
class ReceiveResult:
    response: WebhookResponse
    envelope: RawEnvelope | None
    created: bool = False
    rejected: str | None = None


def object_key_for(org_id: str, provider: str, delivery_key: str, content_sha: str) -> str:
    return f"org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}"


def normalize_content_encoding(value: str | None) -> str | None:
    if not value:
        return None
    encoding = value.split(",")[0].strip().lower()
    if encoding in {"", "identity"}:
        return None
    return encoding


def decoded_envelope_body(envelope: RawEnvelope) -> bytes:
    """Expand persisted wire bytes for classify/decode. Archive stays verbatim."""

    raw = envelope.body or b""
    encoding = normalize_content_encoding((envelope.headers or {}).get("content-encoding"))
    if encoding is None:
        return raw
    from obsalt.otel.receiver import decompress_body

    return decompress_body(raw, encoding)


def receive_webhook(
    *,
    provider: str,
    ingest_key: str,
    raw: bytes,
    headers: RawHeaders,
    resolver: ConnectionResolver,
    plugin: WebhookSource,
    objects: ObjectStore,
    inbox: Inbox,
    limits: ReceiveLimits | None = None,
    compressed_size: int | None = None,
    leases: Any | None = None,
    content_encoding: str | None = None,
) -> ReceiveResult:
    """Authenticate wire bytes first (§6.1). Decompress only after auth for classify/decode."""

    limits = limits or ReceiveLimits()
    wire = raw
    encoding = normalize_content_encoding(content_encoding)
    compressed = compressed_size if compressed_size is not None else len(wire)
    if compressed > limits.compressed_bytes:
        return _reject(413, "compressed body exceeds limit")

    connection = resolver.resolve(provider, ingest_key)
    if connection is None:
        return _reject(404, "unknown ingest key")
    if connection.provider != provider:
        return _reject(404, "provider mismatch")

    singleton = getattr(plugin, "singleton_headers", frozenset())
    dup = require_singleton(headers.as_list(), singleton)
    if dup is not None:
        return ReceiveResult(response=_verify_response(dup), envelope=None, rejected=dup.outcome.value)

    auth = plugin.authenticate(wire, headers.as_list(), connection)
    decoded = wire
    if encoding in {"gzip", "deflate"}:
        from obsalt.otel.receiver import decompress_body

        if not auth.ok:
            try:
                expanded = decompress_body(wire, encoding)
            except Exception:
                expanded = None
            if expanded is not None:
                fallback = plugin.authenticate(expanded, headers.as_list(), connection)
                if fallback.ok:
                    auth = fallback
                    decoded = expanded
        if auth.ok and decoded is wire:
            try:
                decoded = decompress_body(wire, encoding)
            except Exception:
                return _reject(400, "malformed compressed body")
    if not auth.ok:
        return ReceiveResult(response=_verify_response(auth), envelope=None, rejected=auth.outcome.value)
    if len(decoded) > limits.expanded_bytes:
        return _reject(413, "expanded body exceeds limit")

    kind_raw = plugin.classify(decoded)
    kind = _as_kind(kind_raw)
    if kind is ObservationalEventKind.REJECTED_SYNCHRONOUS:
        return _reject(400, "synchronous provider callbacks are not accepted on this endpoint")

    delivery = plugin.delivery_key(decoded, headers.as_list()) or sha256_bytes(decoded)
    hints = plugin.tombstone_hints(decoded)
    if inbox.is_tombstoned(connection.org_id, hints):
        # Tombstoned orphan is purged before acknowledgement.
        return ReceiveResult(
            response=plugin.acknowledgement(kind),
            envelope=None,
            rejected="tombstoned",
        )

    content_sha = sha256_bytes(wire)
    key = object_key_for(connection.org_id, provider, delivery, content_sha)
    objects.put(key, wire, content_type="application/json")
    stored_headers = headers.allowlisted()
    if encoding:
        stored_headers["content-encoding"] = encoding

    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id=connection.org_id,
        provider=provider,
        connection_id=connection.connection_id,
        object_key=key,
        delivery_key=delivery,
        content_sha256=content_sha,
        state=EnvelopeState.QUEUED,
        event_kind=kind,
        source_call_id=hints.source_call_id,
        headers=stored_headers,
        received_at=utcnow(),
        body=wire,
    )
    stored, created = inbox.accept(envelope, tombstone_hints=hints)
    if stored.state is EnvelopeState.TOMBSTONED:
        try:
            objects.delete(key)
        except Exception:
            log.warning("failed to purge tombstoned orphan %s", key)
        return ReceiveResult(
            response=plugin.acknowledgement(kind),
            envelope=None,
            rejected="tombstoned",
        )
    ack = plugin.acknowledgement(kind)
    if leases is not None:
        try:
            leases.notify(stored.envelope_id)
        except Exception:
            log.warning("lease notify failed; postgres outbox remains authoritative")
    return ReceiveResult(response=ack, envelope=stored, created=created)


def _as_kind(value: object) -> ObservationalEventKind:
    if isinstance(value, ObservationalEventKind):
        return value
    if isinstance(value, str):
        try:
            return ObservationalEventKind(value)
        except ValueError:
            return ObservationalEventKind.UNKNOWN_OBSERVATIONAL
    return ObservationalEventKind.UNKNOWN_OBSERVATIONAL


def _verify_response(result: VerifyResult) -> WebhookResponse:
    status = {
        VerifyOutcome.MISSING_CREDENTIAL: 401,
        VerifyOutcome.BAD_SIGNATURE: 401,
        VerifyOutcome.MALFORMED: 400,
        VerifyOutcome.STALE: 401,
        VerifyOutcome.REPLAYED: 401,
    }.get(result.outcome, 401)
    return WebhookResponse(status_code=status, body=b'{"error":"unauthorized"}')


def _reject(status: int, detail: str) -> ReceiveResult:
    return ReceiveResult(
        response=WebhookResponse(status_code=status, body=detail.encode()),
        envelope=None,
        rejected=detail,
    )
