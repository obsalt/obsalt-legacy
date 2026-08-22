"""Plan-faithful locks for remaining rewrite-plan contracts (§5.1, §6.2, §11, §12, §13)."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.analysis.cluster import MemoryHangupClusterStore
from obsalt.analysis.hangup import mapped_count
from obsalt.api import create_app
from obsalt.assemble.assembler import Assembler
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.timeline import timeline_view
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import (
    AnalysisState,
    EnvelopeState,
    HangupReason,
    KeyScope,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.domain.models import CallRevision, FidelityDeclaration, Hangup
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.otel.span_identity import SpanIdentityIndex
from obsalt.otel.tenancy import reject_tenant_assertions
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig, ReadableSpan
from obsalt.query import hangup_rollup
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.drain import _after_promote
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_example.plugin import ExamplePlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_testkit.schema import FixtureSuite, blocking_errors, validate_raw_fixtures
from obsalt_vapi.plugin import VapiPlugin

ROOT = Path(__file__).resolve().parents[1]
VAPI_FIXTURES = ROOT / "packages" / "obsalt-vapi" / "src" / "obsalt_vapi" / "fixtures"
RETELL_FIXTURES = ROOT / "packages" / "obsalt-retell" / "src" / "obsalt_retell" / "fixtures"
ELEVEN_FIXTURES = ROOT / "packages" / "obsalt-elevenlabs" / "src" / "obsalt_elevenlabs" / "fixtures"
CARTESIA_FIXTURES = ROOT / "packages" / "obsalt-cartesia" / "src" / "obsalt_cartesia" / "fixtures"
VAPI_ENUM = ROOT / "packages" / "obsalt-vapi" / "src" / "obsalt_vapi" / "data" / "ended_reasons.json"


def _decl() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION, Signal.TURN_INTERVAL}),
        structurally_absent={Signal.VAD: "not in fixture"},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def _state() -> AppState:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    return AppState(
        settings=Settings(trace_grace_seconds=0),
        plugins=[LoadedPlugin(plugin)],
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={"k": ("acme", frozenset(KeyScope))},
        rollup_generation="g1",
    )


def test_vapi_prefers_seconds_from_start_over_epoch_time() -> None:
    plugin = VapiPlugin()
    raw = json.loads((VAPI_FIXTURES / "raw" / "end_of_call.json").read_text())
    events = list(plugin.decode(_env(VAPI_FIXTURES / "raw" / "end_of_call.json", "vapi")))
    first = next(event for event in events if isinstance(event, TurnObserved))
    assert first.started_at == datetime(2026, 8, 21, 12, 0, 0, 400000, tzinfo=UTC)
    assert first.provenance_by_field["started_at"].source_path == "artifact.messages[].secondsFromStart"
    call = next(event for event in events if isinstance(event, CallObserved))
    assert call.from_number == raw["message"]["call"]["customer"]["number"]


def test_vapi_falls_back_to_epoch_time_without_seconds_from_start() -> None:
    plugin = VapiPlugin()
    payload = json.loads((VAPI_FIXTURES / "raw" / "end_of_call.json").read_text())
    for message in payload["message"]["artifact"]["messages"]:
        message.pop("secondsFromStart", None)
    events = list(plugin.decode(_env_bytes(json.dumps(payload).encode(), "vapi")))
    first = next(event for event in events if isinstance(event, TurnObserved))
    assert first.started_at == datetime(2025, 8, 21, 12, 0, 0, 400000, tzinfo=UTC)
    assert first.provenance_by_field["started_at"].source_path == "artifact.messages[].time"


def test_retell_tool_uses_coarse_utterance_anchor() -> None:
    plugin = RetellPlugin()
    events = list(plugin.decode(_env(RETELL_FIXTURES / "raw" / "call_ended.json", "retell")))
    from obsalt.domain.events import ToolObserved

    tool = next(event for event in events if isinstance(event, ToolObserved) and event.name == "lookup_invoice")
    assert tool.started_at == datetime(2025, 8, 21, 12, 0, 2, 300000, tzinfo=UTC)
    assert tool.ended_at is None
    stamp = tool.provenance_by_field["started_at"]
    assert stamp.provenance is Provenance.OBSALT_DERIVED
    assert "duration not reported" in (stamp.derivation or "")


def test_vapi_enum_coverage_is_complete() -> None:
    codes = json.loads(VAPI_ENUM.read_text())["values"]
    mapped, total = mapped_count(codes, "vapi")
    assert total == 516
    assert mapped == total


def test_vapi_bearer_and_hmac_auth() -> None:
    plugin = VapiPlugin()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    bearer = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"bearer_token": "tok"},
        settings={"auth_mode": "bearer"},
    )
    ok = plugin.authenticate(raw, RawHeaders.from_mapping({"authorization": "Bearer tok"}).as_list(), bearer)
    assert ok.ok
    missing = plugin.authenticate(raw, RawHeaders.from_mapping({"authorization": "Bearer tok"}).as_list(), bearer.model_copy(update={"secrets": {}}))
    assert not missing.ok
    hmac_cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"hmac_secret": "hs"},
        settings={"auth_mode": "hmac", "timestamp_header": "x-vapi-timestamp"},
    )
    ts = str(int(time.time()))
    sig = hmac_hex("hs", ts.encode() + raw)
    hmac_ok = plugin.authenticate(
        raw,
        RawHeaders.from_mapping({"x-vapi-signature": sig, "x-vapi-timestamp": ts}).as_list(),
        hmac_cfg,
    )
    assert hmac_ok.ok
    stale_ts = str(int(time.time()) - 20 * 60)
    stale_sig = hmac_hex("hs", stale_ts.encode() + raw)
    stale = plugin.authenticate(
        raw,
        RawHeaders.from_mapping({"x-vapi-signature": stale_sig, "x-vapi-timestamp": stale_ts}).as_list(),
        hmac_cfg,
    )
    assert not stale.ok


def test_elevenlabs_stale_timestamp_rejected() -> None:
    from obsalt.domain.enums import VerifyOutcome
    from obsalt_elevenlabs.plugin import ElevenLabsPlugin

    plugin = ElevenLabsPlugin()
    raw = (ELEVEN_FIXTURES / "raw" / "post_call_transcription.json").read_bytes()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="elevenlabs",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"webhook_secret": "eleven-secret"},
    )
    ts = str(int(time.time()) - 40 * 60)
    sig = hmac_hex("eleven-secret", f"{ts}.".encode() + raw)
    result = plugin.authenticate(
        raw,
        RawHeaders.from_mapping({"elevenlabs-signature": f"t={ts},v0={sig}"}).as_list(),
        cfg,
    )
    assert result.outcome is VerifyOutcome.STALE
    assert not result.ok


def test_hosted_plugin_raw_fixtures_pass_schema_harness() -> None:
    for path in (VAPI_FIXTURES, RETELL_FIXTURES, ELEVEN_FIXTURES, CARTESIA_FIXTURES):
        messages = validate_raw_fixtures(FixtureSuite(path))
        assert blocking_errors(messages) == [], messages


def test_anchored_duration_is_not_a_waterfall_interval() -> None:
    start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    events = [
        stamp_event(
            CallObserved(source_call_id="c1"),
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=0,
        ),
        stamp_event(
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=140,
                turn_index=0,
                placement=MeasurementPlacement.ANCHORED_DURATION,
                started_at=start,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="turn.stt",
            ),
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=1,
        ),
    ]
    call = Assembler(_decl(), decoder_version="t/1", processing_run_id="r").assemble("o", "cid", "test", events)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["anchored_stage_chips"]
    assert view["anchored_stage_chips"][0]["placement"] == MeasurementPlacement.ANCHORED_DURATION.value


def test_trace_assembly_redacts_before_buffer_and_keeps_caller_token() -> None:
    assembler = MemoryTraceAssembler()
    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
    )
    record = assembler.ingest(
        "acme",
        [span],
        [CallObserved(source_call_id="c1", from_number="+15551230001")],
    )
    assert record.caller_token
    stored = next(event for event in record.events if isinstance(event, CallObserved))
    assert stored.from_number == "<phone>"


def test_otlp_span_identity_conflict_keeps_raw_and_drops_outbox() -> None:
    state = _state()
    client = TestClient(create_app(Settings(trace_grace_seconds=0), state))
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
    assert conflict.status_code == 409
    assert any(key.startswith("org/acme/") for key in state.objects.blobs)
    failed = [env for env in state.inbox.by_id.values() if "span identity conflict" in state.inbox.failures.get(env.envelope_id, "")]
    assert failed
    assert failed[-1].envelope_id not in state.inbox.outbox
    assert failed[-1].state is EnvelopeState.FAILED


def test_mixed_service_namespace_is_rejected_when_it_conflicts() -> None:
    spans = [
        ReadableSpan(
            name="turn",
            trace_id="aa" * 16,
            span_id="bb" * 8,
            start_unix_nano=1,
            end_unix_nano=2,
            resource={"obsalt.org": "acme", "service.namespace": "other"},
        )
    ]
    assert reject_tenant_assertions(spans, "acme") == "mixed-org assertions in one batch"


def test_plugins_endpoint_requires_api_key() -> None:
    client = TestClient(create_app(Settings(), _state()))
    assert client.get("/v1/plugins").status_code == 401
    assert client.get("/v1/plugins", headers={"X-API-Key": "k"}).status_code == 200


def test_hangup_rollup_serves_materialized_store() -> None:
    store = MemoryHangupClusterStore()
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="vapi",
        source_call_id="s1",
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
    )
    store.refresh("acme", [call], "g1")
    store.by_org["acme"]["clusters"][0]["reason"] = "cached-user-hangup"
    data = hangup_rollup([], as_of_generation="g1", store=store, org_id="acme")
    assert data["items"][0]["reason"] == "cached-user-hangup"


def test_decode_dlq_after_eight_failures() -> None:
    inbox = MemoryInbox()
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import utcnow

    envelope = RawEnvelope(
        envelope_id="e-fail",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        received_at=utcnow(),
    )
    inbox.accept(envelope, tombstone_hints=__import__("obsalt.plugin.types", fromlist=["TombstoneHints"]).TombstoneHints())
    for _ in range(7):
        inbox.mark_failed(envelope.envelope_id, "decode failed")
        assert envelope.envelope_id not in {row["envelope_id"] for row in inbox.dlq}
        assert envelope.envelope_id in inbox.outbox
    inbox.mark_failed(envelope.envelope_id, "decode failed")
    assert inbox.dlq[-1]["envelope_id"] == envelope.envelope_id
    assert envelope.envelope_id not in inbox.outbox


def test_rubric_put_increments_version_on_same_id() -> None:
    client = TestClient(create_app(Settings(), _state()))
    created = client.post(
        "/v1/rubrics",
        headers={"X-API-Key": "k"},
        json={"name": "grounded", "description": "no invented facts"},
    )
    assert created.status_code == 200
    rubric_id = created.json()["id"]
    updated = client.put(
        f"/v1/rubrics/{rubric_id}",
        headers={"X-API-Key": "k"},
        json={"name": "grounded-v2", "description": "stricter"},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == rubric_id
    assert updated.json()["version"] == 2


def test_hallucination_stays_pending_until_tier2() -> None:
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    rows = sink.analysis[(rev.org_id, rev.call_id, rev.revision)]
    hallo = next(row for row in rows if row.execution.analyzer_id == "hallucination")
    assert hallo.execution.state is AnalysisState.PENDING


def test_successful_promote_with_turns_marks_assembled() -> None:
    state = _state()
    from obsalt.plugin.types import RawEnvelope, TombstoneHints
    from obsalt.util import utcnow

    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="hello"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    envelope = RawEnvelope(
        envelope_id="e1",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="e1",
        content_sha256="x",
        state=EnvelopeState.QUEUED,
        received_at=utcnow(),
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    state.inbox.outbox.append(envelope.envelope_id)
    _after_promote(state, envelope, rev, TombstoneHints(source_call_id="c1"))
    assert envelope.state is EnvelopeState.ASSEMBLED
    assert envelope.envelope_id not in state.inbox.failures


def test_receive_otlp_happy_path_still_queues_outbox() -> None:
    state = _state()
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=state.objects,
        inbox=state.inbox,
        span_index=SpanIdentityIndex(),
    )
    assert result.created is True
    assert result.status_code == 200
    assert state.inbox.outbox


def _env(path: Path, provider: str):
    return _env_bytes(path.read_bytes(), provider)


def _env_bytes(body: bytes, provider: str):
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    return RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider=provider,
        connection_id="c1",
        object_key="k",
        delivery_key="fixture",
        content_sha256="x",
        body=body,
        received_at=utcnow(),
    )
