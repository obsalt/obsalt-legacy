from __future__ import annotations

from typing import Any

from obsalt.adapters.base import AdapterResult, empty_call, first_present, speaker_from, transcript_from_turns
from obsalt.domain.enums import CallDirection, CallStatus, LatencyComponent, Provider, Speaker, ToolStatus
from obsalt.domain.models import Hangup, LatencySample, ToolInvocation, Turn
from obsalt.domain.redact import payload_shape, preview_text
from obsalt.hangup.taxonomy import annotate_hangup, classify_provider_reason
from obsalt.tools.telemetry import parse_arguments
from obsalt.util import as_float, as_str, canonical_json, duration_ms, parse_datetime, sha256_text


def _call_blob(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("call"), dict):
        return payload["call"]
    return payload


class RetellAdapter:
    provider = Provider.RETELL

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        event_type = as_str(payload.get("event")) or "call_ended"
        blob = _call_blob(payload)
        provider_call_id = as_str(blob.get("call_id"))
        if not provider_call_id:
            return None

        agent_id = as_str(blob.get("agent_id")) or "unknown"
        call = empty_call(org_id=org_id, provider=self.provider, provider_call_id=provider_call_id, agent_id=agent_id)
        call.agent_name = as_str(blob.get("agent_name"))
        call.raw_event_type = event_type
        direction = as_str(blob.get("direction"))
        if direction == "inbound":
            call.direction = CallDirection.INBOUND
        elif direction == "outbound":
            call.direction = CallDirection.OUTBOUND
        call.from_number = as_str(blob.get("from_number"))
        call.to_number = as_str(blob.get("to_number"))
        call.started_at = parse_datetime(blob.get("start_timestamp"))
        call.ended_at = parse_datetime(blob.get("end_timestamp"))
        call.duration_ms = as_float(blob.get("duration_ms")) or duration_ms(call.started_at, call.ended_at)
        call.recording_url = as_str(blob.get("recording_url")) or as_str(blob.get("recording_multi_channel_url"))
        call.transcript_text = as_str(blob.get("transcript")) or ""
        call.metadata["retell_status"] = blob.get("call_status")
        cost = blob.get("call_cost") if isinstance(blob.get("call_cost"), dict) else {}
        cents = as_float(cost.get("combined_cost"))
        if cents is not None:
            call.cost_usd = cents / 100.0

        analysis = blob.get("call_analysis") if isinstance(blob.get("call_analysis"), dict) else {}
        if analysis:
            call.metadata["summary"] = analysis.get("call_summary")
            call.metadata["user_sentiment"] = analysis.get("user_sentiment")
            call.metadata["call_successful"] = analysis.get("call_successful")

        dynamic = blob.get("retell_llm_dynamic_variables") if isinstance(blob.get("retell_llm_dynamic_variables"), dict) else {}
        if dynamic:
            call.grounding.knowledge.append("dynamic_variables: " + canonical_json(dynamic))

        woven = blob.get("transcript_with_tool_calls") or blob.get("transcript_object") or []
        call.turns, call.tools = _turns_and_tools(woven if isinstance(woven, list) else [])
        if not call.transcript_text:
            call.transcript_text = transcript_from_turns(call)

        call.latency_samples = _latency_from_retell(blob.get("latency") if isinstance(blob.get("latency"), dict) else {})

        terminal = event_type in {"call_ended", "call_analyzed"} or as_str(blob.get("call_status")) in {"ended", "error"}
        reason_code = as_str(blob.get("disconnection_reason"))
        if reason_code:
            reason, party = classify_provider_reason("retell", reason_code)
            hangup = Hangup(reason=reason, party=party, provider_reason=reason_code, signal=reason_code)
            call.hangup = annotate_hangup(call, hangup)
            call.status = CallStatus.ERROR if reason.value.startswith("error_") else CallStatus.ENDED
        elif terminal:
            call.status = CallStatus.ENDED

        if event_type == "call_started":
            call.status = CallStatus.ONGOING
            terminal = False

        return AdapterResult(call=call, terminal=terminal, event_type=event_type)


def _turns_and_tools(items: list[Any]) -> tuple[list[Turn], list[ToolInvocation]]:
    turns: list[Turn] = []
    tools: list[ToolInvocation] = []
    pending: dict[str, ToolInvocation] = {}
    index = 0
    last_user_ms: float | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("role")) or ""
        if role == "tool_call_invocation":
            tool_id = as_str(item.get("tool_call_id")) or as_str(item.get("name")) or "tool"
            name = as_str(item.get("name")) or "unknown"
            args = parse_arguments(item.get("arguments"))
            started = parse_datetime(item.get("created_timestamp") or item.get("timestamp"))
            tool = ToolInvocation(
                id=tool_id,
                name=name,
                started_at=started,
                status=ToolStatus.PENDING,
                payload_shape=payload_shape(args),
                argument_hash=sha256_text(canonical_json(args)),
                metadata={"arguments": args},
                time_to_tool_ms=(None if last_user_ms is None or started is None else None),
            )
            if last_user_ms is not None and item.get("words"):
                pass
            pending[tool_id] = tool
            tools.append(tool)
            continue
        if role == "tool_call_result":
            tool_id = as_str(item.get("tool_call_id")) or ""
            tool = pending.get(tool_id)
            if tool is None:
                tool = ToolInvocation(id=tool_id or "tool", name="unknown")
                tools.append(tool)
            success = item.get("successful")
            if success is False:
                tool.status = ToolStatus.ERROR
                tool.error = preview_text(item.get("content"))
            else:
                tool.status = ToolStatus.SUCCESS
            tool.result_preview = preview_text(item.get("content"))
            tool.ended_at = parse_datetime(item.get("created_timestamp") or item.get("timestamp"))
            if tool.started_at and tool.ended_at:
                tool.duration_ms = duration_ms(tool.started_at, tool.ended_at)
            continue

        speaker = speaker_from(role)
        words = item.get("words") if isinstance(item.get("words"), list) else []
        start_ms = None
        end_ms = None
        if words:
            start_ms = as_float(words[0].get("start")) if isinstance(words[0], dict) else None
            end_ms = as_float(words[-1].get("end")) if isinstance(words[-1], dict) else None
        start_ms = first_present(start_ms, as_float(item.get("start_timestamp")), as_float(item.get("words_timestamp")))
        text = as_str(item.get("content")) or ""
        if not text and words:
            text = " ".join(as_str(w.get("word")) or "" for w in words if isinstance(w, dict)).strip()
        duration = None
        if start_ms is not None and end_ms is not None:
            duration = max(0.0, end_ms - start_ms)
        turn = Turn(
            index=index,
            speaker=speaker,
            text=text,
            seconds_from_start=(start_ms / 1000.0) if start_ms is not None else None,
            duration_ms=duration,
        )
        if speaker == Speaker.USER and start_ms is not None:
            last_user_ms = start_ms
        turns.append(turn)
        index += 1
    return turns, tools


def _latency_from_retell(latency: dict[str, Any]) -> list[LatencySample]:
    samples: list[LatencySample] = []
    mapping = {
        "e2e": LatencyComponent.E2E,
        "asr": LatencyComponent.STT,
        "llm": LatencyComponent.LLM,
        "tts": LatencyComponent.TTS,
        "s2s": LatencyComponent.S2S,
        "knowledge_base": LatencyComponent.KNOWLEDGE_BASE,
    }
    for key, component in mapping.items():
        block = latency.get(key)
        if not isinstance(block, dict):
            continue
        values = block.get("values")
        if isinstance(values, list) and values:
            for i, value in enumerate(values):
                ms = as_float(value)
                if ms is None:
                    continue
                extra = {}
                if component == LatencyComponent.LLM:
                    extra["ttft_ms"] = ms
                if component == LatencyComponent.TTS:
                    extra["ttfb_ms"] = ms
                samples.append(LatencySample(component=component, duration_ms=ms, turn_index=i, source="provider", **extra))
            if component == LatencyComponent.E2E:
                for i, value in enumerate(values):
                    ms = as_float(value)
                    if ms is not None:
                        samples.append(LatencySample(component=LatencyComponent.TTFA, duration_ms=ms, turn_index=i, source="provider"))
            continue
        p50 = as_float(block.get("p50"))
        if p50 is not None:
            extra = {}
            if component == LatencyComponent.LLM:
                extra["ttft_ms"] = p50
            samples.append(LatencySample(component=component, duration_ms=p50, source="provider_p50", **extra))
            if component == LatencyComponent.E2E:
                samples.append(LatencySample(component=LatencyComponent.TTFA, duration_ms=p50, source="provider_p50"))
            num = int(as_float(block.get("num")) or 1)
            p95 = as_float(block.get("p95"))
            if p95 is not None and num > 1:
                samples.append(LatencySample(component=component, duration_ms=p95, source="provider_p95"))
    return samples
