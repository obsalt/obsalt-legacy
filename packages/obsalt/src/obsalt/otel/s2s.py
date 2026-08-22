"""Speech-to-speech span decode shared by OpenAI Realtime and Gemini Live mappers.

Native stages are user_input / generation / playout. Cascade STT/LLM/TTS stages
do not exist on these sources and must never be emitted, even as empty rows.
Barge-in is taken only from explicit interruption signals, never from the
heuristic "any user speech after an agent turn" (v0.1 100% false positive).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Stage,
)
from obsalt.domain.events import CallObserved, InterruptionObserved, NormalizedEvent, StageObserved
from obsalt.otel.conventions import (
    CONVERSATION_ID,
    OBSALT_BARGE_IN,
    PROVIDER_CALL_ID,
    SPAN_GENERATION,
    SPAN_LLM,
    SPAN_PLAYOUT,
    SPAN_STT,
    SPAN_STT_PROVIDER_ATTEMPT,
    SPAN_STT_SELECTION,
    SPAN_TTS,
    SPAN_USER_INPUT,
    TURN_INDEX,
)
from obsalt.plugin.types import ReadableSpan

S2S_SPAN_TO_STAGE = {
    SPAN_USER_INPUT: Stage.USER_INPUT,
    SPAN_GENERATION: Stage.GENERATION,
    SPAN_PLAYOUT: Stage.PLAYOUT,
}

# Never map these to S2S output. Speech-to-speech has no cascade split.
CASCADE_SPAN_NAMES = frozenset(
    {
        SPAN_STT,
        SPAN_LLM,
        SPAN_TTS,
        SPAN_STT_PROVIDER_ATTEMPT,
        SPAN_STT_SELECTION,
        "stt",
        "llm",
        "tts",
    }
)

_TRUTHY = {True, "1", "true", "True", "yes", "YES"}

_BARGE_IN_ATTRS = (
    OBSALT_BARGE_IN,
    "conversation.interrupted",
    "openai.realtime.interrupted",
    "gemini.interrupted",
    "interrupted",
)

_BARGE_IN_EVENTS = frozenset(
    {
        "interrupted",
        "barge_in",
        "obsalt.barge_in",
        "speech_interrupted",
        "conversation.interrupted",
    }
)

_CANCEL_INTERRUPT_REASONS = frozenset(
    {
        "turn_detected",
        "interrupted",
        "barge_in",
        "client_interrupted",
        "user_interruption",
    }
)


def _truthy(value: object) -> bool:
    return value in _TRUTHY


def is_real_barge_in(span: ReadableSpan) -> bool:
    """True only for explicit interruption signals, not follow-on user speech."""

    attrs = span.attributes or {}
    for key in _BARGE_IN_ATTRS:
        if _truthy(attrs.get(key)):
            return True
    event_name = str(attrs.get("openai.realtime.event") or "")
    if event_name == "conversation.interrupted":
        return True
    reason = str(
        attrs.get("response.status_details.reason")
        or attrs.get("status_details.reason")
        or attrs.get("openai.realtime.cancel_reason")
        or ""
    ).lower()
    status = str(attrs.get("response.status") or attrs.get("status") or "").lower()
    cancelled = status in {"cancelled", "canceled"} or event_name == "response.cancelled"
    if cancelled and reason in _CANCEL_INTERRUPT_REASONS:
        return True
    for event in span.events or []:
        name = str(event.get("name") or "").lower()
        if name in _BARGE_IN_EVENTS:
            return True
    return False


def _turn_index(span: ReadableSpan) -> int | None:
    raw = (span.attributes or {}).get(TURN_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def decode_s2s_spans(
    spans: Sequence[ReadableSpan],
    *,
    architecture: PipelineArchitecture = PipelineArchitecture.SPEECH_TO_SPEECH,
) -> Iterable[NormalizedEvent]:
    if not spans:
        return
    conv = None
    for span in spans:
        attrs = span.attributes or {}
        conv = attrs.get(CONVERSATION_ID) or attrs.get(PROVIDER_CALL_ID) or conv
    if conv:
        yield CallObserved(source_call_id=str(conv), architecture=architecture)
    for span in spans:
        if span.name in CASCADE_SPAN_NAMES:
            continue
        if is_real_barge_in(span):
            yield InterruptionObserved(turn_index=_turn_index(span), count=1, kind="barge_in")
        stage = S2S_SPAN_TO_STAGE.get(span.name)
        if stage is None:
            continue
        yield StageObserved(
            stage=stage,
            metric=Metric.DURATION,
            value_ms=(span.end_unix_nano - span.start_unix_nano) / 1e6,
            turn_index=_turn_index(span),
            placement=MeasurementPlacement.INTERVAL,
            started_at=datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC),
            ended_at=datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC),
            provenance=Provenance.PROVIDER_REPORTED,
            source_path=f"span:{span.name}",
        )
