from __future__ import annotations

import inspect

from obsalt.domain.enums import Capability
from obsalt.otel.conventions import (
    GENAI_AUDIO_IN,
    GENAI_AUDIO_IN_LIVEKIT,
    GENAI_PROVIDER,
    GENAI_SYSTEM,
    SPAN_STT_PROVIDER_ATTEMPT,
    SPAN_TOOL,
    SPAN_TURN,
)
from obsalt.otel.mappers import MapperRegistry
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig, ReadableSpan
from obsalt_example.plugin import ExamplePlugin
from obsalt_livekit.plugin import LiveKitPlugin
from obsalt_pipecat.plugin import PipecatPlugin
from obsalt_testkit import OtlpMapperConformanceTests


def test_span_names_are_low_cardinality() -> None:
    assert SPAN_TURN == "turn"
    assert SPAN_STT_PROVIDER_ATTEMPT == "stt.provider_attempt"
    assert SPAN_TOOL == "execute_tool"
    assert "{" not in SPAN_TURN


def test_stream_source_is_declared() -> None:
    plugin = ExamplePlugin()
    assert Capability.STREAM_SOURCE in plugin.capabilities
    assert inspect.isasyncgenfunction(plugin.frames) or inspect.iscoroutinefunction(plugin.frames)


async def _drain_example_stream() -> int:
    plugin = ExamplePlugin()
    cfg = ConnectionConfig(org_id="o", provider="example", connection_id="c", ingest_key_hash="x")
    count = 0
    async for _frame in plugin.frames(cfg):
        count += 1
    return count


def test_example_stream_yields_nothing() -> None:
    import asyncio

    assert asyncio.run(_drain_example_stream()) == 0


def test_pipecat_mapper_claims_and_interval() -> None:
    plugin = PipecatPlugin()
    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        parent_span_id=None,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={"turn.index": 0, "gen_ai.conversation.id": "room-1", "turn.speaker": "user"},
    )
    assert plugin.claims(span) > 0
    events = list(plugin.decode([span]))
    assert events
    registry = MapperRegistry([LoadedPlugin(plugin)])
    assert registry.pick(span) is plugin


def test_pipecat_accepts_provider_name_and_system() -> None:
    plugin = PipecatPlugin()
    named = ReadableSpan(
        name="generate_content",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
        attributes={GENAI_PROVIDER: "openai", "gen_ai.conversation.id": "p1"},
    )
    documented = ReadableSpan(
        name="generate_content",
        trace_id="aa" * 16,
        span_id="cc" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
        attributes={GENAI_SYSTEM: "openai", "gen_ai.conversation.id": "p1"},
    )
    assert plugin.claims(named) > 0
    assert plugin.claims(documented) > 0
    from_named = list(plugin.decode([named]))
    from_docs = list(plugin.decode([documented]))
    assert from_named[0].provenance_by_field["agent_id"].source_path == GENAI_PROVIDER
    assert from_docs[0].provenance_by_field["agent_id"].source_path == GENAI_SYSTEM


class TestPipecatMapperConformance(OtlpMapperConformanceTests):
    plugin = PipecatPlugin()
    spans = [
        ReadableSpan(
            name=SPAN_TURN,
            trace_id="aa" * 16,
            span_id="bb" * 8,
            parent_span_id=None,
            start_unix_nano=1_000_000_000,
            end_unix_nano=2_000_000_000,
            attributes={
                "turn.index": 0,
                "gen_ai.conversation.id": "room-1",
                "turn.speaker": "user",
            },
        )
    ]


class TestLiveKitMapperConformance(OtlpMapperConformanceTests):
    plugin = LiveKitPlugin()
    spans = [
        ReadableSpan(
            name="inference",
            trace_id="aa" * 16,
            span_id="bb" * 8,
            start_unix_nano=1_000_000_000,
            end_unix_nano=2_000_000_000,
            attributes={"lk.room.name": "room-1"},
        )
    ]


def test_livekit_maps_inference_to_llm_not_blanket_e2e() -> None:
    from obsalt.domain.enums import Stage
    from obsalt.domain.events import StageObserved

    plugin = LiveKitPlugin()
    events = list(
        plugin.decode(
            [
                ReadableSpan(
                    name="inference",
                    trace_id="aa" * 16,
                    span_id="bb" * 8,
                    start_unix_nano=1_000_000_000,
                    end_unix_nano=2_000_000_000,
                    attributes={"lk.room.name": "room-1"},
                )
            ]
        )
    )
    stages = [event.stage for event in events if isinstance(event, StageObserved)]
    assert Stage.LLM in stages
    assert Stage.E2E not in stages


def test_livekit_accepts_both_audio_token_attribute_names() -> None:
    plugin = LiveKitPlugin()
    merged = ReadableSpan(
        name="inference",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={"lk.room.name": "room-1", GENAI_AUDIO_IN: 42},
    )
    livekit = ReadableSpan(
        name="inference",
        trace_id="aa" * 16,
        span_id="cc" * 8,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={"lk.room.name": "room-1", GENAI_AUDIO_IN_LIVEKIT: 42},
    )
    assert plugin.claims(merged) > 0
    assert plugin.claims(livekit) > 0
    merged_events = list(plugin.decode([merged]))
    livekit_events = list(plugin.decode([livekit]))
    assert any(getattr(e, "source_path", "").endswith(GENAI_AUDIO_IN) for e in merged_events)
    assert any(
        getattr(e, "source_path", "").endswith(GENAI_AUDIO_IN_LIVEKIT) for e in livekit_events
    )
    assert merged_events[0].provenance_by_field["input_audio_tokens"].source_path == GENAI_AUDIO_IN
    assert (
        livekit_events[0].provenance_by_field["input_audio_tokens"].source_path
        == GENAI_AUDIO_IN_LIVEKIT
    )
