"""Live Hamming-shaped span tree for in-process voice agents."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from obsalt.tracing.context import extract_traceparent
from obsalt.tracing.conventions import (
    ASSERTION_ID,
    AUDIO_PLAYOUT_MS,
    CALL_DURATION_MS,
    CALL_ERROR_TYPE,
    CALL_STATUS,
    ERROR_TYPE,
    GENAI_OPERATION,
    GENAI_PROVIDER,
    GENAI_REQUEST_MODEL,
    GENAI_TOOL_CALL_ID,
    GENAI_TOOL_NAME,
    GENAI_USAGE_IN,
    GENAI_USAGE_OUT,
    LLM_FINISH_REASON,
    LLM_MODEL,
    LLM_TOKENS_IN,
    LLM_TOKENS_OUT,
    LLM_TTFT_MS,
    SPAN_CALL,
    SPAN_EVAL,
    SPAN_LLM,
    SPAN_PLAYOUT,
    SPAN_STT,
    SPAN_TRANSCRIPT_FINAL,
    SPAN_TTS,
    SPAN_VAD,
    SPAN_WEBHOOK,
    STT_CONFIDENCE,
    STT_LATENCY_MS,
    STT_PROVIDER,
    TOOL_EXECUTION_MS,
    TOOL_NAME,
    TOOL_RETRY_COUNT,
    TOOL_STATUS_CODE,
    TRANSCRIPT_FINAL_STATUS,
    TTS_FIRST_AUDIO_MS,
    TTS_PROVIDER,
    TTS_SYNTHESIS_MS,
    TURN_INDEX,
    TURN_SPEAKER,
    VAD_EOU_MS,
    join_attributes,
    stt_provider_span_name,
    tool_span_name,
    turn_span_name,
)

_SET_ALIASES: dict[str, tuple[str, ...]] = {
    "confidence": (STT_CONFIDENCE,),
    "latency_ms": (STT_LATENCY_MS,),
    "ttft_ms": (LLM_TTFT_MS,),
    "tokens_in": (LLM_TOKENS_IN, GENAI_USAGE_IN),
    "tokens_out": (LLM_TOKENS_OUT, GENAI_USAGE_OUT),
    "finish_reason": (LLM_FINISH_REASON,),
    "execution_ms": (TOOL_EXECUTION_MS,),
    "status_code": (TOOL_STATUS_CODE,),
    "retry_count": (TOOL_RETRY_COUNT,),
    "synthesis_ms": (TTS_SYNTHESIS_MS,),
    "first_audio_ms": (TTS_FIRST_AUDIO_MS,),
    "playout_ms": (AUDIO_PLAYOUT_MS,),
    "end_of_utterance_ms": (VAD_EOU_MS,),
}


def to_unix_ns(value: datetime | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    number = int(value)
    if number < 1_000_000_000_000:  # seconds
        return number * 1_000_000_000
    if number < 1_000_000_000_000_000:  # milliseconds
        return number * 1_000_000
    return number


def _coerce(value: Any) -> AttributeValue:
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _drop_none(attrs: Mapping[str, Any] | None) -> dict[str, AttributeValue]:
    if not attrs:
        return {}
    return {k: _coerce(v) for k, v in attrs.items() if v is not None}


class SpanHandle:
    """Attaches a span as current for a ``with`` body, then ends it."""

    def __init__(
        self,
        span: Span,
        owner: VoiceCallTracer,
        *,
        end_ns: int | None = None,
        turn_index: int | None = None,
    ) -> None:
        self.span = span
        self._owner = owner
        self._end_ns = end_ns
        self._turn_index = turn_index
        self._token: otel_context.Token | None = None
        self._ended = False

    def __enter__(self) -> SpanHandle:
        self._token = otel_context.attach(trace.set_span_in_context(self.span))
        if self._turn_index is not None:
            self._owner._turn_index = self._turn_index
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            self.span.record_exception(exc)
            self.span.set_status(Status(StatusCode.ERROR, exc_type.__name__ if exc_type else "error"))
            self.span.set_attribute(ERROR_TYPE, exc_type.__name__ if exc_type else "error")
        if self._token is not None:
            otel_context.detach(self._token)
            self._token = None
        if self._turn_index is not None:
            self._owner._turn_index = None
        self._end()

    def _end(self) -> None:
        if self._ended:
            return
        self._ended = True
        if self._end_ns is not None:
            self.span.end(end_time=int(self._end_ns))
        else:
            self.span.end()

    def set(self, **kwargs: Any) -> SpanHandle:
        for key, value in kwargs.items():
            if value is None:
                continue
            names = _SET_ALIASES.get(key, (key,))
            for name in names:
                self.span.set_attribute(name, _coerce(value))
        return self

    def fail(self, error_type: str, _detail: str | None = None) -> SpanHandle:
        # Status description stays an error class, never a transcript/rationale snippet.
        self.span.set_status(Status(StatusCode.ERROR, error_type))
        self.span.set_attribute(ERROR_TYPE, error_type)
        return self

    def stt(
        self,
        provider: str | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        attrs = {STT_PROVIDER: provider, **extra}
        return self._owner._child(SPAN_STT, attrs, start_ns=start_ns, end_ns=end_ns)

    def provider_attempt(
        self,
        provider: str,
        *,
        fallback: bool = False,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        return self._owner._child(
            stt_provider_span_name(provider, fallback=fallback),
            {STT_PROVIDER: provider, **extra},
            start_ns=start_ns,
            end_ns=end_ns,
        )

    def llm(
        self,
        model: str | None = None,
        provider: str | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        attrs = {
            GENAI_OPERATION: "chat",
            GENAI_REQUEST_MODEL: model,
            LLM_MODEL: model,
            GENAI_PROVIDER: provider,
            **extra,
        }
        return self._owner._child(SPAN_LLM, attrs, start_ns=start_ns, end_ns=end_ns)

    def tool(
        self,
        name: str,
        *,
        call_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        attrs = {
            GENAI_OPERATION: "execute_tool",
            GENAI_TOOL_NAME: name,
            TOOL_NAME: name,
            GENAI_TOOL_CALL_ID: call_id,
            **extra,
        }
        return self._owner._child(tool_span_name(name), attrs, start_ns=start_ns, end_ns=end_ns)

    def tts(
        self,
        provider: str | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        return self._owner._child(
            SPAN_TTS,
            {TTS_PROVIDER: provider, **extra},
            start_ns=start_ns,
            end_ns=end_ns,
        )

    def playout(self, *, start_ns: int | None = None, end_ns: int | None = None, **extra: Any) -> SpanHandle:
        return self._owner._child(SPAN_PLAYOUT, extra, start_ns=start_ns, end_ns=end_ns)

    def vad(self, *, start_ns: int | None = None, end_ns: int | None = None, **extra: Any) -> SpanHandle:
        return self._owner._child(SPAN_VAD, extra, start_ns=start_ns, end_ns=end_ns)


class VoiceCallTracer(SpanHandle):
    """Root ``call.lifecycle`` plus Hamming child helpers.

    Usage::

        with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
            with call.turn(0) as turn:
                with turn.stt("deepgram") as stt:
                    stt.set(confidence=0.91, latency_ms=412)
            call.set_call_outcome(duration_ms=45_000, status="ended")
    """

    def __init__(self, span: Span, join: dict[str, Any], *, end_ns: int | None = None) -> None:
        super().__init__(span, self, end_ns=end_ns)
        self._join = join
        self._turn_index: int | None = None

    @classmethod
    def start(
        cls,
        call_id: str,
        workspace_id: str,
        agent_id: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        extra_attributes: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        context: otel_context.Context | None = None,
    ) -> VoiceCallTracer:
        parent = context
        if parent is None and headers:
            parent = extract_traceparent(dict(headers))
        join = join_attributes(call_id, workspace_id, agent_id)
        attrs = dict(join)
        attrs.update(_drop_none(extra_attributes))
        kwargs: dict[str, Any] = {}
        if start_ns is not None:
            kwargs["start_time"] = int(start_ns)
        if parent is not None:
            kwargs["context"] = parent
        tracer = trace.get_tracer("obsalt.voice")
        span = tracer.start_span(name=SPAN_CALL, kind=SpanKind.SERVER, attributes=attrs, **kwargs)
        return cls(span, dict(join), end_ns=end_ns)

    def _child(
        self,
        name: str,
        extra: Mapping[str, Any] | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        turn_index: int | None = None,
    ) -> SpanHandle:
        attrs = dict(self._join)
        idx = turn_index if turn_index is not None else self._turn_index
        if idx is not None:
            attrs[TURN_INDEX] = int(idx)
        attrs.update(_drop_none(extra))
        kwargs: dict[str, Any] = {}
        if start_ns is not None:
            kwargs["start_time"] = int(start_ns)
        tracer = trace.get_tracer("obsalt.voice")
        span = tracer.start_span(name, kind=SpanKind.INTERNAL, attributes=attrs, **kwargs)
        return SpanHandle(span, self, end_ns=end_ns, turn_index=turn_index)

    def turn(
        self,
        index: int,
        speaker: str | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        **extra: Any,
    ) -> SpanHandle:
        attrs: dict[str, Any] = {TURN_INDEX: index, **extra}
        if speaker is not None:
            attrs[TURN_SPEAKER] = speaker
        return self._child(turn_span_name(index), attrs, start_ns=start_ns, end_ns=end_ns, turn_index=index)

    def finalize_transcript(self, status: str = "written", **extra: Any) -> SpanHandle:
        return self._child(SPAN_TRANSCRIPT_FINAL, {TRANSCRIPT_FINAL_STATUS: status, **extra})

    def evaluate(self, assertion_id: str, **extra: Any) -> SpanHandle:
        return self._child(SPAN_EVAL, {ASSERTION_ID: assertion_id, **extra})

    def webhook_dispatch(self, **extra: Any) -> SpanHandle:
        return self._child(SPAN_WEBHOOK, extra)

    def set_call_outcome(
        self,
        *,
        duration_ms: int | float | None = None,
        status: str | None = None,
        error_type: str | None = None,
    ) -> None:
        if duration_ms is not None:
            self.span.set_attribute(CALL_DURATION_MS, int(duration_ms))
        if status is not None:
            self.span.set_attribute(CALL_STATUS, status)
        if error_type is not None:
            self.span.set_attribute(CALL_ERROR_TYPE, error_type)
