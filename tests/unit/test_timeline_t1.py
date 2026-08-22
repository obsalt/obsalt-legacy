"""T1: never draw what you did not measure."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.assemble.assembler import Assembler
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.otel.conventions import SPAN_GENERATION, SPAN_PLAYOUT, SPAN_STT, SPAN_USER_INPUT
from obsalt.otel.s2s import decode_s2s_spans
from obsalt.plugin.types import ReadableSpan
from obsalt_livekit.plugin import LiveKitPlugin
from obsalt_pipecat.plugin import PipecatPlugin

from tests.helpers import fidelity_declaration


def _stamp(events: list) -> list:
    return [
        stamp_event(
            event,
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=index,
        )
        for index, event in enumerate(events)
    ]


def test_unplaced_duration_is_a_chip_not_a_waterfall() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    call = assembler.assemble(
        "o",
        "cid",
        "vapi",
        _stamp(
            [
                CallObserved(source_call_id="c1"),
                StageObserved(
                    stage=Stage.STT,
                    metric=Metric.DURATION,
                    value_ms=140,
                    turn_index=0,
                    placement=MeasurementPlacement.UNPLACED,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="turnLatencies[].transcriberLatency",
                ),
            ]
        ),
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["unplaced_stage_chips"]
    assert view["unplaced_stage_chips"][0]["value_ms"] == 140


def test_interval_without_both_timestamps_is_demoted() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    started = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    call = assembler.assemble(
        "o",
        "cid",
        "test",
        _stamp(
            [
                CallObserved(source_call_id="c1"),
                StageObserved(
                    stage=Stage.LLM,
                    metric=Metric.DURATION,
                    value_ms=200,
                    placement=MeasurementPlacement.INTERVAL,
                    started_at=started,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="span:llm",
                ),
            ]
        ),
    )
    assert call.stage_measurements[0].placement is MeasurementPlacement.UNPLACED
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []


def test_pipecat_ttfb_is_anchored_not_full_span_interval() -> None:
    plugin = PipecatPlugin()
    span = ReadableSpan(
        name="stt",
        trace_id="a" * 32,
        span_id="b" * 16,
        start_unix_nano=1_000_000_000,
        end_unix_nano=1_400_000_000,
        attributes={"metrics.ttfb": 42.0, "gen_ai.conversation.id": "conv-1"},
    )
    events = list(plugin.decode([span]))
    ttfb = next(e for e in events if isinstance(e, StageObserved) and e.metric is Metric.TTFB)
    duration = next(e for e in events if isinstance(e, StageObserved) and e.metric is Metric.DURATION)
    assert ttfb.placement is MeasurementPlacement.ANCHORED_DURATION
    assert ttfb.ended_at is None
    assert ttfb.value_ms == 42.0
    assert duration.placement is MeasurementPlacement.INTERVAL
    assert duration.started_at and duration.ended_at
    assembler = Assembler(plugin.fidelity, decoder_version="pipecat/1", processing_run_id="r")
    call = assembler.assemble("o", "cid", "pipecat", _stamp(events))
    view = timeline_view(call)
    assert all(item["metric"] != "ttfb" for item in view["stage_intervals"])
    assert any(item["metric"] == "ttfb" for item in view["anchored_stage_chips"])
    assert view["draw_stage_waterfall"] is True


def test_real_intervals_draw_a_waterfall() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC)
    call = assembler.assemble(
        "o",
        "cid",
        "otlp",
        _stamp(
            [
                CallObserved(source_call_id="c1"),
                StageObserved(
                    stage=Stage.STT,
                    metric=Metric.DURATION,
                    value_ms=1000,
                    placement=MeasurementPlacement.INTERVAL,
                    started_at=start,
                    ended_at=end,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="span:stt",
                ),
            ]
        ),
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is True
    assert view["stage_intervals"][0]["started_at"]
    assert view["stage_intervals"][0]["ended_at"]


def test_s2s_does_not_invent_cascade_waterfall_stages() -> None:
    spans = [
        ReadableSpan(
            name=SPAN_USER_INPUT,
            trace_id="aa" * 16,
            span_id="bb" * 8,
            start_unix_nano=1_000_000_000,
            end_unix_nano=2_000_000_000,
            attributes={"gen_ai.conversation.id": "c1"},
        ),
        ReadableSpan(
            name=SPAN_GENERATION,
            trace_id="aa" * 16,
            span_id="cc" * 8,
            start_unix_nano=2_000_000_000,
            end_unix_nano=3_000_000_000,
        ),
        ReadableSpan(
            name=SPAN_PLAYOUT,
            trace_id="aa" * 16,
            span_id="dd" * 8,
            start_unix_nano=3_000_000_000,
            end_unix_nano=4_000_000_000,
        ),
        ReadableSpan(
            name=SPAN_STT,
            trace_id="aa" * 16,
            span_id="ee" * 8,
            start_unix_nano=1_000_000_000,
            end_unix_nano=2_000_000_000,
        ),
    ]
    events = list(decode_s2s_spans(spans))
    stages = [e.stage for e in events if isinstance(e, StageObserved)]
    assert Stage.USER_INPUT in stages
    assert Stage.GENERATION in stages
    assert Stage.PLAYOUT in stages
    assert Stage.STT not in stages
    call = next(e for e in events if isinstance(e, CallObserved))
    assert call.architecture is PipelineArchitecture.SPEECH_TO_SPEECH


def test_livekit_does_not_invent_e2e_for_leftover_spans() -> None:
    plugin = LiveKitPlugin()
    leftover = ReadableSpan(
        name="http.request",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
        attributes={"lk.room.name": "room-1"},
    )
    invalid = ReadableSpan(
        name="conversation",
        trace_id="aa" * 16,
        span_id="cc" * 8,
        start_unix_nano=2_000_000_000,
        end_unix_nano=1_000_000_000,
        attributes={"lk.room.name": "room-1"},
    )
    events = list(plugin.decode([leftover, invalid]))
    stages = [e for e in events if isinstance(e, StageObserved)]
    assert stages == []


def test_anchored_chip_never_gets_an_invented_end() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    call = assembler.assemble(
        "o",
        "cid",
        "pipecat",
        _stamp(
            [
                CallObserved(source_call_id="c1"),
                TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="hi", started_at=start),
                StageObserved(
                    stage=Stage.TTS,
                    metric=Metric.TTFB,
                    value_ms=42,
                    placement=MeasurementPlacement.ANCHORED_DURATION,
                    started_at=start,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="metrics.ttfb",
                ),
            ]
        ),
    )
    view = timeline_view(call)
    chip = view["anchored_stage_chips"][0]
    assert chip["ended_at"] is None
    assert chip["started_at"] is not None
    assert view["draw_stage_waterfall"] is False
