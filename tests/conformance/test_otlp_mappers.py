"""OTLP mapper and SDK instrumentation conformance against fixture trees."""

from __future__ import annotations

from tests.helpers import ELEVEN_OTLP, GEMINI_OTLP, LIVEKIT_OTLP, OPENAI_OTLP, PIPECAT_OTLP

from obsalt.domain.enums import MeasurementPlacement, Metric, Stage
from obsalt.domain.events import CallObserved, GroundingObserved, OutcomeObserved, StageObserved
from obsalt.otel.conventions import SPAN_STT_PROVIDER_ATTEMPT
from obsalt.otel.foreign import ForeignConventionMapper
from obsalt.plugin.types import ReadableSpan
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_gemini_live.plugin import GeminiLivePlugin
from obsalt_livekit.plugin import LiveKitPlugin
from obsalt_openai_realtime.plugin import OpenAIRealtimePlugin
from obsalt_pipecat.plugin import PipecatPlugin
from obsalt_testkit import (
    OtlpMapperConformanceTests,
    SdkInstrumentationConformanceTests,
    spans_from_fixture,
)


class TestPipecatFixtureMapper(OtlpMapperConformanceTests):
    plugin = PipecatPlugin()
    spans = spans_from_fixture(PIPECAT_OTLP / "stock_trace.json")


class TestLiveKitFixtureMapper(OtlpMapperConformanceTests):
    plugin = LiveKitPlugin()
    spans = spans_from_fixture(LIVEKIT_OTLP / "room_trace.json")


class TestElevenLabsFixtureMapper(OtlpMapperConformanceTests):
    plugin = ElevenLabsPlugin()
    spans = spans_from_fixture(ELEVEN_OTLP / "conversation_spans.json")


class TestOpenAIRealtimeFixtureMapper(OtlpMapperConformanceTests):
    plugin = OpenAIRealtimePlugin()
    spans = spans_from_fixture(OPENAI_OTLP / "s2s_trace.json")


class TestGeminiLiveFixtureMapper(OtlpMapperConformanceTests):
    plugin = GeminiLivePlugin()
    spans = spans_from_fixture(GEMINI_OTLP / "s2s_trace.json")


class TestOpenAISdk(SdkInstrumentationConformanceTests):
    plugin = OpenAIRealtimePlugin()


class TestGeminiSdk(SdkInstrumentationConformanceTests):
    plugin = GeminiLivePlugin()


def test_pipecat_fixture_emits_grounding_and_intervals() -> None:
    plugin = PipecatPlugin()
    events = list(plugin.decode(spans_from_fixture(PIPECAT_OTLP / "stock_trace.json")))
    assert any(
        isinstance(event, GroundingObserved) and "refund" in event.content.lower()
        for event in events
    )
    assert any(
        isinstance(event, GroundingObserved) and event.kind.value == "tool_result"
        for event in events
    )
    intervals = [
        event
        for event in events
        if isinstance(event, StageObserved) and event.placement is MeasurementPlacement.INTERVAL
    ]
    assert intervals
    assert all(event.started_at and event.ended_at for event in intervals)


def test_pipecat_decodes_stt_provider_attempt_and_ttfb() -> None:
    plugin = PipecatPlugin()
    span = ReadableSpan(
        name=SPAN_STT_PROVIDER_ATTEMPT,
        trace_id="a" * 32,
        span_id="b" * 16,
        start_unix_nano=1_000_000_000,
        end_unix_nano=1_200_000_000,
        attributes={"metrics.ttfb": 42.0, "gen_ai.conversation.id": "conv-1"},
    )
    assert plugin.claims(span) > 0
    events = list(plugin.decode([span]))
    ttfb = [
        event
        for event in events
        if isinstance(event, StageObserved) and event.metric is Metric.TTFB
    ]
    duration = [
        event
        for event in events
        if isinstance(event, StageObserved) and event.metric is Metric.DURATION
    ]
    assert ttfb and ttfb[0].value_ms == 42.0
    assert duration and duration[0].stage is Stage.STT


def test_openinference_mapper_claims_and_decodes() -> None:
    mapper = ForeignConventionMapper()
    span = ReadableSpan(
        name="ChatCompletion",
        trace_id="c" * 32,
        span_id="d" * 16,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={
            "openinference.span.kind": "LLM",
            "input.value": "hello",
            "session.id": "sess-1",
        },
    )
    assert mapper.claims(span) == 20
    events = list(mapper.decode([span]))
    assert any(
        isinstance(event, StageObserved) and event.placement is MeasurementPlacement.INTERVAL
        for event in events
    )


def test_gemini_fidelity_declares_turn_interval() -> None:
    from obsalt.domain.enums import Signal

    assert Signal.TURN_INTERVAL in GeminiLivePlugin().fidelity.provides
    assert Signal.HANGUP in GeminiLivePlugin().fidelity.provides
    assert Signal.TRANSCRIPT in GeminiLivePlugin().fidelity.provides


def test_openai_fixture_copies_agent_ending_and_transcript() -> None:
    events = list(OpenAIRealtimePlugin().decode(spans_from_fixture(OPENAI_OTLP / "s2s_trace.json")))
    call = next(event for event in events if isinstance(event, CallObserved))
    assert call.agent_id == "support"
    outcome = next(event for event in events if isinstance(event, OutcomeObserved))
    assert outcome.reason is not None
    assert outcome.reason.value == "user_hangup"
