from __future__ import annotations

import re
from collections.abc import Sequence

from obsalt.domain.events import (
    CallObserved,
    GroundingObserved,
    NormalizedEvent,
    OutcomeObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.redact_shape import json_type_name
from obsalt.plugin.types import RedactionPolicy, RedactionResult

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def redact_text(text: str, policy: RedactionPolicy) -> str:
    out = text
    if policy.redact_emails:
        out = _EMAIL_RE.sub("<email>", out)
    if policy.redact_phones:
        out = _PHONE_RE.sub("<phone>", out)
    if policy.redact_cards:
        out = _CARD_RE.sub("<card>", out)
        out = _SSN_RE.sub("<ssn>", out)
    for pattern in policy.extra_patterns:
        out = re.compile(pattern).sub("<redacted>", out)
    return out


class DefaultRedactor:
    def redact(self, events: Sequence[NormalizedEvent], policy: RedactionPolicy) -> RedactionResult:
        redacted_fields: list[str] = []
        out: list[NormalizedEvent] = []
        for event in events:
            cloned = event.model_copy(deep=True)
            if isinstance(cloned, TurnObserved) and cloned.text:
                cloned.text = redact_text(cloned.text, policy)
                redacted_fields.append(f"turn[{cloned.turn_index}].text")
            elif isinstance(cloned, GroundingObserved) and cloned.content:
                cloned.content = redact_text(cloned.content, policy)
                redacted_fields.append(f"grounding.{cloned.kind.value}")
            elif isinstance(cloned, ToolObserved):
                cloned.args = _redact_obj(cloned.args, policy)
                cloned.result = _redact_obj(cloned.result, policy)
                redacted_fields.append(f"tool[{cloned.tool_id}]")
            elif isinstance(cloned, CallObserved):
                if cloned.from_number:
                    cloned.from_number = "<phone>"
                    redacted_fields.append("call.from_number")
                if cloned.to_number:
                    cloned.to_number = "<phone>"
                    redacted_fields.append("call.to_number")
            elif isinstance(cloned, OutcomeObserved):
                pass
            out.append(cloned)
        return RedactionResult(events=out, policy_version=policy.version, redacted_fields=redacted_fields)


def _redact_obj(value: object, policy: RedactionPolicy) -> object:
    if isinstance(value, str):
        return redact_text(value, policy)
    if isinstance(value, dict):
        return {str(k): f"<{json_type_name(v)}:redacted>" if _sensitive(str(k)) else _redact_obj(v, policy) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_obj(item, policy) for item in value[:20]]
    return value


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("email", "phone", "ssn", "card", "secret", "token", "password"))


def redact_events(events: Sequence[NormalizedEvent], policy: RedactionPolicy | None = None) -> RedactionResult:
    """Single choke point. Every source's normalized events pass through here before persistence."""
    return DefaultRedactor().redact(events, policy or RedactionPolicy())
