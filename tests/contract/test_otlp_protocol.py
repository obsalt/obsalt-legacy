"""OTLP HTTP contract: content types, proto errors, partial success, backpressure."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.domain.enums import EnvelopeState, ObservationalEventKind
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.otel.receiver import parse_otlp_request, serialized_partial_success, serialized_success
from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore
from obsalt.util import utcnow
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


def test_wrong_content_type_is_415(client: TestClient) -> None:
    res = client.post(
        "/v1/traces",
        content=b"not-otlp",
        headers={"X-API-Key": "k", "content-type": "text/plain"},
    )
    assert res.status_code == 415


def test_malformed_protobuf_is_400(client: TestClient) -> None:
    res = client.post(
        "/v1/traces",
        content=b"\x00\x01not-a-proto",
        headers={"X-API-Key": "k", "content-type": "application/x-protobuf"},
    )
    assert res.status_code == 400


def test_success_body_is_export_trace_service_response(client: TestClient) -> None:
    req = ExportTraceServiceRequest()
    res = client.post(
        "/v1/traces",
        content=req.SerializeToString(),
        headers={"X-API-Key": "k", "content-type": "application/x-protobuf"},
    )
    assert res.status_code == 200
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(res.content)
    assert parsed.SerializeToString() == serialized_success()


def test_json_empty_batch_is_serialized_success(client: TestClient) -> None:
    res = client.post(
        "/v1/traces",
        content=b'{"resourceSpans":[]}',
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 200
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(res.content)
    assert not parsed.HasField("partial_success") or parsed.partial_success.rejected_spans == 0


def test_mixed_org_resource_cannot_choose_tenant(client: TestClient) -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "obsalt.org", "value": {"stringValue": "other"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "turn",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    res = client.post(
        "/v1/traces",
        content=json.dumps(payload),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 401


def test_span_identity_conflict_is_partial_success_not_retryable(memory_state, client: TestClient) -> None:
    from obsalt.otel.span_identity import SpanIdentityIndex
    from obsalt.plugin.types import ReadableSpan

    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
        attributes={"gen_ai.conversation.id": "c1"},
    )
    index = SpanIdentityIndex()
    index.observe("acme", span)
    # Different fingerprint for the same identity is a conflict.
    span.attributes["extra"] = "changed"
    inbox = MemoryInbox()
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=MemoryObjectStore(),
        inbox=inbox,
        spans=[span],
        span_index=index,
    )
    assert result.status_code == 409
    body = serialized_partial_success(rejected=1, error_message="span identity conflict")
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(body)
    assert parsed.partial_success.rejected_spans == 1
    assert parsed.partial_success.error_message


def test_otlp_backpressure_returns_503() -> None:
    inbox = MemoryInbox()
    objects = MemoryObjectStore()
    for index in range(3):
        envelope = RawEnvelope(
            envelope_id=f"e{index}",
            org_id="acme",
            provider="otlp",
            connection_id="otlp",
            object_key=f"k{index}",
            delivery_key=f"d{index}",
            content_sha256=f"s{index}",
            state=EnvelopeState.QUEUED,
            event_kind=ObservationalEventKind.OTLP_BATCH,
            received_at=utcnow(),
            body=b"{}",
        )
        inbox.accept(envelope, tombstone_hints=TombstoneHints())
    result = receive_otlp_batch(
        org_id="acme",
        raw=b"{}",
        content_type="application/json",
        objects=objects,
        inbox=inbox,
        backpressure_limit=3,
    )
    assert result.status_code == 503


def test_parse_otlp_json_roundtrip() -> None:
    req = parse_otlp_request("application/json", b'{"resourceSpans":[]}', None)
    assert len(req.resource_spans) == 0
