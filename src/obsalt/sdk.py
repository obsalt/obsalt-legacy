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
from obsalt.domain.models import CanonicalCall, LatencySample, ToolInvocation, Turn
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        elapsed = self.timer.stop()
        t_ms = self._tracer.now_ms()
        turn = Turn(
            index=len(self._tracer.call.turns),
            speaker=self.speaker,
            text=self.text,
            duration_ms=elapsed,
            seconds_from_start=max(0.0, (t_ms - elapsed) / 1000.0),
            stt_ms=self.stt_ms,
            llm_ms=self.llm_ms,
            llm_ttft_ms=self.llm_ttft_ms,
            tts_ms=self.tts_ms,
            tts_ttfb_ms=self.tts_ttfb_ms,
            time_to_first_audio_ms=self.tts_ttfb_ms or self.llm_ttft_ms,
            interrupted=self.interrupted,
        )
        self._tracer.call.turns.append(turn)
        for component, value, extra in (
            (LatencyComponent.STT, self.stt_ms, {}),
            (LatencyComponent.LLM, self.llm_ms, {"ttft_ms": self.llm_ttft_ms}),
            (LatencyComponent.TTS, self.tts_ms, {"ttfb_ms": self.tts_ttfb_ms}),
            (LatencyComponent.TTFA, turn.time_to_first_audio_ms, {}),
            (LatencyComponent.E2E, turn.time_to_first_audio_ms, {}),
        ):
            if value is None:
                continue
            self._tracer.call.latency_samples.append(
                LatencySample(component=component, duration_ms=value, turn_index=turn.index, source="sdk", **extra)
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
        stored_args = redact_value(None, self.arguments)
        self._tracer.call.tools.append(
            ToolInvocation(
                id=self.id,
                name=self.name,
                duration_ms=elapsed,
                time_to_tool_ms=ttt,
                status=self.status,
                payload_shape=payload_shape(self.arguments),
                argument_hash=sha256_text(canonical_json(stored_args)),
                error=self.error,
                result_preview=preview_text(self.result),
                metadata={"arguments": stored_args},
            )
        )


class CallRecorder:
    """Build a call snapshot and POST it to ``/v1/ingest/native``.

    Use this when you do not want to emit OpenTelemetry from the agent process.
    The ingest server stores the snapshot as evidence and reconstructs traces.

    To emit spans in-process instead, use ``obsalt.tracing.VoiceCallTracer``.
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

    def snapshot(self, *, hangup_reason: str | None = "completed", final: bool = True) -> dict[str, Any]:
        self.call.ended_at = utcnow()
        if self.call.started_at and self.call.ended_at:
            self.call.duration_ms = (self.call.ended_at - self.call.started_at).total_seconds() * 1000.0
        self.call.transcript_text = "\n".join(f"{t.speaker.value}: {t.text}" for t in self.call.turns if t.text)
        payload = {
            "provider": self.call.provider.value,
            "call_id": self.call.provider_call_id,
            "agent_id": self.call.agent_id,
            "agent_name": self.call.agent_name,
            "started_at": self.call.started_at.isoformat() if self.call.started_at else None,
            "ended_at": self.call.ended_at.isoformat() if self.call.ended_at else None,
            "duration_ms": self.call.duration_ms,
            "hangup_reason": hangup_reason,
            "final": final,
            "transcript_text": self.call.transcript_text,
            "grounding": {"system_prompt": self.call.grounding.system_prompt, "knowledge": self.call.grounding.knowledge},
            "turns": [t.model_dump(mode="json") for t in self.call.turns],
            "tools": [t.model_dump(mode="json") for t in self.call.tools],
            "latency_samples": [s.model_dump(mode="json") for s in self.call.latency_samples],
        }
        return payload

    def send(self, client: Any, **snapshot_kwargs: Any) -> Any:
        """POST this snapshot through an ``ObsaltClient`` (or anything with ``ingest_native``)."""
        return client.ingest_native(self.snapshot(**snapshot_kwargs))
