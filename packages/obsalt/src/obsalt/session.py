"""Thin OpenTelemetry wrapper for custom agents. Emits OTLP; does not POST an obsalt JSON snapshot."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span

from obsalt.otel.conventions import (
    AGENT_ID,
    CALL_ID,
    OBSALT_HANGUP_PROVIDER_CODE,
    OBSALT_HANGUP_REASON,
    PII_AGENT_TRANSCRIPT,
    PII_USER_TRANSCRIPT,
    PROVIDER_CALL_ID,
    SPAN_CALL,
    SPAN_GENERATION,
    SPAN_LLM,
    SPAN_PLAYOUT,
    SPAN_STT_PROVIDER_ATTEMPT,
    SPAN_TOOL,
    SPAN_TTS,
    SPAN_TURN,
    SPAN_USER_INPUT,
    STT_FALLBACK,
    STT_PROVIDER,
    TURN_INDEX,
    TURN_SPEAKER,
)


class VoiceCall:
    """Low-cardinality tracer for custom agents. Hosted platforms use webhooks instead.

    ``org_id`` corroborates the ingest key. It cannot choose another organization.
    Transcript text belongs on ``obsalt.pii.*`` attributes, never in span names.
    """

    def __init__(self, span: Span, call_id: str, provider_call_id: str) -> None:
        self._span = span
        self.call_id = call_id
        self.provider_call_id = provider_call_id

    @classmethod
    @contextmanager
    def start(
        cls,
        *,
        call_id: str,
        org_id: str,
        agent_id: str,
        provider_call_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Iterator[VoiceCall]:
        """Open the call-lifecycle span. ``conversation_id`` becomes ``gen_ai.conversation.id``."""
        tracer = trace.get_tracer("obsalt")
        attrs: dict[str, Any] = {
            CALL_ID: call_id,
            AGENT_ID: agent_id,
            "obsalt.org": org_id,
            PROVIDER_CALL_ID: provider_call_id or call_id,
        }
        if conversation_id:
            attrs["gen_ai.conversation.id"] = conversation_id
        with tracer.start_as_current_span(SPAN_CALL, attributes=attrs) as span:
            yield cls(span, call_id, provider_call_id or call_id)

    @contextmanager
    def turn(self, index: int, speaker: str, text: str = "") -> Iterator[TurnHandle]:
        tracer = trace.get_tracer("obsalt")
        attrs: dict[str, Any] = {TURN_INDEX: index, TURN_SPEAKER: speaker}
        pii_key = PII_USER_TRANSCRIPT if speaker == "user" else PII_AGENT_TRANSCRIPT
        if text:
            attrs[pii_key] = text
        with tracer.start_as_current_span(SPAN_TURN, attributes=attrs) as span:
            yield TurnHandle(span, index)

    @contextmanager
    def user_input(self) -> Iterator[Span]:
        tracer = trace.get_tracer("obsalt")
        with tracer.start_as_current_span(SPAN_USER_INPUT) as span:
            yield span

    @contextmanager
    def generation(self) -> Iterator[Span]:
        tracer = trace.get_tracer("obsalt")
        with tracer.start_as_current_span(SPAN_GENERATION) as span:
            yield span

    @contextmanager
    def playout(self) -> Iterator[Span]:
        tracer = trace.get_tracer("obsalt")
        with tracer.start_as_current_span(SPAN_PLAYOUT) as span:
            yield span

    def end(self, reason: str, *, provider_code: str | None = None) -> None:
        """Record why the call ended. ``reason`` is a hangup taxonomy value."""
        self._span.set_attribute(OBSALT_HANGUP_REASON, reason)
        if provider_code:
            self._span.set_attribute(OBSALT_HANGUP_PROVIDER_CODE, provider_code)


class TurnHandle:
    def __init__(self, span: Span, index: int) -> None:
        self._span = span
        self.index = index

    @contextmanager
    def stt(self, provider: str, *, fallback: bool = False) -> Iterator[StageHandle]:
        tracer = trace.get_tracer("obsalt")
        attrs: dict[str, str | bool | int] = {
            STT_PROVIDER: provider,
            STT_FALLBACK: fallback,
            TURN_INDEX: self.index,
        }
        with tracer.start_as_current_span(SPAN_STT_PROVIDER_ATTEMPT, attributes=attrs) as span:
            yield StageHandle(span)

    @contextmanager
    def llm(self, model: str) -> Iterator[StageHandle]:
        tracer = trace.get_tracer("obsalt")
        with tracer.start_as_current_span(
            SPAN_LLM, attributes={"gen_ai.request.model": model, TURN_INDEX: self.index}
        ) as span:
            yield StageHandle(span)

    @contextmanager
    def tts(self, provider: str) -> Iterator[StageHandle]:
        tracer = trace.get_tracer("obsalt")
        with tracer.start_as_current_span(
            SPAN_TTS, attributes={"tts.provider": provider, TURN_INDEX: self.index}
        ) as span:
            yield StageHandle(span)

    @contextmanager
    def tool(self, name: str, tool_id: str) -> Iterator[StageHandle]:
        tracer = trace.get_tracer("obsalt")
        attrs: dict[str, str | int] = {
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": tool_id,
            TURN_INDEX: self.index,
        }
        with tracer.start_as_current_span(SPAN_TOOL, attributes=attrs) as span:
            yield StageHandle(span)


class StageHandle:
    def __init__(self, span: Span) -> None:
        self._span = span

    def set(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            self._span.set_attribute(key, value)
