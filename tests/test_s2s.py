from __future__ import annotations

from obsalt.domain.enums import PipelineArchitecture, Stage
from obsalt.domain.events import CallObserved, InterruptionObserved, StageObserved
from obsalt.otel.conventions import (
    SPAN_GENERATION,
    SPAN_PLAYOUT,
    SPAN_STT,
    SPAN_TURN,
    SPAN_USER_INPUT,
)
from obsalt.otel.s2s import decode_s2s_spans, is_real_barge_in
from obsalt.plugin.types import ReadableSpan
from obsalt_gemini_live.plugin import GeminiLivePlugin
from obsalt_openai_realtime.plugin import OpenAIRealtimePlugin


def _span(name: str, *, attrs: dict | None = None, start: int = 1_000_000_000, end: int = 2_000_000_000) -> ReadableSpan:
    return ReadableSpan(
        name=name,
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=start,
        end_unix_nano=end,
        attributes=attrs or {},
    )


def test_s2s_does_not_emit_cascade_stages() -> None:
    spans = [
        _span(SPAN_USER_INPUT, attrs={"gen_ai.conversation.id": "c1"}),
        _span(SPAN_GENERATION),
        _span(SPAN_PLAYOUT),
        _span(SPAN_STT, attrs={"gen_ai.conversation.id": "c1"}),
        _span("llm"),
        _span("tts"),
    ]
    events = list(decode_s2s_spans(spans))
    stages = [e.stage for e in events if isinstance(e, StageObserved)]
    assert Stage.USER_INPUT in stages
    assert Stage.GENERATION in stages
    assert Stage.PLAYOUT in stages
    assert Stage.STT not in stages
    assert Stage.LLM not in stages
    assert Stage.TTS not in stages
    call = next(e for e in events if isinstance(e, CallObserved))
    assert call.architecture is PipelineArchitecture.SPEECH_TO_SPEECH


def test_follow_on_user_speech_is_not_barge_in() -> None:
    agent = _span(SPAN_GENERATION, start=1_000_000_000, end=2_000_000_000)
    user = _span(SPAN_USER_INPUT, start=3_000_000_000, end=4_000_000_000, attrs={"turn.speaker": "user"})
    events = list(decode_s2s_spans([agent, user]))
    assert not any(isinstance(e, InterruptionObserved) for e in events)
    assert is_real_barge_in(user) is False


def test_explicit_interrupted_attr_is_barge_in() -> None:
    span = _span(SPAN_GENERATION, attrs={"obsalt.barge_in": True})
    assert is_real_barge_in(span) is True
    events = list(decode_s2s_spans([span]))
    assert any(isinstance(e, InterruptionObserved) for e in events)


def test_openai_and_gemini_plugins_use_s2s_shape() -> None:
    openai = OpenAIRealtimePlugin()
    gemini = GeminiLivePlugin()
    span = _span(SPAN_TURN)  # cascade turn name must not become STT/LLM/TTS
    assert openai.claims(_span(SPAN_USER_INPUT)) > openai.claims(span)
    events = list(openai.decode([_span(SPAN_USER_INPUT, attrs={"gen_ai.conversation.id": "x"})]))
    assert all(not isinstance(e, StageObserved) or e.stage is Stage.USER_INPUT for e in events)
    events_g = list(gemini.decode([_span(SPAN_PLAYOUT, attrs={"gen_ai.conversation.id": "x"})]))
    assert all(not isinstance(e, StageObserved) or e.stage is Stage.PLAYOUT for e in events_g)
