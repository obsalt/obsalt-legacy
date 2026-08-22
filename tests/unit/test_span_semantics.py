"""Unit tests for T1 span clocks and mapper semantics."""

from __future__ import annotations

from obsalt.domain.enums import MeasurementPlacement, Metric, Stage
from obsalt.domain.events import InterruptionObserved, StageObserved
from obsalt.otel.conventions import SPAN_GENERATION, SPAN_USER_INPUT
from obsalt.otel.s2s import decode_s2s_spans
from obsalt.otel.span_time import stage_from_span_semantics, valid_span_interval
from obsalt.plugin.types import ReadableSpan
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_livekit.plugin import LiveKitPlugin
from obsalt_pipecat.plugin import PipecatPlugin


def _span(
    name: str,
    *,
    start: int | None = 1_000_000_000,
    end: int | None = 2_000_000_000,
    attrs: dict | None = None,
) -> ReadableSpan:
    return ReadableSpan(
        name=name,
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=start or 0,
        end_unix_nano=end or 0,
        attributes=attrs or {},
    )


def test_valid_span_interval_rejects_missing_zero_and_inverted() -> None:
    assert valid_span_interval(_span("stt")) is True
    assert valid_span_interval(_span("stt", start=0, end=2_000_000_000)) is False
    assert valid_span_interval(_span("stt", start=None, end=2_000_000_000)) is False
    assert valid_span_interval(_span("stt", start=2_000_000_000, end=1_000_000_000)) is False
    assert valid_span_interval(_span("stt", start=1_000_000_000, end=1_000_000_000)) is False


def test_stage_from_span_semantics_ignores_leftover_http_even_with_provider_attrs() -> None:
    assert stage_from_span_semantics("conversation") is Stage.E2E
    assert stage_from_span_semantics("stt.transcription") is Stage.STT
    leftover = stage_from_span_semantics(
        "http.request",
        {"elevenlabs.conversation_id": "c1", "lk.room.name": "room-1"},
    )
    assert leftover is None


def test_s2s_skips_invalid_timestamps_but_still_emits_explicit_barge_in() -> None:
    events = list(
        decode_s2s_spans(
            [
                _span(SPAN_USER_INPUT, start=0, end=2_000_000_000, attrs={"gen_ai.conversation.id": "c1"}),
                _span(SPAN_GENERATION, start=3_000_000_000, end=1_000_000_000),
                    _span(SPAN_GENERATION, start=0, end=0, attrs={"obsalt.barge_in": True}),
            ]
        )
    )
    stages = [e for e in events if isinstance(e, StageObserved)]
    assert stages == []
    assert any(isinstance(e, InterruptionObserved) for e in events)


def test_pipecat_does_not_invent_interval_from_inverted_span() -> None:
    plugin = PipecatPlugin()
    events = list(
        plugin.decode(
            [
                _span(
                    "stt.transcription",
                    start=2_000_000_000,
                    end=1_000_000_000,
                    attrs={"metrics.ttfb": 12.0, "gen_ai.conversation.id": "p1"},
                )
            ]
        )
    )
    intervals = [
        e
        for e in events
        if isinstance(e, StageObserved) and e.placement is MeasurementPlacement.INTERVAL
    ]
    assert intervals == []
    ttfb = next(e for e in events if isinstance(e, StageObserved) and e.metric is Metric.TTFB)
    assert ttfb.placement is MeasurementPlacement.ANCHORED_DURATION
    assert ttfb.ended_at is None


def test_elevenlabs_mapper_does_not_mint_e2e_for_leftover_spans() -> None:
    plugin = ElevenLabsPlugin()
    leftover = _span("http.request", attrs={"elevenlabs.conversation_id": "c1"})
    conversation = _span("conversation", attrs={"elevenlabs.conversation_id": "c1"})
    events = list(plugin.decode_spans([leftover, conversation]))
    assert len(events) == 1
    assert events[0].stage is Stage.E2E
    assert events[0].placement is MeasurementPlacement.INTERVAL


def test_livekit_leftover_and_inverted_spans_emit_nothing() -> None:
    plugin = LiveKitPlugin()
    events = list(
        plugin.decode(
            [
                _span("http.request", attrs={"lk.room.name": "room-1"}),
                _span("conversation", start=2_000_000_000, end=1_000_000_000, attrs={"lk.room.name": "room-1"}),
            ]
        )
    )
    assert [e for e in events if isinstance(e, StageObserved)] == []
