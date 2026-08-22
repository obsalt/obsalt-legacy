"""Webhook receive pipeline. Order is non-negotiable (§6.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

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
) -> ReceiveResult:
    limits = limits or ReceiveLimits()
    if compressed_size is not None and compressed_size > limits.compressed_bytes:
        return _reject(413, "compressed body exceeds limit")
    if len(raw) > limits.expanded_bytes:
        return _reject(413, "expanded body exceeds limit")

    connection = resolver.resolve(provider, ingest_key)
    if connection is None:
        return _reject(404, "unknown ingest key")
    if connection.provider != provider:
        return _reject(404, "provider mismatch")

    singleton = getattr(plugin, "singleton_headers", frozenset())
    dup = require_singleton(headers.as_list(), singleton)
    if dup is not None:
        return ReceiveResult(response=_verify_response(dup), envelope=None, rejected=dup.outcome.value)

    auth = plugin.authenticate(raw, headers.as_list(), connection)
    if not auth.ok:
        return ReceiveResult(response=_verify_response(auth), envelope=None, rejected=auth.outcome.value)

    kind_raw = plugin.classify(raw)
    kind = _as_kind(kind_raw)
    if kind is ObservationalEventKind.REJECTED_SYNCHRONOUS:
        return _reject(400, "synchronous provider callbacks are not accepted on this endpoint")

    delivery = plugin.delivery_key(raw, headers.as_list()) or sha256_bytes(raw)
    hints = plugin.tombstone_hints(raw)
    if inbox.is_tombstoned(connection.org_id, hints):
        # Tombstoned orphan is purged before acknowledgement.
        return ReceiveResult(
            response=plugin.acknowledgement(kind),
            envelope=None,
            rejected="tombstoned",
        )

    content_sha = sha256_bytes(raw)
    key = object_key_for(connection.org_id, provider, delivery, content_sha)
    objects.put(key, raw, content_type="application/json")

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
        headers=headers.allowlisted(),
        received_at=utcnow(),
        body=raw,
    )
    stored, created = inbox.accept(envelope, tombstone_hints=hints)
    ack = plugin.acknowledgement(kind)
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
