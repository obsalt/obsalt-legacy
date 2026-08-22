"""OTLP receive uses the same object-store + inbox/outbox path as webhooks (§6.2).

Decode and forwarding happen in a worker. Destination failures never fail the
ingest acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass

from collections.abc import Sequence

from obsalt.domain.enums import EnvelopeState, ObservationalEventKind
from obsalt.ingest.receive import Inbox, ObjectStore, ReceiveLimits, object_key_for
from obsalt.otel.span_identity import SpanIdentityIndex, otlp_delivery_key as span_delivery_key
from obsalt.plugin.types import RawEnvelope, ReadableSpan, TombstoneHints
from obsalt.util import new_id, sha256_bytes, utcnow


@dataclass
class OtlpReceiveResult:
    envelope: RawEnvelope | None
    created: bool = False
    rejected: str | None = None
    status_code: int = 200


def otlp_delivery_key(org_id: str, raw: bytes, spans: Sequence[ReadableSpan] | None = None) -> str:
    """Identical exporter retries share this key. Prefer per-span identity (§6.2)."""

    return span_delivery_key(org_id, raw, spans)


def receive_otlp_batch(
    *,
    org_id: str,
    raw: bytes,
    content_type: str,
    objects: ObjectStore,
    inbox: Inbox,
    connection_id: str = "otlp-ingest",
    limits: ReceiveLimits | None = None,
    compressed_size: int | None = None,
    source_call_id: str | None = None,
    spans: Sequence[ReadableSpan] | None = None,
    span_index: SpanIdentityIndex | None = None,
) -> OtlpReceiveResult:
    limits = limits or ReceiveLimits()
    if compressed_size is not None and compressed_size > limits.compressed_bytes:
        return OtlpReceiveResult(envelope=None, rejected="compressed body exceeds limit", status_code=413)
    if len(raw) > limits.expanded_bytes:
        return OtlpReceiveResult(envelope=None, rejected="expanded body exceeds limit", status_code=413)

    hints = TombstoneHints(source_call_id=source_call_id)
    if inbox.is_tombstoned(org_id, hints):
        return OtlpReceiveResult(envelope=None, rejected="tombstoned", status_code=200)

    if span_index is not None and spans:
        for span in spans:
            span_index.observe(org_id, span)

    content_sha = sha256_bytes(raw)
    delivery = otlp_delivery_key(org_id, raw, spans)
    key = object_key_for(org_id, "otlp", delivery, content_sha)
    objects.put(key, raw, content_type=content_type)

    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id=org_id,
        provider="otlp",
        connection_id=connection_id,
        object_key=key,
        delivery_key=delivery,
        content_sha256=content_sha,
        state=EnvelopeState.QUEUED,
        event_kind=ObservationalEventKind.OTLP_BATCH,
        source_call_id=source_call_id,
        headers={"content-type": content_type},
        received_at=utcnow(),
        body=raw,
    )
    stored, created = inbox.accept(envelope, tombstone_hints=hints)
    return OtlpReceiveResult(envelope=stored, created=created, status_code=200)
