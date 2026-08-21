from __future__ import annotations

import time
from types import TracebackType
from typing import Any, Self

from obsalt.domain.enums import (
    LatencyComponent,
    Provider,
    Speaker,
    ToolStatus,
    parse_provider,
    speaker_from,
)
from obsalt.domain.models import CanonicalCall, LatencySample, NativeSnapshot, ToolInvocation, Turn
from obsalt.domain.redact import payload_shape, preview_text, redact_value
from obsalt.util import call_id_for, canonical_json, new_id, sha256_text, utcnow


class _Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.elapsed_ms: float | None = None

    def stop(self) -> float:
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000.0
        return self.elapsed_ms


class TurnSpan:
    def __init__(self, tracer: CallRecorder, speaker: Speaker, text: str = "") -> None:
        self._tracer = tracer
        self.speaker = speaker
        self.text = text
        self.timer = _Timer()
        self.stt_ms: float | None = None
        self.llm_ms: float | None = None
        self.llm_ttft_ms: float | None = None
        self.tts_ms: float | None = None
        self.tts_ttfb_ms: float | None = None
        self.interrupted = False
        self.confidence: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        elapsed = self.timer.stop()
        self._tracer.record_turn(
            self.speaker,
            self.text,
            duration_ms=elapsed,
            stt_ms=self.stt_ms,
            llm_ms=self.llm_ms,
            llm_ttft_ms=self.llm_ttft_ms,
            tts_ms=self.tts_ms,
            tts_ttfb_ms=self.tts_ttfb_ms,
            interrupted=self.interrupted,
            confidence=self.confidence,
        )


class ToolSpan:
    def __init__(self, tracer: CallRecorder, name: str, arguments: Any | None = None) -> None:
        self._tracer = tracer
        self.name = name
        self.arguments = arguments or {}
        self.timer = _Timer()
        self.status = ToolStatus.SUCCESS
        self.error: str | None = None
        self.result: Any | None = None
        self.id = new_id()

    def fail(self, error: str) -> None:
        self.status = ToolStatus.ERROR
        self.error = error

    def set_result(self, result: Any, *, ok: bool = True) -> None:
        self.result = result
        self.status = ToolStatus.SUCCESS if ok else ToolStatus.ERROR

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: TracebackType | None) -> None:
        elapsed = self.timer.stop()
        if exc is not None and self.status == ToolStatus.SUCCESS:
            self.status = ToolStatus.ERROR
            self.error = str(exc)
        last_user = next((t for t in reversed(self._tracer.call.turns) if t.speaker == Speaker.USER), None)
        ttt = None
        if last_user and last_user.seconds_from_start is not None:
            ttt = max(0.0, self._tracer.now_ms() - last_user.seconds_from_start * 1000.0)
        self._tracer.record_tool(
            self.name,
            self.arguments,
            duration_ms=elapsed,
            time_to_tool_ms=ttt,
            status=self.status,
            error=self.error,
            result=self.result,
            tool_id=self.id,
        )


class CallRecorder:
    """Build a call snapshot and POST it to ``/v1/ingest/native``.

    Use this when you do not want to emit OpenTelemetry from the agent process.
    The ingest server stores the snapshot as evidence and reconstructs traces.

    For live spans **and** evidence from one object, use ``obsalt.VoiceCall``.
    To emit spans only, use ``obsalt.VoiceCallTracer``.
    """

    def __init__(
        self,
        *,
        org_id: str = "demo",
        provider: Provider | str = Provider.NATIVE,
        call_id: str | None = None,
        agent_id: str = "unknown",
        agent_name: str | None = None,
        system_prompt: str = "",
        recording_url: str | None = None,
    ) -> None:
        provider_enum = parse_provider(provider)
        provider_call_id = call_id or new_id()
        self._t0 = time.perf_counter()
        self.call = CanonicalCall(
            id=call_id_for(org_id, provider_enum.value, provider_call_id),
            org_id=org_id,
            provider=provider_enum,
            provider_call_id=provider_call_id,
            agent_id=agent_id,
            agent_name=agent_name,
            started_at=utcnow(),
            recording_url=recording_url,
        )
        if system_prompt:
            self.call.grounding.system_prompt = system_prompt

    def now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0

    def turn(self, speaker: Speaker | str, text: str = "") -> TurnSpan:
        sp = speaker if isinstance(speaker, Speaker) else speaker_from(speaker)
        return TurnSpan(self, sp, text)

    def tool(self, name: str, arguments: Any | None = None) -> ToolSpan:
        return ToolSpan(self, name, arguments)

    def record_turn(
        self,
        speaker: Speaker | str,
        text: str = "",
        *,
        duration_ms: float | None = None,
        stt_ms: float | None = None,
        llm_ms: float | None = None,
        llm_ttft_ms: float | None = None,
        tts_ms: float | None = None,
        tts_ttfb_ms: float | None = None,
        interrupted: bool = False,
        confidence: float | None = None,
        seconds_from_start: float | None = None,
    ) -> Turn:
        sp = speaker if isinstance(speaker, Speaker) else speaker_from(speaker)
        elapsed = duration_ms if duration_ms is not None else 0.0
        t_ms = self.now_ms()
        start_s = seconds_from_start
        if start_s is None:
            start_s = max(0.0, (t_ms - elapsed) / 1000.0)
        turn = Turn(
            index=len(self.call.turns),
            speaker=sp,
            text=text,
            duration_ms=elapsed,
            seconds_from_start=start_s,
            stt_ms=stt_ms,
            llm_ms=llm_ms,
            llm_ttft_ms=llm_ttft_ms,
            tts_ms=tts_ms,
            tts_ttfb_ms=tts_ttfb_ms,
            time_to_first_audio_ms=tts_ttfb_ms or llm_ttft_ms,
            interrupted=interrupted,
            confidence=confidence,
        )
        self.call.turns.append(turn)
        for component, value, extra in (
            (LatencyComponent.STT, stt_ms, {}),
            (LatencyComponent.LLM, llm_ms, {"ttft_ms": llm_ttft_ms}),
            (LatencyComponent.TTS, tts_ms, {"ttfb_ms": tts_ttfb_ms}),
            (LatencyComponent.TTFA, turn.time_to_first_audio_ms, {}),
            (LatencyComponent.E2E, turn.time_to_first_audio_ms, {}),
        ):
            if value is None:
                continue
            self.call.latency_samples.append(
                LatencySample(component=component, duration_ms=value, turn_index=turn.index, source="sdk", **extra)
            )
        return turn

    def record_tool(
        self,
        name: str,
        arguments: Any | None = None,
        *,
        duration_ms: float | None = None,
        time_to_tool_ms: float | None = None,
        status: ToolStatus = ToolStatus.SUCCESS,
        error: str | None = None,
        result: Any | None = None,
        tool_id: str | None = None,
    ) -> ToolInvocation:
        stored_args = redact_value(None, arguments or {})
        invocation = ToolInvocation(
            id=tool_id or new_id(),
            name=name,
            duration_ms=duration_ms,
            time_to_tool_ms=time_to_tool_ms,
            status=status,
            payload_shape=payload_shape(arguments or {}),
            argument_hash=sha256_text(canonical_json(stored_args)),
            error=error,
            result_preview=preview_text(result),
            metadata={"arguments": stored_args},
        )
        self.call.tools.append(invocation)
        return invocation

    def snapshot(
        self,
        *,
        hangup_reason: str | None = "completed",
        final: bool = True,
        spans_exported: bool = False,
        traceparent: str | None = None,
        recording_url: str | None = None,
    ) -> dict[str, Any]:
        self.call.ended_at = utcnow()
        if self.call.started_at and self.call.ended_at:
            self.call.duration_ms = (self.call.ended_at - self.call.started_at).total_seconds() * 1000.0
        self.call.transcript_text = "\n".join(f"{t.speaker.value}: {t.text}" for t in self.call.turns if t.text)
        if recording_url:
            self.call.recording_url = recording_url
        payload = NativeSnapshot(
            provider=self.call.provider.value,
            call_id=self.call.provider_call_id,
            agent_id=self.call.agent_id,
            agent_name=self.call.agent_name,
            started_at=self.call.started_at.isoformat() if self.call.started_at else None,
            ended_at=self.call.ended_at.isoformat() if self.call.ended_at else None,
            duration_ms=self.call.duration_ms,
            hangup_reason=hangup_reason,
            final=final,
            transcript_text=self.call.transcript_text,
            grounding={
                "system_prompt": self.call.grounding.system_prompt,
                "knowledge": self.call.grounding.knowledge,
            },
            turns=[t.model_dump(mode="json") for t in self.call.turns],
            tools=[t.model_dump(mode="json") for t in self.call.tools],
            latency_samples=[s.model_dump(mode="json") for s in self.call.latency_samples],
            recording_url=self.call.recording_url,
            spans_exported=spans_exported,
            traceparent=traceparent,
        )
        return payload.model_dump(mode="json")

    def send(self, client: Any, **snapshot_kwargs: Any) -> Any:
        """POST this snapshot through an ``ObsaltClient`` (or anything with ``ingest_native``)."""
        return client.ingest_native(self.snapshot(**snapshot_kwargs))
