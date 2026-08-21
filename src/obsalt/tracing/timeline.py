"""Join the span tree to evidence without putting PII on spans.

The OTLP export (Tempo) and this view share span names and join keys. Transcripts,
prompts, tool payloads, and recordings stay in the evidence pane / store.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from obsalt.domain.enums import LatencyComponent, Provider, Speaker
from obsalt.domain.models import CanonicalCall, Turn
from obsalt.tracing import conventions as c
from obsalt.tracing.evidence import recording_artifact_id, transcript_artifact_id
from obsalt.tracing.layout import (
    hosted_provider,
    join_from_call,
    reviewer_safe_attrs,
    stt_attempts_for,
    tool_failed,
    tools_by_turn,
)
from obsalt.tracing.tracer import to_unix_ns

GapKind = Literal["structural", "missing"]
SpanStatus = Literal["ok", "error"]


class CoverageGap(BaseModel):
    id: str
    signal: str
    kind: GapKind
    reason: str


class Coverage(BaseModel):
    """Honest inventory of what this call can show.

    Structural gaps are path limits (a Vapi webhook will never include a
    Deepgram→Azure fallback hop). Missing gaps are absent on this call but
    representable on the path (no recording_url, no STT timings).
    """

    signals: dict[str, bool]
    gaps: list[CoverageGap] = Field(default_factory=list)
    completeness: float = 0.0


class TimelineSpan(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    start_ms: float | None = None
    duration_ms: float | None = None
    status: SpanStatus = "ok"
    error_type: str | None = None
    turn_index: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class CallView(BaseModel):
    join: dict[str, Any]
    trace: dict[str, Any]
    evidence: dict[str, Any]
    coverage: Coverage
    links: dict[str, str]


def build_call_view(call: CanonicalCall) -> CallView:
    join = join_from_call(call)
    extra = reviewer_safe_attrs(
        {
            c.EVIDENCE_TRANSCRIPT_ID: transcript_artifact_id(call),
            c.EVIDENCE_RECORDING_ID: recording_artifact_id(call),
            c.EVIDENCE_REDACTION: "redacted",
            "voice.runtime": call.provider.value,
            c.CALL_DURATION_MS: int(call.duration_ms) if call.duration_ms is not None else None,
            c.CALL_STATUS: call.status.value,
        }
    )
    spans = _span_tree(call, {**join, **extra})
    coverage = assess_coverage(call, spans)
    live = bool(call.metadata.get("spans_exported"))
    return CallView(
        join=join,
        trace={
            "source": "live" if live else "reconstructed",
            "root": c.SPAN_CALL,
            "spans": [s.model_dump() for s in spans],
        },
        evidence=_evidence_packet(call),
        coverage=coverage,
        links={
            "call": f"/v1/calls/{call.id}",
            "view": f"/v1/calls/{call.id}/view",
            "ui": f"/v1/calls/{call.id}/ui",
        },
    )


def assess_coverage(call: CanonicalCall, spans: list[TimelineSpan] | None = None) -> Coverage:
    spans = spans if spans is not None else _span_tree(call, join_from_call(call))
    names = {s.name for s in spans}
    attempts = [a for turn in call.turns for a in stt_attempts_for(turn)]
    has_fallback = any(a.get("fallback") for a in attempts) or any("fallback" in n for n in names)
    has_stt = any(
        turn.stt_ms is not None or turn.confidence is not None or stt_attempts_for(turn)
        for turn in call.turns
    ) or c.SPAN_STT in names
    has_llm = any(turn.llm_ms is not None or turn.llm_ttft_ms is not None for turn in call.turns) or c.SPAN_LLM in names
    has_tts = any(turn.tts_ms is not None or turn.tts_ttfb_ms is not None for turn in call.turns) or c.SPAN_TTS in names
    has_vad = any(turn.metadata.get("vad.end_of_utterance_ms") is not None for turn in call.turns) or c.SPAN_VAD in names
    has_playout = (
        any(turn.metadata.get("audio.playout_ms") is not None for turn in call.turns) or c.SPAN_PLAYOUT in names
    )
    transcript = bool(call.transcript_text or any(t.text for t in call.turns))
    recording = bool(call.recording_url)
    live = bool(call.metadata.get("spans_exported"))
    signals = {
        "waterfall": bool(spans),
        "transcript": transcript,
        "recording": recording,
        "stt": has_stt,
        "stt_fallback": has_fallback,
        "llm": has_llm,
        "tts": has_tts,
        "tools": bool(call.tools),
        "evals": bool(call.evals or call.hallucinations),
        "hangup": call.hangup is not None,
        "live_spans": live,
        "vad": has_vad,
        "playout": has_playout,
        "traceparent": bool(call.metadata.get("traceparent")),
    }
    gaps: list[CoverageGap] = []
    hosted = hosted_provider(call)

    if not transcript:
        gaps.append(
            CoverageGap(
                id="transcript",
                signal="transcript",
                kind="missing",
                reason="No transcript text on this call. Path B: pass text= to VoiceCall.turn() and POST a snapshot.",
            )
        )
    if not recording:
        gaps.append(
            CoverageGap(
                id="recording",
                signal="recording",
                kind="missing",
                reason="No recording_url. The waterfall will not have audio playback until the vendor or snapshot includes one.",
            )
        )
    if not has_fallback:
        if hosted:
            gaps.append(
                CoverageGap(
                    id="stt_fallback",
                    signal="stt_fallback",
                    kind="structural",
                    reason=(
                        "Hosted platforms do not send STT provider fallback hops. "
                        "obsalt will not invent Deepgram→Azure children from a single vendor hop."
                    ),
                )
            )
        elif call.provider != Provider.NATIVE or not live:
            gaps.append(
                CoverageGap(
                    id="stt_fallback",
                    signal="stt_fallback",
                    kind="missing",
                    reason="No stt.provider.fallback.* hop recorded. Use stt.provider_attempt(..., fallback=True) when a vendor times out.",
                )
            )
        else:
            gaps.append(
                CoverageGap(
                    id="stt_fallback",
                    signal="stt_fallback",
                    kind="missing",
                    reason="This call has a single STT hop. Fallback children appear only when a second provider_attempt is recorded.",
                )
            )
    if hosted and not live:
        gaps.append(
            CoverageGap(
                id="live_spans",
                signal="live_spans",
                kind="structural",
                reason=(
                    "Path A reconstructs call.lifecycle after the terminal webhook. "
                    "There is no live waterfall during the vendor-owned call."
                ),
            )
        )
    elif not live:
        gaps.append(
            CoverageGap(
                id="live_spans",
                signal="live_spans",
                kind="missing",
                reason="spans_exported is false. Tempo has a reconstructed tree (if OTLP is configured), not in-process live spans.",
            )
        )
    if not has_stt and any(t.speaker == Speaker.USER and t.text for t in call.turns):
        gaps.append(
            CoverageGap(
                id="stt",
                signal="stt",
                kind="missing",
                reason="User text is stored as evidence but STT timings/confidence were not supplied.",
            )
        )
    if not has_vad:
        gaps.append(
            CoverageGap(
                id="vad",
                signal="vad",
                kind="missing" if not hosted else "structural",
                reason="No vad.end_of_utterance timing. Hosted webhooks rarely include endpointing; Path B should call turn.vad().",
            )
        )
    if not has_playout:
        gaps.append(
            CoverageGap(
                id="playout",
                signal="playout",
                kind="missing" if not hosted else "structural",
                reason="No audio.playout span. Dead air after TTS is a separate hop from tts.synthesis — do not copy TTFA onto playout.",
            )
        )
    return Coverage(
        signals=signals,
        gaps=gaps,
        completeness=round(sum(1 for v in signals.values() if v) / len(signals), 4) if signals else 0.0,
    )


def coverage_summary(call: CanonicalCall) -> dict[str, Any]:
    coverage = assess_coverage(call)
    return {
        "signals": coverage.signals,
        "gaps": [g.id for g in coverage.gaps],
        "completeness": coverage.completeness,
        "view_path": f"/v1/calls/{call.id}/ui",
    }


def _span_tree(call: CanonicalCall, base_attrs: dict[str, Any]) -> list[TimelineSpan]:
    spans: list[TimelineSpan] = []
    call_start_ns = to_unix_ns(call.started_at)
    root_end = None
    if call.duration_ms is not None:
        root_end = call.duration_ms
    elif call.started_at and call.ended_at:
        root_end = (call.ended_at - call.started_at).total_seconds() * 1000.0
    error_type = None
    status: SpanStatus = "ok"
    if call.status.value == "error":
        status = "error"
        error_type = call.hangup.reason.value if call.hangup else "error"
    root_attrs = dict(base_attrs)
    if call.hangup:
        root_attrs["hangup.reason"] = call.hangup.reason.value
        root_attrs["hangup.party"] = call.hangup.party.value
    spans.append(
        TimelineSpan(
            id="call",
            name=c.SPAN_CALL,
            start_ms=0.0,
            duration_ms=root_end,
            status=status,
            error_type=error_type,
            attributes=reviewer_safe_attrs(root_attrs),
        )
    )

    cursor = 0.0
    attached = tools_by_turn(call)
    stt_provider = c.known_provider((call.metadata or {}).get("stt_provider"))
    tts_provider = c.known_provider((call.metadata or {}).get("tts_provider"))
    llm_model = (call.metadata or {}).get("llm_model")
    llm_provider = c.known_provider((call.metadata or {}).get("llm_provider"))

    for turn in call.turns:
        turn_start = _turn_start_ms(turn, call_start_ns, cursor)
        turn_dur = turn.duration_ms
        if turn_dur is None:
            pieces = [turn.stt_ms, turn.llm_ms or turn.llm_ttft_ms, turn.tts_ms, turn.time_to_first_audio_ms]
            known = [p for p in pieces if p is not None]
            turn_dur = sum(known) if known else 0.0
        turn_id = f"turn.{turn.index}"
        turn_attrs = reviewer_safe_attrs({**base_attrs, c.TURN_INDEX: turn.index, c.TURN_SPEAKER: turn.speaker.value})
        spans.append(
            TimelineSpan(
                id=turn_id,
                name=c.turn_span_name(turn.index),
                parent_id="call",
                start_ms=turn_start,
                duration_ms=turn_dur,
                turn_index=turn.index,
                attributes=turn_attrs,
            )
        )
        offset = turn_start or 0.0
        if turn.speaker == Speaker.USER:
            offset = _emit_stt_branch(
                spans,
                turn,
                parent_id=turn_id,
                base_attrs=base_attrs,
                stt_provider=stt_provider,
                start_ms=offset,
            )
        elif turn.speaker == Speaker.AGENT:
            offset = _emit_agent_branch(
                spans,
                turn,
                tools=attached.get(turn.index, []),
                parent_id=turn_id,
                base_attrs=base_attrs,
                llm_model=llm_model,
                llm_provider=llm_provider,
                tts_provider=tts_provider,
                start_ms=offset,
            )
        cursor = (turn_start or cursor) + (turn_dur or 0.0)

    transcript_status = "written" if call.transcript_text else "empty"
    spans.append(
        TimelineSpan(
            id="transcript.finalization",
            name=c.SPAN_TRANSCRIPT_FINAL,
            parent_id="call",
            start_ms=cursor,
            duration_ms=0.0,
            status="error" if transcript_status == "empty" else "ok",
            error_type="empty_transcript" if transcript_status == "empty" else None,
            attributes=reviewer_safe_attrs({**base_attrs, c.TRANSCRIPT_FINAL_STATUS: transcript_status}),
        )
    )
    for result in call.evals:
        sid = f"eval.{result.rubric_id}"
        spans.append(
            TimelineSpan(
                id=sid,
                name=c.SPAN_EVAL,
                parent_id="call",
                start_ms=cursor,
                duration_ms=0.0,
                status="ok" if result.passed else "error",
                error_type=None if result.passed else "assertion_failed",
                attributes=reviewer_safe_attrs(
                    {
                        **base_attrs,
                        c.ASSERTION_ID: result.rubric_id,
                        c.ASSERTION_RESULT: "pass" if result.passed else "fail",
                        c.ASSERTION_SCORE: result.score,
                    }
                ),
            )
        )
    for flag in call.hallucinations:
        sid = f"eval.hallucination.{flag.kind.value}.{flag.turn_index}"
        spans.append(
            TimelineSpan(
                id=sid,
                name=c.SPAN_EVAL,
                parent_id="call",
                start_ms=cursor,
                duration_ms=0.0,
                status="error",
                error_type="hallucination",
                turn_index=flag.turn_index,
                attributes=reviewer_safe_attrs(
                    {
                        **base_attrs,
                        c.ASSERTION_ID: f"hallucination.{flag.kind.value}",
                        c.ASSERTION_RESULT: "fail",
                        c.ASSERTION_SCORE: flag.confidence,
                    }
                ),
            )
        )
    return spans


def _turn_start_ms(turn: Turn, call_start_ns: int | None, cursor: float) -> float:
    if turn.seconds_from_start is not None:
        return float(turn.seconds_from_start) * 1000.0
    turn_ns = to_unix_ns(turn.started_at)
    if turn_ns is not None and call_start_ns is not None:
        return max(0.0, (turn_ns - call_start_ns) / 1_000_000.0)
    return cursor


def _emit_stt_branch(
    spans: list[TimelineSpan],
    turn: Turn,
    *,
    parent_id: str,
    base_attrs: dict[str, Any],
    stt_provider: str | None,
    start_ms: float,
) -> float:
    vad_ms = turn.metadata.get("vad.end_of_utterance_ms")
    if vad_ms is not None:
        spans.append(
            TimelineSpan(
                id=f"{parent_id}/vad",
                name=c.SPAN_VAD,
                parent_id=parent_id,
                start_ms=start_ms,
                duration_ms=float(vad_ms),
                turn_index=turn.index,
                attributes=reviewer_safe_attrs({**base_attrs, c.TURN_INDEX: turn.index, c.VAD_EOU_MS: vad_ms}),
            )
        )
    attempts = stt_attempts_for(turn)
    if turn.stt_ms is None and turn.confidence is None and not attempts:
        return start_ms
    provider = stt_provider
    if attempts:
        provider = attempts[-1].get("provider") or provider
    stt_id = f"{parent_id}/stt"
    stt_ms = turn.stt_ms
    if stt_ms is None and attempts:
        stt_ms = sum(float(a["latency_ms"]) for a in attempts if a.get("latency_ms") is not None)
    spans.append(
        TimelineSpan(
            id=stt_id,
            name=c.SPAN_STT,
            parent_id=parent_id,
            start_ms=start_ms,
            duration_ms=stt_ms,
            turn_index=turn.index,
            attributes=reviewer_safe_attrs(
                {
                    **base_attrs,
                    c.TURN_INDEX: turn.index,
                    c.STT_PROVIDER: provider,
                    c.STT_CONFIDENCE: turn.confidence,
                    c.STT_LATENCY_MS: turn.stt_ms,
                }
            ),
        )
    )
    hop_start = start_ms
    if attempts:
        for i, attempt in enumerate(attempts):
            name = c.stt_provider_span_name(str(attempt["provider"]), fallback=bool(attempt.get("fallback")))
            hop_ms = attempt.get("latency_ms")
            error = attempt.get("error")
            spans.append(
                TimelineSpan(
                    id=f"{stt_id}/{i}-{attempt['provider']}",
                    name=name,
                    parent_id=stt_id,
                    start_ms=hop_start,
                    duration_ms=float(hop_ms) if hop_ms is not None else None,
                    status="error" if error else "ok",
                    error_type=str(error) if error else None,
                    turn_index=turn.index,
                    attributes=reviewer_safe_attrs(
                        {
                            **base_attrs,
                            c.TURN_INDEX: turn.index,
                            c.STT_PROVIDER: attempt["provider"],
                            c.STT_LATENCY_MS: hop_ms,
                            c.STT_CONFIDENCE: attempt.get("confidence"),
                        }
                    ),
                )
            )
            if hop_ms is not None:
                hop_start += float(hop_ms)
    elif provider:
        spans.append(
            TimelineSpan(
                id=f"{stt_id}/{provider}",
                name=c.stt_provider_span_name(provider),
                parent_id=stt_id,
                start_ms=start_ms,
                duration_ms=stt_ms,
                turn_index=turn.index,
                attributes=reviewer_safe_attrs(
                    {
                        **base_attrs,
                        c.TURN_INDEX: turn.index,
                        c.STT_PROVIDER: provider,
                        c.STT_LATENCY_MS: turn.stt_ms,
                        c.STT_CONFIDENCE: turn.confidence,
                    }
                ),
            )
        )
    return start_ms + (stt_ms or 0.0)


def _emit_agent_branch(
    spans: list[TimelineSpan],
    turn: Turn,
    *,
    tools: list,
    parent_id: str,
    base_attrs: dict[str, Any],
    llm_model: str | None,
    llm_provider: str | None,
    tts_provider: str | None,
    start_ms: float,
) -> float:
    llm_ms = turn.llm_ms or turn.llm_ttft_ms
    llm_id = f"{parent_id}/llm"
    spans.append(
        TimelineSpan(
            id=llm_id,
            name=c.SPAN_LLM,
            parent_id=parent_id,
            start_ms=start_ms,
            duration_ms=llm_ms,
            turn_index=turn.index,
            attributes=reviewer_safe_attrs(
                {
                    **base_attrs,
                    c.TURN_INDEX: turn.index,
                    c.GENAI_OPERATION: "chat",
                    c.GENAI_REQUEST_MODEL: llm_model,
                    c.LLM_MODEL: llm_model,
                    c.GENAI_PROVIDER: llm_provider,
                    c.LLM_TTFT_MS: turn.llm_ttft_ms or turn.llm_ms,
                }
            ),
        )
    )
    tool_cursor = start_ms
    for tool in tools:
        failed = tool_failed(tool)
        spans.append(
            TimelineSpan(
                id=f"{llm_id}/tool.{tool.id}",
                name=c.tool_span_name(tool.name),
                parent_id=llm_id,
                start_ms=tool_cursor,
                duration_ms=tool.duration_ms,
                status="error" if failed else "ok",
                error_type=tool.status.value if failed else None,
                turn_index=turn.index,
                attributes=reviewer_safe_attrs(
                    {
                        **base_attrs,
                        c.TURN_INDEX: turn.index,
                        c.GENAI_OPERATION: "execute_tool",
                        c.GENAI_TOOL_NAME: tool.name,
                        c.TOOL_NAME: tool.name,
                        c.GENAI_TOOL_CALL_ID: tool.id,
                        c.TOOL_EXECUTION_MS: tool.duration_ms,
                        c.TOOL_RETRY_COUNT: tool.retry_count,
                    }
                ),
            )
        )
        if tool.duration_ms:
            tool_cursor += tool.duration_ms
    cursor = start_ms + (llm_ms or 0.0)
    if turn.tts_ms is not None or turn.tts_ttfb_ms is not None:
        tts_ms = turn.tts_ms
        spans.append(
            TimelineSpan(
                id=f"{parent_id}/tts",
                name=c.SPAN_TTS,
                parent_id=parent_id,
                start_ms=cursor,
                duration_ms=tts_ms,
                turn_index=turn.index,
                attributes=reviewer_safe_attrs(
                    {
                        **base_attrs,
                        c.TURN_INDEX: turn.index,
                        c.TTS_PROVIDER: tts_provider,
                        c.TTS_SYNTHESIS_MS: turn.tts_ms,
                        c.TTS_FIRST_AUDIO_MS: turn.tts_ttfb_ms,
                    }
                ),
            )
        )
        cursor += tts_ms or 0.0
    playout_ms = turn.metadata.get("audio.playout_ms")
    if playout_ms is not None:
        spans.append(
            TimelineSpan(
                id=f"{parent_id}/playout",
                name=c.SPAN_PLAYOUT,
                parent_id=parent_id,
                start_ms=cursor,
                duration_ms=float(playout_ms),
                turn_index=turn.index,
                attributes=reviewer_safe_attrs(
                    {**base_attrs, c.TURN_INDEX: turn.index, c.AUDIO_PLAYOUT_MS: playout_ms}
                ),
            )
        )
    return cursor


def _evidence_packet(call: CanonicalCall) -> dict[str, Any]:
    latency = {}
    for sample in call.latency_samples:
        key = sample.component.value if isinstance(sample.component, LatencyComponent) else str(sample.component)
        latency.setdefault(key, []).append(
            {"duration_ms": sample.duration_ms, "turn_index": sample.turn_index, "source": sample.source}
        )
    return {
        "transcript_text": call.transcript_text,
        "recording_url": call.recording_url,
        "turns": [
            {
                "index": t.index,
                "speaker": t.speaker.value,
                "text": t.text,
                "interrupted": t.interrupted,
                "confidence": t.confidence,
                "stt_ms": t.stt_ms,
                "llm_ms": t.llm_ms,
                "llm_ttft_ms": t.llm_ttft_ms,
                "tts_ms": t.tts_ms,
                "tts_ttfb_ms": t.tts_ttfb_ms,
                "time_to_first_audio_ms": t.time_to_first_audio_ms,
            }
            for t in call.turns
        ],
        "tools": [
            {
                "id": tool.id,
                "name": tool.name,
                "status": tool.status.value,
                "duration_ms": tool.duration_ms,
                "retry_count": tool.retry_count,
                "payload_shape": tool.payload_shape,
                "result_preview": tool.result_preview,
                "turn_index": tool.turn_index,
                "error": tool.error,
            }
            for tool in call.tools
        ],
        "hangup": (
            {
                "reason": call.hangup.reason.value,
                "party": call.hangup.party.value,
                "loss_score": call.hangup.loss_score,
                "loss_reasons": call.hangup.loss_reasons,
                "last_user_text": call.hangup.last_user_text,
                "last_agent_text": call.hangup.last_agent_text,
            }
            if call.hangup
            else None
        ),
        "evals": [e.model_dump(mode="json") for e in call.evals],
        "hallucinations": [h.model_dump(mode="json") for h in call.hallucinations],
        "grounding": {
            "system_prompt": call.grounding.system_prompt,
            "knowledge": call.grounding.knowledge,
            "tool_results": call.grounding.tool_results,
        },
        "latency": latency,
        "provider": call.provider.value,
        "agent_id": call.agent_id,
        "duration_ms": call.duration_ms,
        "finalized": call.finalized,
    }
