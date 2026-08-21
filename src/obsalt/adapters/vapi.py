from __future__ import annotations

from typing import Any

from obsalt.adapters.base import AdapterResult, empty_call, first_present, speaker_from, text_of, transcript_from_turns
from obsalt.domain.enums import (
    CallDirection,
    CallStatus,
    LatencyComponent,
    Provider,
    Speaker,
    ToolStatus,
)
from obsalt.domain.models import Hangup, LatencySample, ToolInvocation, Turn
from obsalt.domain.redact import payload_shape, preview_text
from obsalt.hangup.taxonomy import annotate_hangup, classify_provider_reason
from obsalt.util import as_float, as_str, canonical_json, dig, duration_ms, ms_from_seconds, parse_datetime, sha256_text


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    if "message" in payload and isinstance(payload["message"], dict):
        return payload["message"]
    return payload


class VapiAdapter:
    provider = Provider.VAPI

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        message = _unwrap(payload)
        event_type = as_str(message.get("type")) or "unknown"
        call_obj = message.get("call") if isinstance(message.get("call"), dict) else {}
        provider_call_id = as_str(dig(call_obj, "id")) or as_str(message.get("callId")) or as_str(payload.get("call_id"))
        if not provider_call_id:
            return None

        assistant = message.get("assistant") if isinstance(message.get("assistant"), dict) else {}
        agent_id = (
            as_str(dig(call_obj, "assistantId"))
            or as_str(assistant.get("id"))
            or as_str(dig(call_obj, "assistant", "id"))
            or "unknown"
        )
        call = empty_call(org_id=org_id, provider=self.provider, provider_call_id=provider_call_id, agent_id=agent_id)
        call.agent_name = as_str(assistant.get("name")) or as_str(dig(call_obj, "assistant", "name"))
        call.raw_event_type = event_type
        call.metadata["vapi_type"] = event_type

        call_type = as_str(call_obj.get("type")) or ""
        if "inbound" in call_type.lower():
            call.direction = CallDirection.INBOUND
        elif "outbound" in call_type.lower():
            call.direction = CallDirection.OUTBOUND

        customer = message.get("customer") if isinstance(message.get("customer"), dict) else call_obj.get("customer") or {}
        phone = message.get("phoneNumber") if isinstance(message.get("phoneNumber"), dict) else {}
        call.from_number = as_str(dig(customer, "number")) or as_str(call_obj.get("customerId"))
        call.to_number = as_str(phone.get("number"))

        started = first_present(message.get("startedAt"), call_obj.get("startedAt"))
        ended = first_present(message.get("endedAt"), call_obj.get("endedAt"))
        call.started_at = parse_datetime(started)
        call.ended_at = parse_datetime(ended)
        call.duration_ms = duration_ms(call.started_at, call.ended_at)

        cost = as_float(message.get("cost")) or as_float(call_obj.get("cost"))
        call.cost_usd = cost

        if event_type == "status-update":
            status = as_str(message.get("status"))
            if status == "ended":
                call.status = CallStatus.ENDED
            elif status in {"in-progress", "ringing", "queued"}:
                call.status = CallStatus.ONGOING
            return AdapterResult(call=call, terminal=False, event_type=event_type)

        if event_type in {"transcript", 'transcript[transcriptType="final"]'}:
            role = speaker_from(as_str(message.get("role")))
            text = as_str(message.get("transcript")) or ""
            if text and as_str(message.get("transcriptType")) != "partial":
                call.turns = [Turn(index=0, speaker=role, text=text)]
            return AdapterResult(call=call, terminal=False, event_type=event_type)

        if event_type in {"tool-calls", "function-call"}:
            call.tools = _tools_from_live(message)
            return AdapterResult(call=call, terminal=False, event_type=event_type)

        if event_type == "user-interrupted":
            call.turns = [Turn(index=0, speaker=Speaker.AGENT, text="", interrupted=True)]
            call.metadata["interrupted_turn"] = message.get("turnId")
            return AdapterResult(call=call, terminal=False, event_type=event_type)

        artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
        messages = artifact.get("messages") or message.get("messages") or []
        if isinstance(messages, list):
            call.turns, tools = _turns_and_tools(messages)
            call.tools = tools

        transcript = as_str(artifact.get("transcript")) or as_str(message.get("transcript"))
        if transcript:
            call.transcript_text = transcript
        else:
            call.transcript_text = transcript_from_turns(call)

        recording = artifact.get("recording") if isinstance(artifact.get("recording"), dict) else {}
        call.recording_url = (
            as_str(recording.get("stereoUrl"))
            or as_str(recording.get("monoUrl"))
            or as_str(artifact.get("recordingUrl"))
            or as_str(artifact.get("stereoRecordingUrl"))
        )

        assistant_obj = assistant or (call_obj.get("assistant") if isinstance(call_obj.get("assistant"), dict) else {})
        model = assistant_obj.get("model") if isinstance(assistant_obj.get("model"), dict) else {}
        prompt_messages = model.get("messages") if isinstance(model.get("messages"), list) else []
        prompts = [text_of(m) for m in prompt_messages if isinstance(m, dict) and m.get("role") == "system"]
        call.grounding.system_prompt = "\n".join(p for p in prompts if p)

        analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
        if analysis.get("summary"):
            call.metadata["summary"] = analysis.get("summary")

        call.latency_samples.extend(_latency_from_vapi(message, artifact, call.turns))

        ended_reason = as_str(message.get("endedReason")) or as_str(call_obj.get("endedReason"))
        terminal = event_type == "end-of-call-report" or bool(ended_reason)
        if ended_reason:
            reason, party = classify_provider_reason("vapi", ended_reason)
            hangup = Hangup(reason=reason, party=party, provider_reason=ended_reason, signal=ended_reason)
            call.hangup = annotate_hangup(call, hangup)
            call.status = CallStatus.ERROR if reason.value.startswith("error_") else CallStatus.ENDED
        elif terminal:
            call.status = CallStatus.ENDED

        if terminal and call.duration_ms is None:
            call.duration_ms = duration_ms(call.started_at, call.ended_at)

        return AdapterResult(call=call, terminal=terminal, event_type=event_type)


def _turns_and_tools(messages: list[Any]) -> tuple[list[Turn], list[ToolInvocation]]:
    turns: list[Turn] = []
    tools: list[ToolInvocation] = []
    pending_tools: dict[str, ToolInvocation] = {}
    index = 0
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = as_str(raw.get("role")) or ""
        if role in {"tool_calls", "tool_call", "function"} or raw.get("toolCalls") or raw.get("toolCallList"):
            for tool in _tools_from_message(raw):
                pending_tools[tool.id] = tool
                tools.append(tool)
            continue
        if role in {"tool_call_result", "tool", "function_call_result"} or raw.get("toolCallId") or raw.get("toolCallResult"):
            _apply_tool_result(pending_tools, tools, raw)
            continue
        speaker = speaker_from(role)
        if speaker in {Speaker.SYSTEM}:
            continue
        text = text_of(raw)
        seconds = as_float(raw.get("secondsFromStart"))
        duration = ms_from_seconds(raw.get("duration"))
        started = parse_datetime(raw.get("time")) or parse_datetime(raw.get("startTime"))
        turn = Turn(
            index=index,
            speaker=speaker,
            text=text,
            seconds_from_start=seconds,
            duration_ms=duration,
            started_at=started,
            interrupted=bool(raw.get("interrupted")),
            metadata={k: raw[k] for k in ("endTime", "metadata") if k in raw},
        )
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        turn.stt_ms = as_float(meta.get("sttDuration") or meta.get("sttLatency") or raw.get("sttDuration"))
        turn.llm_ms = as_float(meta.get("llmLatency") or meta.get("llmDuration") or raw.get("llmLatency"))
        turn.tts_ms = as_float(meta.get("ttsLatency") or meta.get("ttsDuration") or raw.get("ttsLatency"))
        turn.time_to_first_audio_ms = as_float(meta.get("e2eLatency") or meta.get("turnLatency"))
        turns.append(turn)
        index += 1
    return turns, tools


def _tools_from_live(message: dict[str, Any]) -> list[ToolInvocation]:
    items = message.get("toolCallList") or []
    if not items and message.get("functionCall"):
        items = [message["functionCall"]]
    tools = []
    for item in items:
        if not isinstance(item, dict):
            continue
        params = item.get("parameters") or item.get("arguments") or item.get("input") or {}
        name = as_str(item.get("name")) or "unknown"
        tool_id = as_str(item.get("id")) or as_str(item.get("toolCallId")) or name
        tools.append(
            ToolInvocation(
                id=tool_id,
                name=name,
                status=ToolStatus.PENDING,
                payload_shape=payload_shape(params),
                argument_hash=sha256_text(canonical_json(params)),
                metadata={"arguments": params},
            )
        )
    return tools


def _tools_from_message(raw: dict[str, Any]) -> list[ToolInvocation]:
    calls = raw.get("toolCalls") or raw.get("toolCallList") or []
    tools = []
    for item in calls:
        if not isinstance(item, dict):
            continue
        inner = item.get("function") if isinstance(item.get("function"), dict) else item
        name = as_str(inner.get("name")) or as_str(item.get("name")) or "unknown"
        args = inner.get("arguments") or inner.get("parameters") or item.get("parameters") or {}
        tool_id = as_str(item.get("id")) or as_str(dig(item, "toolCall", "id")) or name
        started = parse_datetime(raw.get("time"))
        tools.append(
            ToolInvocation(
                id=tool_id,
                name=name,
                started_at=started,
                status=ToolStatus.PENDING,
                payload_shape=payload_shape(args) if not isinstance(args, str) else payload_shape({"_raw": args}),
                metadata={"arguments": args},
                turn_index=None,
            )
        )
    return tools


def _apply_tool_result(pending: dict[str, ToolInvocation], tools: list[ToolInvocation], raw: dict[str, Any]) -> None:
    result_id = as_str(raw.get("toolCallId")) or as_str(dig(raw, "toolCallResult", "toolCallId"))
    result = raw.get("result") or raw.get("content") or dig(raw, "toolCallResult", "result")
    error = as_str(raw.get("error"))
    tool = pending.get(result_id) if result_id else (tools[-1] if tools else None)
    if tool is None:
        tool = ToolInvocation(id=result_id or "tool", name=as_str(raw.get("name")) or "unknown")
        tools.append(tool)
    tool.ended_at = parse_datetime(raw.get("time"))
    tool.result_preview = preview_text(result)
    if error or (isinstance(result, str) and "error" in result.lower()[:40]):
        tool.status = ToolStatus.ERROR
        tool.error = error or preview_text(result)
    else:
        tool.status = ToolStatus.SUCCESS
    if tool.started_at and tool.ended_at:
        tool.duration_ms = duration_ms(tool.started_at, tool.ended_at)


def _latency_from_vapi(message: dict[str, Any], artifact: dict[str, Any], turns: list[Turn]) -> list[LatencySample]:
    samples: list[LatencySample] = []
    perf = message.get("performanceMetrics") or artifact.get("performanceMetrics") or message.get("performance") or {}
    if isinstance(perf, dict):
        turns_perf = perf.get("turnLatencies") or perf.get("turns") or []
        if isinstance(turns_perf, list):
            for item in turns_perf:
                if not isinstance(item, dict):
                    continue
                idx = int(as_float(item.get("turn") or item.get("index")) or 0)
                for component, key in (
                    (LatencyComponent.STT, "stt"),
                    (LatencyComponent.LLM, "llm"),
                    (LatencyComponent.TTS, "tts"),
                    (LatencyComponent.E2E, "e2e"),
                    (LatencyComponent.TTFA, "ttfa"),
                ):
                    value = as_float(item.get(key) or item.get(f"{key}Ms") or item.get(f"{key}Latency"))
                    if value is not None:
                        samples.append(
                            LatencySample(component=component, duration_ms=value, turn_index=idx, source="provider")
                        )
        for component, key in (
            (LatencyComponent.STT, "stt"),
            (LatencyComponent.LLM, "llm"),
            (LatencyComponent.TTS, "tts"),
            (LatencyComponent.E2E, "e2e"),
        ):
            value = as_float(perf.get(key))
            if value is not None:
                samples.append(LatencySample(component=component, duration_ms=value, source="provider"))
    return samples
