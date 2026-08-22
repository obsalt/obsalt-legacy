from __future__ import annotations

import json
import time
from pathlib import Path

from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.timeline import timeline_view
from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    Provenance,
    Stage,
    Statistic,
    TimelineFidelity,
    VerifyOutcome,
)
from obsalt.domain.events import (
    AggregateObserved,
    GroundingObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.ingest.headers import RawHeaders
from obsalt.plugin.types import ConnectionConfig, RawEnvelope
from obsalt.util import new_id, utcnow
from obsalt.worker.process import MemoryRevisionSink, process_envelope
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_testkit import (
    AuthenticationConformanceTests,
    DecoderConformanceTests,
    SchemaFixtureTests,
    SecondsVsMillisecondsTests,
)
from obsalt_vapi.plugin import VapiPlugin

VAPI_FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-vapi" / "src" / "obsalt_vapi" / "fixtures"
RETELL_FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-retell" / "src" / "obsalt_retell" / "fixtures"
ELEVEN_FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-elevenlabs" / "src" / "obsalt_elevenlabs" / "fixtures"
CARTESIA_FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-cartesia" / "src" / "obsalt_cartesia" / "fixtures"
VAPI_ENUM = Path(__file__).resolve().parents[1] / "packages" / "obsalt-vapi" / "src" / "obsalt_vapi" / "data" / "ended_reasons.json"
RETELL_ENUM = Path(__file__).resolve().parents[1] / "packages" / "obsalt-retell" / "src" / "obsalt_retell" / "data" / "disconnection_reasons.json"


class TestVapiDecoder(DecoderConformanceTests):
    plugin = VapiPlugin()
    fixtures_dir = VAPI_FIXTURES


class TestVapiSchema(SchemaFixtureTests):
    plugin = VapiPlugin()
    fixtures_dir = VAPI_FIXTURES


class TestRetellDecoder(DecoderConformanceTests):
    plugin = RetellPlugin()
    fixtures_dir = RETELL_FIXTURES


class TestRetellSchema(SchemaFixtureTests):
    plugin = RetellPlugin()
    fixtures_dir = RETELL_FIXTURES


class TestRetellUnits(SecondsVsMillisecondsTests):
    def test_word_timestamps_are_seconds(self) -> None:
        plugin = RetellPlugin()
        events = list(plugin.decode(_env(RETELL_FIXTURES / "raw" / "call_ended.json", "retell")))
        # 0.4 seconds after start_timestamp 1755777600000 → 400ms offset, not 400000ms
        from obsalt.domain.events import TurnObserved

        first = next(e for e in events if isinstance(e, TurnObserved))
        assert first.started_at is not None
        start_ms = first.started_at.timestamp() * 1000
        call_start_ms = 1755777600000
        self.assert_seconds_field_converted(0.4, start_ms - call_start_ms)

    def test_latency_values_are_milliseconds(self) -> None:
        plugin = RetellPlugin()
        events = list(plugin.decode(_env(RETELL_FIXTURES / "raw" / "call_ended.json", "retell")))
        e2e = next(e for e in events if isinstance(e, StageObserved) and e.stage is Stage.E2E and e.turn_index == 0)
        self.assert_millisecond_field_unchanged(580, e2e.value_ms)


def test_vapi_reads_published_latency_keys() -> None:
    plugin = VapiPlugin()
    events = list(plugin.decode(_env(VAPI_FIXTURES / "raw" / "end_of_call.json", "vapi")))
    stages = [e for e in events if isinstance(e, StageObserved)]
    assert any(e.stage is Stage.STT and e.value_ms == 140 for e in stages)
    assert any(e.stage is Stage.LLM and e.metric is Metric.TTFT and e.value_ms == 320 for e in stages)
    assert any(e.stage is Stage.TTS and e.value_ms == 90 for e in stages)
    assert any("transcriberLatency" in (e.source_path or "") for e in stages)
    assert any("modelLatency" in (e.source_path or "") for e in stages)
    assert any("voiceLatency" in (e.source_path or "") for e in stages)
    for event in stages:
        assert event.placement is MeasurementPlacement.UNPLACED
        assert event.provenance is Provenance.PROVIDER_REPORTED
        assert event.started_at is None
    assert any(isinstance(e, GroundingObserved) and e.kind.value == "system_prompt" for e in events)
    assert any(isinstance(e, GroundingObserved) and e.kind.value == "tool_result" for e in events)
    assert any(isinstance(e, GroundingObserved) and e.kind.value == "user_text" for e in events)
    assert any(isinstance(e, ToolObserved) for e in events)


def test_vapi_does_not_invent_stage_waterfall() -> None:
    plugin = VapiPlugin()
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    call = process_envelope(
        _env(VAPI_FIXTURES / "raw" / "end_of_call.json", "vapi"),
        plugin,
        declaration=plugin.fidelity,
        pointers=pointers,
        sink=sink,
        decoder_version=plugin.decoder_version,
        source="vapi",
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert call.timeline_fidelity.value == "turn_level"
    assert view["unplaced_stage_chips"]


def test_retell_separates_samples_from_aggregates() -> None:
    plugin = RetellPlugin()
    events = list(plugin.decode(_env(RETELL_FIXTURES / "raw" / "call_ended.json", "retell")))
    samples = [e for e in events if isinstance(e, StageObserved) and e.stage is Stage.E2E]
    aggs = [e for e in events if isinstance(e, AggregateObserved) and e.stage is Stage.E2E]
    assert [e.value_ms for e in samples] == [580, 900]
    assert any(e.statistic is Statistic.P50 and e.value_ms == 620 for e in aggs)
    assert any(e.statistic is Statistic.P95 and e.value_ms == 900 for e in aggs)
    tools = [e for e in events if isinstance(e, ToolObserved)]
    assert any(e.name == "lookup_invoice" for e in tools)
    assert any(isinstance(e, GroundingObserved) and e.kind.value == "tool_result" for e in events)


def test_retell_signature_uses_body_plus_timestamp() -> None:
    plugin = RetellPlugin()
    raw = (RETELL_FIXTURES / "raw" / "call_ended.json").read_bytes()
    ts = str(int(time.time() * 1000))
    secret = "retell-api-key"
    sig = hmac_hex(secret, raw + ts.encode())
    cfg = ConnectionConfig(
        org_id="acme", provider="retell", connection_id="c1", ingest_key_hash="x", secrets={"api_key": secret}
    )
    ok = plugin.authenticate(raw, RawHeaders.from_mapping({"x-retell-signature": f"v={ts},d={sig}"}).as_list(), cfg)
    assert ok.ok
    missing = plugin.authenticate(raw, RawHeaders.from_mapping({"x-retell-signature": f"v={ts},d={sig}"}).as_list(), cfg.model_copy(update={"secrets": {}}))
    assert missing.outcome is VerifyOutcome.MISSING_CREDENTIAL


def test_vapi_legacy_secret_fail_closed() -> None:
    plugin = VapiPlugin()
    raw = b'{"message":{"type":"end-of-call-report","call":{"id":"x"}}}'
    cfg = ConnectionConfig(org_id="acme", provider="vapi", connection_id="c", ingest_key_hash="x", secrets={})
    result = plugin.authenticate(raw, RawHeaders.from_mapping({"x-vapi-secret": "nope"}).as_list(), cfg)
    assert result.outcome is VerifyOutcome.MISSING_CREDENTIAL


def test_vapi_enum_coverage_above_95() -> None:
    from obsalt.analysis.hangup import mapped_count

    codes = json.loads(VAPI_ENUM.read_text())["values"]
    mapped, total = mapped_count(codes, "vapi")
    assert total == len(codes)
    assert mapped / total > 0.95, f"coverage {mapped}/{total} = {mapped/total:.3f}"


def test_retell_enum_coverage_complete() -> None:
    from obsalt.analysis.hangup import mapped_count

    codes = json.loads(RETELL_ENUM.read_text())["values"]
    mapped, total = mapped_count(codes, "retell")
    assert mapped == total


def test_replay_promotes_new_revision() -> None:
    plugin = VapiPlugin()
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    env = _env(VAPI_FIXTURES / "raw" / "end_of_call.json", "vapi")
    first = process_envelope(env, plugin, declaration=plugin.fidelity, pointers=pointers, sink=sink, decoder_version="vapi/3", source="vapi")
    second = process_envelope(env, plugin, declaration=plugin.fidelity, pointers=pointers, sink=sink, decoder_version="vapi/3", source="vapi")
    assert first.revision != second.revision
    assert sink.get("acme", first.call_id, first.revision) is not None


class TestElevenLabsDecoder(DecoderConformanceTests):
    plugin = ElevenLabsPlugin()
    fixtures_dir = ELEVEN_FIXTURES


class TestElevenLabsSchema(SchemaFixtureTests):
    plugin = ElevenLabsPlugin()
    fixtures_dir = ELEVEN_FIXTURES


class TestElevenLabsAuth(AuthenticationConformanceTests):
    plugin = ElevenLabsPlugin()
    connection = ConnectionConfig(
        org_id="acme",
        provider="elevenlabs",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"webhook_secret": "eleven-secret"},
    )
    valid_raw = (ELEVEN_FIXTURES / "raw" / "post_call_transcription.json").read_bytes()

    @property
    def valid_headers(self) -> dict[str, str]:
        ts = str(int(time.time()))
        sig = hmac_hex("eleven-secret", f"{ts}.".encode() + self.valid_raw)
        return {"elevenlabs-signature": f"t={ts},v0={sig}"}


class TestCartesiaDecoder(DecoderConformanceTests):
    plugin = CartesiaPlugin()
    fixtures_dir = CARTESIA_FIXTURES


class TestCartesiaSchema(SchemaFixtureTests):
    plugin = CartesiaPlugin()
    fixtures_dir = CARTESIA_FIXTURES


class TestCartesiaAuth(AuthenticationConformanceTests):
    plugin = CartesiaPlugin()
    connection = ConnectionConfig(
        org_id="acme",
        provider="cartesia",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"webhook_secret": "line-secret"},
    )
    valid_raw = (CARTESIA_FIXTURES / "raw" / "call_ended.json").read_bytes()
    valid_headers = {"x-webhook-secret": "line-secret"}


def test_elevenlabs_coarse_message_anchors() -> None:
    from datetime import timedelta

    from obsalt.domain.coverage import derive_fidelity

    plugin = ElevenLabsPlugin()
    events = list(plugin.decode(_env(ELEVEN_FIXTURES / "raw" / "post_call_transcription.json", "elevenlabs")))
    turns = [e for e in events if isinstance(e, TurnObserved)]
    assert len(turns) == 3
    assert turns[0].started_at is not None and turns[0].ended_at is None
    assert turns[1].started_at is not None
    assert (turns[1].started_at - turns[0].started_at) == timedelta(seconds=3)
    assert (turns[2].started_at - turns[0].started_at) == timedelta(seconds=8)
    assert derive_fidelity(events) is TimelineFidelity.MESSAGE_LEVEL
    assert not any(isinstance(e, StageObserved) and e.stage in {Stage.STT, Stage.LLM, Stage.TTS} for e in events)


def test_cartesia_turn_intervals_and_unplaced_ttfbs() -> None:
    plugin = CartesiaPlugin()
    events = list(plugin.decode(_env(CARTESIA_FIXTURES / "raw" / "call_ended.json", "cartesia")))
    turns = [e for e in events if isinstance(e, TurnObserved)]
    assert all(t.started_at and t.ended_at for t in turns)
    stages = [e for e in events if isinstance(e, StageObserved)]
    assert all(e.placement is MeasurementPlacement.UNPLACED for e in stages)
    assert all(e.started_at is None and e.ended_at is None for e in stages)
    assert any(e.stage is Stage.STT and e.metric is Metric.TTFB and e.value_ms == 90 for e in stages)
    assert any(e.stage is Stage.TTS and e.metric is Metric.TTFB and e.value_ms == 110 for e in stages)
    assert not any(e.placement is MeasurementPlacement.INTERVAL for e in stages)


def _env(path: Path, provider: str) -> RawEnvelope:
    return RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider=provider,
        connection_id="c1",
        object_key="k",
        delivery_key=path.name,
        content_sha256="x",
        body=path.read_bytes(),
        received_at=utcnow(),
        source_call_id=None,
    )
