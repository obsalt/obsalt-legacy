from __future__ import annotations

from typing import Any

from obsalt.adapters.base import AdapterResult, empty_call, transcript_from_turns
from obsalt.domain.enums import (
    CallDirection,
    CallStatus,
    LatencyComponent,
    Provider,
    ToolStatus,
    parse_provider,
    speaker_from,
)
from obsalt.domain.models import Hangup, LatencySample, ToolInvocation, Turn
from obsalt.hangup.taxonomy import annotate_hangup, classify_provider_reason
from obsalt.util import as_float, as_str, duration_ms, parse_datetime


class NativeAdapter:
    """obsalt's own event schema — used by the Python SDK and sidecars."""

    provider = Provider.NATIVE

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        provider_call_id = as_str(payload.get("call_id") or payload.get("provider_call_id"))
        if not provider_call_id:
            return None
        raw_provider = as_str(payload.get("provider")) or Provider.NATIVE.value
        try:
            provider = parse_provider(raw_provider)
        except ValueError:
            provider = Provider.NATIVE
        # Native snapshots may wrap another provider's id.
        agent_id = as_str(payload.get("agent_id")) or "unknown"
        call = empty_call(
            org_id=org_id,
            provider=provider,
            provider_call_id=provider_call_id,
            agent_id=agent_id,
        )
        call.agent_name = as_str(payload.get("agent_name"))
        direction = as_str(payload.get("direction"))
        if direction in CallDirection._value2member_map_:
            call.direction = CallDirection(direction)
        call.started_at = parse_datetime(payload.get("started_at"))
        call.ended_at = parse_datetime(payload.get("ended_at"))
        call.duration_ms = as_float(payload.get("duration_ms")) or duration_ms(
            call.started_at, call.ended_at
        )
        call.from_number = as_str(payload.get("from_number"))
        call.to_number = as_str(payload.get("to_number"))
        call.recording_url = as_str(payload.get("recording_url"))
        call.raw_event_type = as_str(payload.get("event_type")) or "native"
        if isinstance(payload.get("grounding"), dict):
            g = payload["grounding"]
            call.grounding.system_prompt = as_str(g.get("system_prompt")) or ""
            call.grounding.knowledge = list(g.get("knowledge") or [])
        for i, raw in enumerate(payload.get("turns") or []):
            if not isinstance(raw, dict):
                continue
            call.turns.append(
                Turn(
                    index=int(raw.get("index", i)),
                    speaker=speaker_from(as_str(raw.get("speaker"))),
                    text=as_str(raw.get("text")) or "",
                    seconds_from_start=as_float(raw.get("seconds_from_start")),
                    duration_ms=as_float(raw.get("duration_ms")),
                    stt_ms=as_float(raw.get("stt_ms")),
                    llm_ms=as_float(raw.get("llm_ms")),
                    llm_ttft_ms=as_float(raw.get("llm_ttft_ms")),
                    tts_ms=as_float(raw.get("tts_ms")),
                    tts_ttfb_ms=as_float(raw.get("tts_ttfb_ms")),
                    time_to_first_audio_ms=as_float(raw.get("time_to_first_audio_ms")),
                    interrupted=bool(raw.get("interrupted")),
                    started_at=parse_datetime(raw.get("started_at")),
                    ended_at=parse_datetime(raw.get("ended_at")),
                )
            )
        for raw in payload.get("tools") or []:
            if not isinstance(raw, dict):
                continue
            status = as_str(raw.get("status")) or "pending"
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            args = raw.get("arguments")
            if args is None:
                args = meta.get("arguments")
            stored_meta = dict(meta)
            if args is not None:
                stored_meta["arguments"] = args
            call.tools.append(
                ToolInvocation(
                    id=as_str(raw.get("id")) or as_str(raw.get("name")) or "tool",
                    name=as_str(raw.get("name")) or "unknown",
                    duration_ms=as_float(raw.get("duration_ms")),
                    time_to_tool_ms=as_float(raw.get("time_to_tool_ms")),
                    status=(
                        ToolStatus(status)
                        if status in ToolStatus._value2member_map_
                        else ToolStatus.PENDING
                    ),
                    retry_count=int(raw.get("retry_count") or 0),
                    payload_shape=raw.get("payload_shape"),
                    argument_hash=as_str(raw.get("argument_hash")),
                    error=as_str(raw.get("error")),
                    result_preview=as_str(raw.get("result_preview")),
                    metadata=stored_meta,
                    started_at=parse_datetime(raw.get("started_at")),
                    ended_at=parse_datetime(raw.get("ended_at")),
                )
            )
        for raw in payload.get("latency_samples") or []:
            if not isinstance(raw, dict):
                continue
            component = as_str(raw.get("component"))
            duration = as_float(raw.get("duration_ms"))
            if component in LatencyComponent._value2member_map_ and duration is not None:
                call.latency_samples.append(
                    LatencySample(
                        component=LatencyComponent(component),
                        duration_ms=duration,
                        turn_index=raw.get("turn_index"),
                        ttft_ms=as_float(raw.get("ttft_ms")),
                        ttfb_ms=as_float(raw.get("ttfb_ms")),
                        source=as_str(raw.get("source")) or "sdk",
                    )
                )
        call.transcript_text = as_str(payload.get("transcript_text")) or transcript_from_turns(call)
        hangup_reason = as_str(payload.get("hangup_reason"))
        terminal = bool(payload.get("final", True))
        if hangup_reason:
            reason, party = classify_provider_reason("native", hangup_reason)
            call.hangup = annotate_hangup(
                call,
                Hangup(
                    reason=reason,
                    party=party,
                    provider_reason=hangup_reason,
                    signal=hangup_reason,
                ),
            )
            call.status = CallStatus.ENDED
        elif terminal:
            call.status = CallStatus.ENDED
        return AdapterResult(
            call=call, terminal=terminal, event_type=call.raw_event_type or "native"
        )
