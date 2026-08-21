"""Unified custom-agent session: live OTLP spans + evidence snapshot."""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any, Mapping, Self

from opentelemetry import context as otel_context

from obsalt.domain.enums import Provider, Speaker, ToolStatus, parse_provider, speaker_from
from obsalt.sdk import CallRecorder
from obsalt.tracing.context import inject_traceparent
from obsalt.tracing.conventions import (
    CALL_LANGUAGES,
    EVIDENCE_REDACTION,
    EVIDENCE_TRANSCRIPT_ID,
    ROOM_ID,
    SCENARIO_ID,
    TEST_RUN_ID,
)
from obsalt.tracing.evidence import transcript_artifact_id
from obsalt.tracing.tracer import SpanHandle, VoiceCallTracer


class _BoundSpan:
    """Wraps a live span and copies timings/text into the evidence recorder."""

    def __init__(
        self,
        handle: SpanHandle,
        session: VoiceCall,
        *,
        kind: str,
        turn: _BoundTurn | None = None,
        tool_name: str | None = None,
        tool_args: Any = None,
        tool_id: str | None = None,
    ) -> None:
        self._handle = handle
        self._session = session
        self._kind = kind
        self._turn = turn
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._tool_id = tool_id
        self._tool_result: Any = None
        self._tool_ok = True
        self._tool_error: str | None = None
        self._t0 = 0.0
        self._attempt_provider: str | None = None
        self._attempt_fallback = False
        self._attempt_latency: float | None = None
        self._attempt_confidence: float | None = None

    def set(self, **kwargs: Any) -> Self:
        self._handle.set(**kwargs)
        turn = self._turn
        if turn is None:
            return self
        if self._kind == "stt":
            if kwargs.get("latency_ms") is not None:
                turn.stt_ms = float(kwargs["latency_ms"])
            if kwargs.get("confidence") is not None:
                turn.confidence = float(kwargs["confidence"])
        elif self._kind == "stt_attempt":
            if kwargs.get("latency_ms") is not None:
                self._attempt_latency = float(kwargs["latency_ms"])
            if kwargs.get("confidence") is not None:
                self._attempt_confidence = float(kwargs["confidence"])
        elif self._kind == "playout":
            if kwargs.get("playout_ms") is not None:
                turn.playout_ms = float(kwargs["playout_ms"])
        elif self._kind == "vad":
            if kwargs.get("end_of_utterance_ms") is not None:
                turn.vad_eou_ms = float(kwargs["end_of_utterance_ms"])
        elif self._kind == "llm":
            if kwargs.get("ttft_ms") is not None:
                turn.llm_ttft_ms = float(kwargs["ttft_ms"])
            if kwargs.get("tokens_in") is not None or kwargs.get("tokens_out") is not None:
                if turn.llm_ms is None and kwargs.get("ttft_ms") is not None:
                    turn.llm_ms = float(kwargs["ttft_ms"])
        elif self._kind == "tts":
            if kwargs.get("synthesis_ms") is not None:
                turn.tts_ms = float(kwargs["synthesis_ms"])
            if kwargs.get("first_audio_ms") is not None:
                turn.tts_ttfb_ms = float(kwargs["first_audio_ms"])
        elif self._kind == "tool":
            if kwargs.get("execution_ms") is not None:
                self._tool_duration = float(kwargs["execution_ms"])
        return self

    def fail(self, error_type: str, _detail: str | None = None) -> Self:
        self._handle.fail(error_type, _detail)
        self._tool_ok = False
        self._tool_error = error_type
        return self

    def set_result(self, result: Any, *, ok: bool = True) -> Self:
        self._tool_result = result
        self._tool_ok = ok
        if not ok:
            self._tool_error = self._tool_error or "error"
        return self

    def stt(self, provider: str | None = None, **extra: Any) -> _BoundSpan:
        assert self._turn is not None
        if provider:
            self._turn.stt_provider = provider
        return _BoundSpan(self._handle.stt(provider, **extra), self._session, kind="stt", turn=self._turn)

    def provider_attempt(self, provider: str, *, fallback: bool = False, **extra: Any) -> _BoundSpan:
        child = _BoundSpan(
            self._handle.provider_attempt(provider, fallback=fallback, **extra),
            self._session,
            kind="stt_attempt",
            turn=self._turn,
        )
        child._attempt_provider = provider
        child._attempt_fallback = fallback
        return child

    def llm(self, model: str | None = None, provider: str | None = None, **extra: Any) -> _BoundSpan:
        assert self._turn is not None
        if model:
            self._turn.llm_model = model
        if provider:
            self._turn.llm_provider = provider
        return _BoundSpan(self._handle.llm(model, provider, **extra), self._session, kind="llm", turn=self._turn)

    def tool(
        self,
        name: str,
        arguments: Any | None = None,
        *,
        call_id: str | None = None,
        **extra: Any,
    ) -> _BoundSpan:
        return _BoundSpan(
            self._handle.tool(name, call_id=call_id, **extra),
            self._session,
            kind="tool",
            turn=self._turn,
            tool_name=name,
            tool_args=arguments,
            tool_id=call_id,
        )

    def tts(self, provider: str | None = None, **extra: Any) -> _BoundSpan:
        assert self._turn is not None
        if provider:
            self._turn.tts_provider = provider
        return _BoundSpan(self._handle.tts(provider, **extra), self._session, kind="tts", turn=self._turn)

    def playout(self, **extra: Any) -> _BoundSpan:
        return _BoundSpan(self._handle.playout(**extra), self._session, kind="playout", turn=self._turn)

    def vad(self, **extra: Any) -> _BoundSpan:
        return _BoundSpan(self._handle.vad(**extra), self._session, kind="vad", turn=self._turn)

    def __enter__(self) -> Self:
        self._t0 = time.perf_counter()
        self._handle.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        self._handle.__exit__(exc_type, exc, tb)
        turn = self._turn
        if turn is not None:
            if self._kind == "stt" and turn.stt_ms is None:
                turn.stt_ms = elapsed_ms
            elif self._kind == "llm" and turn.llm_ms is None:
                turn.llm_ms = elapsed_ms
            elif self._kind == "tts" and turn.tts_ms is None:
                turn.tts_ms = elapsed_ms
        if self._kind == "stt_attempt" and self._turn is not None and self._attempt_provider:
            latency = self._attempt_latency if self._attempt_latency is not None else elapsed_ms
            self._turn.stt_attempts.append(
                {
                    "provider": self._attempt_provider,
                    "fallback": self._attempt_fallback,
                    "latency_ms": latency,
                    "confidence": self._attempt_confidence,
                    "error": self._tool_error,
                }
            )
        if self._kind == "vad" and self._turn is not None and self._turn.vad_eou_ms is None:
            self._turn.vad_eou_ms = elapsed_ms
        if self._kind == "playout" and self._turn is not None and self._turn.playout_ms is None:
            self._turn.playout_ms = elapsed_ms
        if self._kind == "tool" and self._tool_name:
            duration = getattr(self, "_tool_duration", elapsed_ms)
            status = ToolStatus.SUCCESS if self._tool_ok and exc is None else ToolStatus.ERROR
            error = self._tool_error or (str(exc) if exc else None)
            self._session._recorder.record_tool(
                self._tool_name,
                self._tool_args,
                duration_ms=duration,
                status=status,
                error=error,
                result=self._tool_result,
                tool_id=self._tool_id,
                turn_index=self._turn.index if self._turn else None,
            )


class _BoundTurn(_BoundSpan):
    def __init__(self, handle: SpanHandle, session: VoiceCall, speaker: Speaker, text: str, index: int) -> None:
        super().__init__(handle, session, kind="turn", turn=self)
        self.speaker = speaker
        self.text = text
        self.index = index
        self.stt_ms: float | None = None
        self.llm_ms: float | None = None
        self.llm_ttft_ms: float | None = None
        self.tts_ms: float | None = None
        self.tts_ttfb_ms: float | None = None
        self.interrupted = False
        self.confidence: float | None = None
        self.stt_attempts: list[dict[str, Any]] = []
        self.vad_eou_ms: float | None = None
        self.playout_ms: float | None = None
        self.stt_provider: str | None = None
        self.tts_provider: str | None = None
        self.llm_model: str | None = None
        self.llm_provider: str | None = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        self._handle.__exit__(exc_type, exc, tb)
        metadata: dict[str, Any] = {}
        if self.stt_attempts:
            metadata["stt_attempts"] = self.stt_attempts
        if self.vad_eou_ms is not None:
            metadata["vad.end_of_utterance_ms"] = self.vad_eou_ms
        if self.playout_ms is not None:
            metadata["audio.playout_ms"] = self.playout_ms
        meta = self._session._recorder.call.metadata
        if self.stt_provider:
            meta.setdefault("stt_provider", self.stt_provider)
        if self.tts_provider:
            meta.setdefault("tts_provider", self.tts_provider)
        if self.llm_model:
            meta.setdefault("llm_model", self.llm_model)
        if self.llm_provider:
            meta.setdefault("llm_provider", self.llm_provider)
        self._session._recorder.record_turn(
            self.speaker,
            self.text,
            duration_ms=elapsed_ms,
            stt_ms=self.stt_ms,
            llm_ms=self.llm_ms,
            llm_ttft_ms=self.llm_ttft_ms,
            tts_ms=self.tts_ms,
            tts_ttfb_ms=self.tts_ttfb_ms,
            interrupted=self.interrupted,
            confidence=self.confidence,
            metadata=metadata or None,
        )


class VoiceCall:
    """One object for custom agents (Pipecat, LiveKit, your loop).

    Emits OpenTelemetry spans as the conversation happens **and** records an
    evidence snapshot (transcript, tools, timings). If you pass ``client``,
    the snapshot is POSTed to ``obsalt serve`` when the context exits.

    ``workspace_id`` must match the org on the API key or Tempo ``call.id``
    will not equal ``GET /v1/calls/{id}``.
    """

    def __init__(
        self,
        tracer: VoiceCallTracer,
        recorder: CallRecorder,
        *,
        client: Any | None = None,
        ingest_on_exit: bool = False,
    ) -> None:
        self._tracer = tracer
        self._recorder = recorder
        self._client = client
        self._ingest_on_exit = ingest_on_exit
        self._traceparent: str | None = None
        self._ingest_result: Any = None
        self._sent = False

    @classmethod
    def start(
        cls,
        call_id: str,
        workspace_id: str,
        agent_id: str,
        *,
        provider: Provider | str = Provider.NATIVE,
        client: Any | None = None,
        ingest_on_exit: bool | None = None,
        system_prompt: str = "",
        recording_url: str | None = None,
        extra_attributes: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        context: otel_context.Context | None = None,
    ) -> VoiceCall:
        provider_enum = parse_provider(provider)
        tracer = VoiceCallTracer.start(
            call_id,
            workspace_id,
            agent_id,
            provider=provider_enum,
            extra_attributes=extra_attributes,
            headers=headers,
            context=context,
        )
        recorder = CallRecorder(
            org_id=workspace_id,
            provider=provider_enum,
            call_id=call_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
            recording_url=recording_url,
        )
        if extra_attributes:
            for key in (ROOM_ID, TEST_RUN_ID, SCENARIO_ID, CALL_LANGUAGES):
                if extra_attributes.get(key) is not None:
                    recorder.call.metadata[key] = extra_attributes[key]
        auto = ingest_on_exit if ingest_on_exit is not None else client is not None
        return cls(tracer, recorder, client=client, ingest_on_exit=auto)

    @property
    def obsalt_call_id(self) -> str:
        return self._tracer.obsalt_call_id

    @property
    def provider_call_id(self) -> str:
        return self._tracer.provider_call_id

    @property
    def span(self):
        return self._tracer.span

    def turn(self, index: int, speaker: Speaker | str, text: str = "", **extra: Any) -> _BoundTurn:
        sp = speaker if isinstance(speaker, Speaker) else speaker_from(speaker)
        handle = self._tracer.turn(index, sp.value, **extra)
        return _BoundTurn(handle, self, sp, text, index)

    def finalize_transcript(self, status: str = "written", **extra: Any) -> SpanHandle:
        return self._tracer.finalize_transcript(status, **extra)

    def evaluate(self, assertion_id: str, **extra: Any) -> SpanHandle:
        return self._tracer.evaluate(assertion_id, **extra)

    def set_call_outcome(
        self,
        *,
        duration_ms: int | float | None = None,
        status: str | None = None,
        error_type: str | None = None,
    ) -> None:
        self._tracer.set_call_outcome(duration_ms=duration_ms, status=status, error_type=error_type)

    def snapshot(self, *, hangup_reason: str | None = "completed", final: bool = True) -> dict[str, Any]:
        return self._recorder.snapshot(
            hangup_reason=hangup_reason,
            final=final,
            spans_exported=True,
            traceparent=self._traceparent,
        )

    def send(self, client: Any | None = None, **snapshot_kwargs: Any) -> Any:
        target = client or self._client
        if target is None:
            raise RuntimeError("VoiceCall.send() needs an ObsaltClient (pass client= to start() or send())")
        payload = self.snapshot(**snapshot_kwargs)
        self._stamp_evidence()
        self._ingest_result = target.ingest_native(payload)
        self._sent = True
        return self._ingest_result

    def _stamp_evidence(self) -> None:
        try:
            artifact = transcript_artifact_id(self._recorder.call)
        except Exception:
            return
        self._tracer.span.set_attribute(EVIDENCE_TRANSCRIPT_ID, artifact)
        self._tracer.span.set_attribute(EVIDENCE_REDACTION, "redacted")

    def __enter__(self) -> Self:
        self._tracer.__enter__()
        self._traceparent = inject_traceparent({}).get("traceparent")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        ingest_error: BaseException | None = None
        if self._client is not None and self._ingest_on_exit and not self._sent:
            hangup = "error_unknown" if exc_type else "completed"
            try:
                self.send(hangup_reason=hangup)
            except BaseException as err:  # noqa: BLE001 — re-raise after closing the span
                ingest_error = err
        self._tracer.__exit__(exc_type, exc, tb)
        if ingest_error is not None and exc_type is None:
            raise ingest_error
