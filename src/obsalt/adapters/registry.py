from __future__ import annotations

from typing import Any

from obsalt.adapters.base import Adapter, AdapterResult
from obsalt.adapters.bland import BlandAdapter
from obsalt.adapters.native import NativeAdapter
from obsalt.adapters.openai_realtime import OpenAIRealtimeAdapter
from obsalt.adapters.retell import RetellAdapter
from obsalt.adapters.vapi import VapiAdapter
from obsalt.domain.enums import Provider
from obsalt.domain.models import CanonicalCall, ToolInvocation, Turn


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[Provider, Adapter] = {
            Provider.VAPI: VapiAdapter(),
            Provider.RETELL: RetellAdapter(),
            Provider.BLAND: BlandAdapter(),
            Provider.OPENAI_REALTIME: OpenAIRealtimeAdapter(),
            Provider.NATIVE: NativeAdapter(),
        }

    def parse(self, provider: Provider, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        adapter = self._adapters[provider]
        result = adapter.parse(payload, org_id=org_id)
        if result is None and provider == Provider.OPENAI_REALTIME:
            result = self._adapters[Provider.NATIVE].parse({**payload, "provider": provider.value}, org_id=org_id)
        return result


def merge_calls(base: CanonicalCall, incoming: CanonicalCall) -> CanonicalCall:
    """Idempotent merge of a live event into an existing call snapshot."""
    if incoming.agent_id and incoming.agent_id != "unknown":
        base.agent_id = incoming.agent_id
    base.agent_name = incoming.agent_name or base.agent_name
    if incoming.direction.value != "unknown":
        base.direction = incoming.direction
    base.from_number = incoming.from_number or base.from_number
    base.to_number = incoming.to_number or base.to_number
    base.started_at = base.started_at or incoming.started_at
    base.ended_at = incoming.ended_at or base.ended_at
    base.duration_ms = incoming.duration_ms or base.duration_ms
    if incoming.status.value == "ended" or incoming.status.value == "error":
        base.status = incoming.status
    base.recording_url = incoming.recording_url or base.recording_url
    if incoming.transcript_text and len(incoming.transcript_text) >= len(base.transcript_text):
        base.transcript_text = incoming.transcript_text
    if incoming.hangup is not None:
        base.hangup = incoming.hangup
    if incoming.cost_usd is not None:
        base.cost_usd = incoming.cost_usd
    if incoming.grounding.system_prompt:
        base.grounding.system_prompt = incoming.grounding.system_prompt
    if incoming.grounding.knowledge:
        existing = set(base.grounding.knowledge)
        for item in incoming.grounding.knowledge:
            if item not in existing:
                base.grounding.knowledge.append(item)
    base.metadata.update(incoming.metadata)
    base.raw_event_type = incoming.raw_event_type or base.raw_event_type
    base.turns = _merge_turns(base.turns, incoming.turns)
    base.tools = _merge_tools(base.tools, incoming.tools)
    base.latency_samples = list(base.latency_samples) + [
        s for s in incoming.latency_samples if s not in base.latency_samples
    ]
    base.updated_at = incoming.updated_at
    return base


def _merge_turns(existing: list[Turn], incoming: list[Turn]) -> list[Turn]:
    if not incoming:
        return existing
    if not existing:
        return incoming
    # If incoming looks like a full snapshot (multiple turns, higher indexes), prefer it.
    if len(incoming) >= len(existing) and incoming[-1].index >= (existing[-1].index if existing else 0):
        if any(t.text for t in incoming):
            # Reindex if the incoming snapshot is complete.
            if len(incoming) > 1 or (len(existing) == 1 and incoming[0].text):
                full = incoming if len(incoming) >= len(existing) and sum(1 for t in incoming if t.text) >= sum(1 for t in existing if t.text) else existing + incoming
                # Dedup by speaker+text+seconds
                seen: set[tuple] = set()
                merged: list[Turn] = []
                for turn in existing + incoming:
                    key = (turn.speaker.value, turn.text, round(turn.seconds_from_start or -1, 2))
                    if key in seen and turn.text:
                        continue
                    seen.add(key)
                    merged.append(turn)
                for i, turn in enumerate(merged):
                    turn.index = i
                return merged
    for turn in incoming:
        if turn.text or turn.interrupted:
            existing.append(turn)
    for i, turn in enumerate(existing):
        turn.index = i
    return existing


def _merge_tools(existing: list[ToolInvocation], incoming: list[ToolInvocation]) -> list[ToolInvocation]:
    by_id = {t.id: t for t in existing}
    for tool in incoming:
        current = by_id.get(tool.id)
        if current is None:
            by_id[tool.id] = tool
            continue
        if tool.status.value != "pending":
            current.status = tool.status
        current.error = tool.error or current.error
        current.result_preview = tool.result_preview or current.result_preview
        current.ended_at = tool.ended_at or current.ended_at
        current.duration_ms = tool.duration_ms or current.duration_ms
        current.payload_shape = tool.payload_shape or current.payload_shape
        current.metadata.update(tool.metadata)
    return list(by_id.values())
