"""OTLP span identity is (org, trace, span) plus a content fingerprint."""

from __future__ import annotations

from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.otel.span_identity import SpanIdentityIndex, otlp_delivery_key, span_content_fingerprint
from obsalt.otel.tenancy import reject_tenant_assertions
from obsalt.plugin.types import ReadableSpan
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore


def _span(**kwargs) -> ReadableSpan:
    base = dict(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
        attributes={"k": "v"},
    )
    base.update(kwargs)
    return ReadableSpan(**base)


def test_delivery_key_ignores_raw_padding_and_changes_with_span() -> None:
    span = _span()
    raw = b'{"resourceSpans":[]}'
    assert otlp_delivery_key("acme", raw, [span]) == otlp_delivery_key("acme", raw + b" ", [span])
    other = _span(end_unix_nano=99)
    assert otlp_delivery_key("acme", raw, [other]) != otlp_delivery_key("acme", raw, [span])


def test_index_accepts_duplicate_and_conflicts_on_fingerprint() -> None:
    index = SpanIdentityIndex()
    first = _span()
    assert index.observe("acme", first) == "accepted"
    assert index.observe("acme", _span()) == "duplicate"
    assert index.observe("acme", _span(end_unix_nano=99)) == "conflict"
    assert span_content_fingerprint(first)


def test_mixed_service_namespace_is_rejected_when_it_conflicts() -> None:
    spans = [
        _span(resource={"obsalt.org": "acme", "service.namespace": "other"}),
    ]
    assert reject_tenant_assertions(spans, "acme") == "mixed-org assertions in one batch"


def test_receive_records_identity_and_queues_outbox() -> None:
    inbox = MemoryInbox()
    objects = MemoryObjectStore()
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=objects,
        inbox=inbox,
        spans=[_span()],
        span_index=SpanIdentityIndex(),
    )
    assert result.created is True
    assert result.status_code == 200
    assert inbox.outbox
    assert result.envelope is not None
    assert result.envelope.object_key in objects.blobs


def test_identity_conflict_keeps_raw_and_drops_outbox() -> None:
    first = _span()
    second = _span(name="turn-changed", end_unix_nano=9, attributes={"extra": "changed"})
    index = SpanIdentityIndex()
    assert index.observe("acme", first) == "accepted"
    inbox = MemoryInbox()
    objects = MemoryObjectStore()
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=objects,
        inbox=inbox,
        spans=[second],
        span_index=index,
    )
    assert result.status_code == 409
    assert result.rejected == "span identity conflict"
    assert result.envelope is not None
    assert result.envelope.object_key in objects.blobs
    assert result.envelope.envelope_id not in inbox.outbox
    assert inbox.failures[result.envelope.envelope_id] == "span identity conflict"
