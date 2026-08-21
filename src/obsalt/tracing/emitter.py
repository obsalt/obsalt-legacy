from __future__ import annotations

from datetime import timedelta

from obsalt.domain.enums import LatencyComponent, Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, ToolInvocation, Turn
from obsalt.tracing import conventions as c
from obsalt.tracing.evidence import recording_artifact_id, transcript_artifact_id
from obsalt.tracing.metrics import VoiceMetrics
from obsalt.tracing.tracer import VoiceCallTracer, to_unix_ns


def _turn_window(call: CanonicalCall, turn: Turn) -> tuple[int | None, int | None]:
    start = None
    if call.started_at is not None and turn.seconds_from_start is not None:
        start = call.started_at + timedelta(seconds=turn.seconds_from_start)
    elif turn.started_at is not None:
        start = turn.started_at
    end = turn.ended_at
    if end is None and start is not None and turn.duration_ms is not None:
        end = start + timedelta(milliseconds=turn.duration_ms)
    return to_unix_ns(start), to_unix_ns(end)


def _within_call(call: CanonicalCall, ns: int | None) -> int | None:
    if ns is None:
        return None
    start = to_unix_ns(call.started_at)
    end = to_unix_ns(call.ended_at)
    if start is not None and ns < start - 60_000_000_000:
        return None
    if end is not None and ns > end + 60_000_000_000:
        return None
    return ns


def _shift(start_ns: int | None, ms: float | None) -> int | None:
    if start_ns is None or ms is None:
        return None
    return start_ns + int(ms * 1_000_000)


def emit_call_trace(call: CanonicalCall, *, metrics: VoiceMetrics | None = None, environment: str = "prod") -> None:
    """Reconstruct Hamming's span tree from an evidence packet (provider webhook)."""
    metrics = metrics or VoiceMetrics()
    extra = {
        c.EVIDENCE_TRANSCRIPT_ID: transcript_artifact_id(call),
        c.EVIDENCE_RECORDING_ID: recording_artifact_id(call),
        c.EVIDENCE_REDACTION: "redacted",
        "voice.runtime": call.provider.value,
    }
    stt_provider = c.known_provider((call.metadata or {}).get("stt_provider"))
    tts_provider = c.known_provider((call.metadata or {}).get("tts_provider"))
    llm_model = (call.metadata or {}).get("llm_model")
    llm_provider = c.known_provider((call.metadata or {}).get("llm_provider"))

    tracer = VoiceCallTracer.start(
        call_id=call.id,
        workspace_id=call.org_id,
        agent_id=call.agent_id,
        start_ns=to_unix_ns(call.started_at),
        end_ns=to_unix_ns(call.ended_at),
        extra_attributes=extra,
    )
    tools_by_turn = _attach_tools(call)
    with tracer:
        outcome = (
            "error"
            if call.status.value == "error"
            else (call.hangup.reason.value if call.hangup else call.status.value)
        )
        tracer.set_call_outcome(
            duration_ms=call.duration_ms,
            status=call.status.value,
            error_type=call.hangup.reason.value
            if call.hangup and call.hangup.reason.value.startswith("error_")
            else None,
        )
        if call.hangup:
            tracer.span.set_attribute("hangup.reason", call.hangup.reason.value)
            tracer.span.set_attribute("hangup.party", call.hangup.party.value)
        metrics.record_call(agent=call.agent_id, environment=environment, outcome=outcome)

        for turn in call.turns:
            t_start, t_end = _turn_window(call, turn)
            with tracer.turn(turn.index, turn.speaker.value, start_ns=t_start, end_ns=t_end) as turn_span:
                if turn.speaker == Speaker.USER:
                    if turn.stt_ms is not None or turn.confidence is not None:
                        _emit_stt(turn_span, turn, stt_provider, t_start, metrics, call.agent_id, environment)
                    continue
                if turn.speaker == Speaker.AGENT:
                    _emit_agent_turn(
                        turn_span,
                        turn,
                        tools_by_turn.get(turn.index, []),
                        call=call,
                        llm_model=llm_model,
                        llm_provider=llm_provider,
                        tts_provider=tts_provider,
                        t_start=t_start,
                        metrics=metrics,
                        agent=call.agent_id,
                        environment=environment,
                    )

        with tracer.finalize_transcript("written" if call.transcript_text else "empty"):
            pass

        for result in call.evals:
            with tracer.evaluate(result.rubric_id) as ev:
                ev.set(**{c.ASSERTION_RESULT: "pass" if result.passed else "fail", c.ASSERTION_SCORE: result.score})
                if not result.passed:
                    ev.fail("assertion_failed")
                    metrics.record_assertion_failure(
                        agent=call.agent_id, assertion_type=result.rubric_name, environment=environment
                    )
        for flag in call.hallucinations:
            with tracer.evaluate(f"hallucination.{flag.kind.value}") as ev:
                ev.set(**{c.ASSERTION_RESULT: "fail", c.ASSERTION_SCORE: flag.confidence})
                ev.fail("hallucination")
                metrics.record_assertion_failure(
                    agent=call.agent_id, assertion_type="hallucination", environment=environment
                )

        for sample in call.latency_samples:
            stage = {
                LatencyComponent.STT: "stt",
                LatencyComponent.LLM: "llm",
                LatencyComponent.TTS: "tts",
                LatencyComponent.E2E: "e2e",
                LatencyComponent.TTFA: "ttfa",
                LatencyComponent.TOOL: "tool",
            }.get(sample.component)
            if stage:
                metrics.record_stage(stage, sample.duration_ms, agent=call.agent_id, environment=environment)


def _emit_stt(
    turn_span,
    turn: Turn,
    provider: str | None,
    t_start: int | None,
    metrics: VoiceMetrics,
    agent: str,
    environment: str,
) -> None:
    stt_end = _shift(t_start, turn.stt_ms)
    with turn_span.stt(provider, start_ns=t_start, end_ns=stt_end) as stt:
        stt.set(confidence=turn.confidence, latency_ms=turn.stt_ms)
        if provider:
            with stt.provider_attempt(provider, start_ns=t_start, end_ns=stt_end) as attempt:
                attempt.set(**{c.STT_LATENCY_MS: turn.stt_ms, c.STT_CONFIDENCE: turn.confidence})
    if turn.stt_ms is not None:
        metrics.record_stage("stt", turn.stt_ms, agent=agent, environment=environment)
    if turn.confidence is not None and turn.confidence < 0.7:
        metrics.record_low_confidence(agent=agent, stt_provider=provider or "unknown", environment=environment)


def _emit_agent_turn(
    turn_span,
    turn: Turn,
    tools: list[ToolInvocation],
    *,
    call: CanonicalCall,
    llm_model: str | None,
    llm_provider: str | None,
    tts_provider: str | None,
    t_start: int | None,
    metrics: VoiceMetrics,
    agent: str,
    environment: str,
) -> None:
    llm_ms = turn.llm_ms or turn.llm_ttft_ms
    llm_end = _shift(t_start, llm_ms)
    with turn_span.llm(llm_model, llm_provider, start_ns=t_start, end_ns=llm_end) as llm:
        llm.set(ttft_ms=turn.llm_ttft_ms or turn.llm_ms, tokens_in=None, tokens_out=None)
        for tool in tools:
            tool_start = _within_call(call, to_unix_ns(tool.started_at)) or t_start
            tool_end = _within_call(call, to_unix_ns(tool.ended_at)) or _shift(tool_start, tool.duration_ms)
            with llm.tool(tool.name, call_id=tool.id, start_ns=tool_start, end_ns=tool_end) as ts:
                ts.set(execution_ms=tool.duration_ms, retry_count=tool.retry_count)
                if tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
                    ts.fail(tool.status.value)
                    metrics.record_tool_failure(
                        agent=agent, tool_name=tool.name, failure_type=tool.status.value, environment=environment
                    )
                if tool.duration_ms is not None:
                    metrics.record_stage("tool", tool.duration_ms, agent=agent, environment=environment)
    if llm_ms:
        metrics.record_stage("llm", llm_ms, agent=agent, environment=environment)
    tts_end = _shift(t_start, turn.tts_ms)
    if turn.tts_ms is not None or turn.tts_ttfb_ms is not None:
        with turn_span.tts(tts_provider, start_ns=t_start, end_ns=tts_end) as tts:
            tts.set(synthesis_ms=turn.tts_ms, first_audio_ms=turn.tts_ttfb_ms)
        if turn.tts_ms:
            metrics.record_stage("tts", turn.tts_ms, agent=agent, environment=environment)
    if turn.time_to_first_audio_ms is not None:
        metrics.record_stage("ttfa", turn.time_to_first_audio_ms, agent=agent, environment=environment)
        metrics.record_stage("e2e", turn.time_to_first_audio_ms, agent=agent, environment=environment)


def _attach_tools(call: CanonicalCall) -> dict[int, list[ToolInvocation]]:
    """Nest tools under the agent turn that used them — never as call-level orphans."""
    by_turn: dict[int, list[ToolInvocation]] = {}
    agent_turns = [t for t in call.turns if t.speaker == Speaker.AGENT]
    for tool in call.tools:
        idx = tool.turn_index
        if idx is None and agent_turns:
            if tool.started_at:
                later = [t for t in agent_turns if t.started_at and t.started_at >= tool.started_at]
                earlier = [t for t in agent_turns if t.started_at and t.started_at <= tool.started_at]
                if later:
                    idx = later[0].index
                elif earlier:
                    idx = earlier[-1].index
                else:
                    idx = agent_turns[0].index
            else:
                idx = agent_turns[-1].index
        if idx is None:
            idx = agent_turns[-1].index if agent_turns else 0
        by_turn.setdefault(idx, []).append(tool)
    return by_turn
