from __future__ import annotations

import inspect

from obsalt.domain.enums import Capability
from obsalt.otel.conventions import SPAN_STT_PROVIDER_ATTEMPT, SPAN_TOOL, SPAN_TURN
from obsalt.otel.mappers import MapperRegistry
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig, ReadableSpan
from obsalt_example.plugin import ExamplePlugin
from obsalt_pipecat.plugin import PipecatPlugin


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
