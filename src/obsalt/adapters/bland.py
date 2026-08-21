from __future__ import annotations

import re
from typing import Any

from obsalt.adapters.base import AdapterResult, empty_call, speaker_from, transcript_from_turns
from obsalt.domain.enums import (
    CallDirection,
    CallStatus,
    HangupParty,
    HangupReason,
    LatencyComponent,
    Provider,
    Speaker,
    ToolStatus,
)
from obsalt.domain.models import Hangup, LatencySample, ToolInvocation, Turn
from obsalt.domain.redact import payload_shape, preview_text
from obsalt.hangup.taxonomy import annotate_hangup, classify_provider_reason
from obsalt.util import as_float, as_str, duration_ms, parse_datetime, sha256_text, canonical_json

_LATENCY_LINE = re.compile(r"(STT|ASR|TTS|LLM|E2E)\s*:\s*(\d+(?:\.\d+)?)\s*ms", re.I)
_TOOL_LINE = re.compile(r"Executing custom tool:\s*(.+?)\s+with input:\s*(.*)$", re.I)


def _is_live_event(payload: dict[str, Any]) -> bool:
    return "category" in payload and "message" in payload and "call_id" in payload


class BlandAdapter:
    provider = Provider.BLAND

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        if _is_live_event(payload):
            return _parse_live(payload, org_id=org_id)

        provider_call_id = as_str(payload.get("call_id")) or as_str(payload.get("c_id"))
        if not provider_call_id:
            return None

        agent_id = (
            as_str(payload.get("pathway_id"))
            or as_str((payload.get("metadata") or {}).get("agentName"))
            or as_str(payload.get("voice_id"))
            or "unknown"
        )
        call = empty_call(org_id=org_id, provider=self.provider, provider_call_id=provider_call_id, agent_id=agent_id)
        call.raw_event_type = "post_call"
        inbound = payload.get("inbound")
        if inbound is True:
            call.direction = CallDirection.INBOUND
        elif inbound is False:
            call.direction = CallDirection.OUTBOUND
        call.from_number = as_str(payload.get("from"))
        call.to_number = as_str(payload.get("to"))
        call.started_at = parse_datetime(payload.get("started_at") or payload.get("created_at"))
        call.ended_at = parse_datetime(payload.get("end_at"))
        length_min = as_float(payload.get("call_length"))
        corrected = as_float(payload.get("corrected_duration"))
        if corrected is not None:
            call.duration_ms = corrected * 1000.0
        elif length_min is not None:
            call.duration_ms = length_min * 60_000.0
        else:
            call.duration_ms = duration_ms(call.started_at, call.ended_at)
        call.recording_url = as_str(payload.get("recording_url"))
        call.transcript_text = as_str(payload.get("concatenated_transcript")) or ""
        call.cost_usd = as_float(payload.get("price"))
        if payload.get("summary"):
            call.metadata["summary"] = payload.get("summary")
        call.metadata["answered_by"] = payload.get("answered_by")
        call.metadata["disposition_tag"] = payload.get("disposition_tag")
        call.metadata["call_ended_by"] = payload.get("call_ended_by")
        variables = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
        if variables:
            call.grounding.knowledge.append("variables: " + canonical_json({k: v for k, v in variables.items() if k not in {"from", "to"}}))

        transcripts = payload.get("transcripts") if isinstance(payload.get("transcripts"), list) else []
        call.turns, extra_tools = _turns_from_transcripts(transcripts)
        call.tools.extend(extra_tools)
        if not call.transcript_text:
            call.transcript_text = transcript_from_turns(call)

        pathway_logs = payload.get("pathway_logs") if isinstance(payload.get("pathway_logs"), list) else []
        for log in pathway_logs:
            if not isinstance(log, dict):
                continue
            decision = as_str(log.get("decision"))
            if decision:
                call.grounding.knowledge.append(decision)

        citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
        for citation in citations:
            if isinstance(citation, dict) and citation.get("value") is not None:
                call.grounding.knowledge.append(f"{citation.get('variable_name')}={citation.get('value')}")

        # Derive e2e from transcript timestamps.
        previous = None
        for turn in call.turns:
            if previous and previous.speaker == Speaker.USER and turn.speaker == Speaker.AGENT:
                if previous.started_at and turn.started_at:
                    gap = max(0.0, (turn.started_at - previous.started_at).total_seconds() * 1000.0)
                    turn.time_to_first_audio_ms = gap
                    call.latency_samples.append(
                        LatencySample(component=LatencyComponent.E2E, duration_ms=gap, turn_index=turn.index, source="derived")
                    )
                    call.latency_samples.append(
                        LatencySample(component=LatencyComponent.TTFA, duration_ms=gap, turn_index=turn.index, source="derived")
                    )
            previous = turn

        ended_by = as_str(payload.get("call_ended_by")) or ""
        disposition = as_str(payload.get("disposition_tag"))
        error_message = as_str(payload.get("error_message"))
        reason_code = disposition or ended_by or ("FAILED" if error_message else "")
        reason, party = classify_provider_reason("bland", reason_code)
        if ended_by.upper() == "USER":
            party = HangupParty.USER
            if reason in {HangupReason.COMPLETED, HangupReason.UNKNOWN}:
                reason = HangupReason.USER_HANGUP
        elif ended_by.upper() == "ASSISTANT":
            party = HangupParty.AGENT
            if reason == HangupReason.UNKNOWN:
                reason = HangupReason.AGENT_HANGUP
        if payload.get("transferred_to"):
            reason = HangupReason.TRANSFER
            party = HangupParty.AGENT
        if (as_str(payload.get("answered_by")) or "").lower() == "voicemail":
            reason = HangupReason.VOICEMAIL
        if error_message and payload.get("completed") is False:
            reason = HangupReason.ERROR_UNKNOWN
            call.status = CallStatus.ERROR
        hangup = Hangup(
            reason=reason,
            party=party,
            provider_reason=reason_code or ended_by or "unknown",
            signal=ended_by or disposition,
        )
        call.hangup = annotate_hangup(call, hangup)
        if call.status != CallStatus.ERROR:
            call.status = CallStatus.ENDED

        terminal = True
        if payload.get("queue_status") in {"queued", "started"}:
            terminal = False
            call.status = CallStatus.ONGOING
        return AdapterResult(call=call, terminal=terminal, event_type=call.raw_event_type or "post_call")


def _turns_from_transcripts(items: list[Any]) -> tuple[list[Turn], list[ToolInvocation]]:
    turns: list[Turn] = []
    tools: list[ToolInvocation] = []
    index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("user") or item.get("role")) or "user"
        text = as_str(item.get("text")) or ""
        started = parse_datetime(item.get("created_at"))
        if role in {"agent-action", "tool"}:
            tools.append(
                ToolInvocation(
                    id=str(item.get("id") or f"action-{index}"),
                    name=text.split(":")[0].strip() or "agent-action",
                    started_at=started,
                    ended_at=started,
                    status=ToolStatus.SUCCESS,
                    result_preview=preview_text(text),
                    payload_shape=payload_shape({"text": text}),
                )
            )
            continue
        turns.append(
            Turn(
                index=index,
                speaker=speaker_from(role),
                text=text,
                started_at=started,
            )
        )
        index += 1
    # duration from successive timestamps
    for i, turn in enumerate(turns[:-1]):
        nxt = turns[i + 1]
        if turn.started_at and nxt.started_at:
            turn.duration_ms = max(0.0, (nxt.started_at - turn.started_at).total_seconds() * 1000.0)
            turn.ended_at = nxt.started_at
    return turns, tools


def _parse_live(payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
    provider_call_id = as_str(payload.get("call_id"))
    if not provider_call_id:
        return None
    call = empty_call(org_id=org_id, provider=Provider.BLAND, provider_call_id=provider_call_id)
    category = as_str(payload.get("category")) or "call"
    message = as_str(payload.get("message")) or ""
    call.raw_event_type = f"live:{category}"
    terminal = False
    if category == "latency":
        match = _LATENCY_LINE.search(message)
        if match:
            name, value = match.group(1).upper(), float(match.group(2))
            component = {
                "STT": LatencyComponent.STT,
                "ASR": LatencyComponent.STT,
                "LLM": LatencyComponent.LLM,
                "TTS": LatencyComponent.TTS,
                "E2E": LatencyComponent.E2E,
            }[name]
            extra = {}
            if component == LatencyComponent.LLM:
                extra["ttft_ms"] = value
            if component == LatencyComponent.TTS:
                extra["ttfb_ms"] = value
            call.latency_samples.append(LatencySample(component=component, duration_ms=value, source="provider", **extra))
            if component == LatencyComponent.E2E:
                call.latency_samples.append(LatencySample(component=LatencyComponent.TTFA, duration_ms=value, source="provider"))
    elif category == "tool":
        match = _TOOL_LINE.search(message)
        name = match.group(1).strip() if match else "tool"
        call.tools.append(
            ToolInvocation(
                id=sha256_text(message)[:16],
                name=name,
                status=ToolStatus.PENDING,
                metadata={"raw": message[:300]},
            )
        )
    return AdapterResult(call=call, terminal=terminal, event_type=call.raw_event_type)
