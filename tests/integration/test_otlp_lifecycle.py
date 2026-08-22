"""OTLP receive stays on the inbox path; conflicts are partial success."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.domain.enums import EnvelopeState
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.otel.receiver import serialized_partial_success
from obsalt.plugin.host import LoadedPlugin
from obsalt_pipecat.plugin import PipecatPlugin
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from tests.helpers import example_state


def test_otlp_uses_inbox_and_does_not_forward_on_request_path() -> None:
    state = example_state()
    dest_hits = {"n": 0}

    def _fake_forward(*_args, **_kwargs):
        dest_hits["n"] += 1
        raise AssertionError("forward must not run on the receive path")

    state.destinations = [{"org_id": "acme", "url": "https://example.test/v1/traces"}]
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    assert result.envelope is not None
    assert result.envelope.object_key in state.objects.blobs
    assert state.inbox.outbox
    assert dest_hits["n"] == 0


def test_otlp_span_identity_conflict_is_partial_success() -> None:
    state = example_state()
    client = TestClient(create_app(Settings(environment="test", trace_grace_seconds=0), state))
    first = {
        "resourceSpans": [
            {
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
                ]
            }
        ]
    }
    second = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "turn-changed",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "9",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    ok = client.post(
        "/v1/traces",
        content=json.dumps(first),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert ok.status_code == 200
    conflict = client.post(
        "/v1/traces",
        content=json.dumps(second),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert conflict.status_code == 200
    parsed = ExportTraceServiceResponse()
    parsed.ParseFromString(conflict.content)
    assert parsed.partial_success.rejected_spans == 1
    assert parsed.partial_success.error_message
    expected = serialized_partial_success(rejected=1, error_message="span identity conflict")
    parsed_expected = ExportTraceServiceResponse()
    parsed_expected.ParseFromString(expected)
    assert parsed.partial_success.rejected_spans == parsed_expected.partial_success.rejected_spans
    assert any(key.startswith("org/acme/") for key in state.objects.blobs)
    failed = [
        env
        for env in state.inbox.by_id.values()
        if "span identity conflict" in state.inbox.failures.get(env.envelope_id, "")
    ]
    assert failed
    assert failed[-1].envelope_id not in state.inbox.outbox
    assert failed[-1].state is EnvelopeState.FAILED


def test_otlp_complete_batch_assembles_stage_level() -> None:
    state = example_state(extra_plugins=[LoadedPlugin(PipecatPlugin())])
    client = TestClient(create_app(Settings(environment="test", trace_grace_seconds=0), state))
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "turn",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {"key": "turn.index", "value": {"intValue": "0"}},
                                    {"key": "gen_ai.conversation.id", "value": {"stringValue": "room-1"}},
                                    {"key": "turn.speaker", "value": {"stringValue": "user"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    res = client.post(
        "/v1/traces",
        content=json.dumps(payload),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"]
    item = listed.json()["items"][0]
    detail = client.get(f"/v1/calls/{item['id']}", headers={"X-API-Key": "k"})
    assert detail.status_code == 200
    assert detail.json()["timeline_fidelity"] in {"stage_level", "turn_level"}
