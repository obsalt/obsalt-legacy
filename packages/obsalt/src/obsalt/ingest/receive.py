"""Webhook receive pipeline. Order is non-negotiable.

1. Read raw bytes. Enforce size limits.
2. Resolve ingest_key -> org/connection/plugin.
3. Authenticate with plugin. Fail closed.
4. Classify observational events only.
5. Derive transport delivery key.
6. Persist raw body to org-namespaced object key.
7. Postgres transaction: envelope index + dedupe + outbox + tombstone check.
8. Provider-specific acknowledgement. Decode happens in a worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from obsalt.auth.headers import reject_duplicate_singletons
from obsalt.domain.enums import EnvelopeState, ObservationalEventKind, VerifyOutcome
from obsalt.domain.identity import content_hash, sha256_bytes
from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope, VerifyResult, WebhookResponse

MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024

DIAGNOSTIC_HEADERS = frozenset(
    {
        b"content-type",
        b"user-agent",
        b"x-request-id",
        b"x-correlation-id",
    }
)
FORBIDDEN_ARCHIVE_HEADERS = frozenset(
    {
        b"authorization",
        b"cookie",
        b"set-cookie",
        b"proxy-authorization",
        b"x-api-key",
        b"x-vapi-secret",
        b"x-retell-signature",
        b"elevenlabs-signature",
        b"x-webhook-secret",
        b"x-webhook-signature",
    }
)


class ConnectionResolver(Protocol):
    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None: ...


class ObjectWriter(Protocol):
    def put(self, key: str, body: bytes, *, headers: dict[str, str]) -> None: ...

    def delete(self, key: str) -> None: ...


class Inbox(Protocol):
    def accept(
        self,
        envelope: RawEnvelope,
        *,
        delivery_key: str,
        tombstone_hints: dict[str, Any],
    ) -> tuple[str, bool]:
        """Insert or resume. Returns (envelope_id, created). Raises Tombstoned."""

    def is_tombstoned(self, hints: dict[str, Any]) -> bool: ...


class Tombstoned(Exception):
    pass


@dataclass
class ReceiveResult:
    response: WebhookResponse
    envelope_id: str | None
    verify: VerifyResult | None
    state: EnvelopeState | None
    created: bool = False


def allowlisted_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in headers:
        lowered = key.lower()
        if lowered in FORBIDDEN_ARCHIVE_HEADERS:
            continue
        if lowered in DIAGNOSTIC_HEADERS or lowered.startswith(b"x-obsalt-"):
            out.append((key.decode("latin-1"), value.decode("latin-1")))
    return out


def object_key(org_id: str, provider: str, delivery_key: str) -> str:
    digest = content_hash(delivery_key)[:32]
    return f"raw/{org_id}/{provider}/{digest}"


def fallback_delivery_key(raw: bytes) -> str:
    return f"sha256:{sha256_bytes(raw)}"


class ReceiveService:
    def __init__(
        self,
        *,
        host: PluginHost,
        resolver: ConnectionResolver,
        objects: ObjectWriter,
        inbox: Inbox,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.host = host
        self.resolver = resolver
        self.objects = objects
        self.inbox = inbox
        self.now = now or (lambda: datetime.now(UTC))

    def handle(
        self,
        *,
        provider: str,
        ingest_key: str,
        raw: bytes,
        headers: list[tuple[bytes, bytes]],
        content_encoding: str | None = None,
    ) -> ReceiveResult:
        if len(raw) > MAX_EXPANDED_BYTES:
            return ReceiveResult(
                response=WebhookResponse(status_code=413, body=b'{"error":"payload too large"}'),
                envelope_id=None,
                verify=None,
                state=None,
            )
        _ = content_encoding
        connection = self.resolver.resolve(provider, ingest_key)
        if connection is None:
            return ReceiveResult(
                response=WebhookResponse(status_code=404, body=b'{"error":"unknown ingest key"}'),
                envelope_id=None,
                verify=None,
                state=None,
            )
        plugin = self.host.webhook(provider)
        singleton: frozenset[bytes] = getattr(plugin, "singleton_headers", frozenset())
        dup = reject_duplicate_singletons(headers, singleton)
        if dup is not None:
            return ReceiveResult(
                response=_auth_failure(dup),
                envelope_id=None,
                verify=dup,
                state=None,
            )
        verify = plugin.authenticate(raw, headers, connection)
        if not verify.ok:
            return ReceiveResult(
                response=_auth_failure(verify),
                envelope_id=None,
                verify=verify,
                state=None,
            )
        kind = plugin.classify(raw)
        if kind is ObservationalEventKind.REJECTED_SYNCHRONOUS:
            return ReceiveResult(
                response=WebhookResponse(
                    status_code=400,
                    body=b'{"error":"obsalt accepts observational events only"}',
                ),
                envelope_id=None,
                verify=verify,
                state=None,
            )
        delivery = plugin.delivery_key(raw, headers) or fallback_delivery_key(raw)
        hints = plugin.tombstone_hints(raw).model_dump()
        hints["org_id"] = connection.org_id
        if self.inbox.is_tombstoned(hints):
            return ReceiveResult(
                response=WebhookResponse(status_code=410, body=b'{"error":"tombstoned"}'),
                envelope_id=None,
                verify=verify,
                state=EnvelopeState.TOMBSTONED,
            )
        key = object_key(connection.org_id, provider, delivery)
        archived = allowlisted_headers(headers)
        self.objects.put(key, raw, headers=dict(archived))
        envelope_id = str(uuid4())
        envelope = RawEnvelope(
            envelope_id=envelope_id,
            org_id=connection.org_id,
            provider=provider,
            connection_id=connection.connection_id,
            object_key=key,
            body=None,
            headers=archived,
            delivery_key=delivery,
            received_at=self.now().isoformat(),
            event_kind=kind,
        )
        try:
            stored_id, created = self.inbox.accept(envelope, delivery_key=delivery, tombstone_hints=hints)
        except Tombstoned:
            self.objects.delete(key)
            return ReceiveResult(
                response=WebhookResponse(status_code=410, body=b'{"error":"tombstoned"}'),
                envelope_id=None,
                verify=verify,
                state=EnvelopeState.TOMBSTONED,
            )
        ack = plugin.acknowledgement(kind)
        return ReceiveResult(
            response=ack,
            envelope_id=stored_id,
            verify=verify,
            state=EnvelopeState.QUEUED,
            created=created,
        )


def _auth_failure(verify: VerifyResult) -> WebhookResponse:
    status = {
        VerifyOutcome.MISSING_CREDENTIAL: 401,
        VerifyOutcome.BAD_SIGNATURE: 401,
        VerifyOutcome.MALFORMED: 400,
        VerifyOutcome.STALE: 401,
        VerifyOutcome.REPLAYED: 409,
        VerifyOutcome.OK: 200,
    }[verify.outcome]
    body = json.dumps({"error": verify.outcome.value, "detail": verify.detail}).encode()
    return WebhookResponse(status_code=status, body=body)


def decode_json(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data
