from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from obsalt.domain.enums import CallStatus, Provider, Speaker
from obsalt.domain.models import CanonicalCall, GroundingContext
from obsalt.util import as_str, call_id_for, utcnow


@dataclass
class AdapterResult:
    call: CanonicalCall
    terminal: bool
    event_type: str


class Adapter(Protocol):
    provider: Provider

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None: ...


def speaker_from(role: str | None) -> Speaker:
    if not role:
        return Speaker.UNKNOWN
    value = role.strip().lower()
    if value in {"user", "customer", "human", "caller"}:
        return Speaker.USER
    if value in {"agent", "assistant", "bot", "ai", "model"}:
        return Speaker.AGENT
    if value in {"system"}:
        return Speaker.SYSTEM
    if value in {"tool", "function", "tool_call_result", "tool_call_invocation", "agent-action"}:
        return Speaker.TOOL
    return Speaker.UNKNOWN


def empty_call(
    *,
    org_id: str,
    provider: Provider,
    provider_call_id: str,
    agent_id: str = "unknown",
) -> CanonicalCall:
    return CanonicalCall(
        id=call_id_for(org_id, provider.value, provider_call_id),
        org_id=org_id,
        provider=provider,
        provider_call_id=provider_call_id,
        agent_id=agent_id or "unknown",
        ingested_at=utcnow(),
        updated_at=utcnow(),
        grounding=GroundingContext(),
        status=CallStatus.ONGOING,
    )


def transcript_from_turns(call: CanonicalCall) -> str:
    if call.transcript_text.strip():
        return call.transcript_text
    return "\n".join(f"{t.speaker.value}: {t.text}".strip() for t in call.turns if t.text)


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def text_of(message: dict[str, Any]) -> str:
    for key in ("message", "content", "text", "transcript", "content_text"):
        value = as_str(message.get(key))
        if value:
            return value
    return ""
