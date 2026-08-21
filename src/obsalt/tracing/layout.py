"""Shared call → span-tree layout.

Used by OTLP reconstruction and by the HTTP join view so Tempo and the
correlation UI describe the same conversation.
"""

from __future__ import annotations

from typing import Any

from obsalt.domain.enums import Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, ToolInvocation, Turn
from obsalt.tracing import conventions as c


def tools_by_turn(call: CanonicalCall) -> dict[int, list[ToolInvocation]]:
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


def stt_attempts_for(turn: Turn) -> list[dict[str, Any]]:
    """Provider hops recorded on the turn. Empty means 'unknown', not 'invent a fallback'."""
    raw = turn.metadata.get("stt_attempts")
    if not isinstance(raw, list):
        return []
    attempts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider = c.known_provider(item.get("provider"))
        if not provider:
            continue
        attempts.append(
            {
                "provider": provider,
                "fallback": bool(item.get("fallback")),
                "latency_ms": item.get("latency_ms"),
                "confidence": item.get("confidence"),
                "error": item.get("error") or item.get("error_type"),
                "model": item.get("model"),
            }
        )
    return attempts


def hosted_provider(call: CanonicalCall) -> bool:
    return call.provider.value in {"vapi", "retell", "bland"}


def reviewer_safe_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop PII keys and values that look like transcripts, emails, or phones."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if key in c.PII_FORBIDDEN_ATTR_KEYS:
            continue
        if key in {"from_number", "to_number", "customer.number", "transcript_text", "text"}:
            continue
        if isinstance(value, str):
            lowered = value.lower()
            if "@" in value and "." in value.split("@")[-1]:
                continue
            if any(token in lowered for token in ("ssn", "password", "secret")):
                continue
        out[key] = value
    return out


def join_from_call(call: CanonicalCall) -> dict[str, Any]:
    attrs = c.join_attributes(
        call.id,
        call.org_id,
        call.agent_id,
        provider_call_id=call.provider_call_id,
    )
    for key in (c.ROOM_ID, c.TEST_RUN_ID, c.SCENARIO_ID, c.CALL_LANGUAGES):
        value = call.metadata.get(key)
        if value is not None:
            attrs[key] = value
    return reviewer_safe_attrs(attrs)


def tool_failed(tool: ToolInvocation) -> bool:
    return tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}
