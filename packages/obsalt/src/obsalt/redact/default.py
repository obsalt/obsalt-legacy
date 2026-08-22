"""Synchronous redaction choke point. Runs on the normalized event stream (T5).

Raw blobs stay unredacted by definition and have a shorter retention boundary.
Queryable normalized content never bypasses this function.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from obsalt.domain.events import (
    CallObserved,
    GroundingObserved,
    NormalizedEvent,
    ToolObserved,
    TurnObserved,
)
from obsalt.plugin.protocol import RedactionPolicy, RedactionResult

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def redact_text(text: str, policy: RedactionPolicy) -> str:
    out = text
    if policy.redact_email:
        out = _EMAIL_RE.sub("<email>", out)
    if policy.redact_card:
        out = _CARD_RE.sub("<card>", out)
    out = _SSN_RE.sub("<ssn>", out)
    if policy.redact_phone:
        out = _PHONE_RE.sub("<phone>", out)
    return out


def _redact_value(value: Any, policy: RedactionPolicy) -> Any:
    if isinstance(value, str):
        return redact_text(value, policy)
    if isinstance(value, dict):
        return {str(k): _redact_value(v, policy) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, policy) for item in value]
    return value


class DefaultRedactor:
    name = "default"

    def redact(self, events: Sequence[NormalizedEvent], policy: RedactionPolicy) -> RedactionResult:
        redacted_fields: list[str] = []
        out: list[NormalizedEvent] = []
        for event in events:
            if isinstance(event, TurnObserved) and event.text:
                text = redact_text(event.text, policy)
                if text != event.text:
                    redacted_fields.append(f"turn[{event.turn_index}].text")
                out.append(event.model_copy(update={"text": text}))
            elif isinstance(event, GroundingObserved):
                text = redact_text(event.content, policy)
                if text != event.content:
                    redacted_fields.append(f"grounding.{event.kind.value}")
                out.append(event.model_copy(update={"content": text}))
            elif isinstance(event, ToolObserved):
                args = _redact_value(event.args, policy)
                result = _redact_value(event.result, policy)
                if args != event.args or result != event.result:
                    redacted_fields.append(f"tool[{event.tool_id}].payload")
                out.append(event.model_copy(update={"args": args, "result": result}))
            elif isinstance(event, CallObserved):
                frm = redact_text(event.from_number, policy) if event.from_number else event.from_number
                to = redact_text(event.to_number, policy) if event.to_number else event.to_number
                if frm != event.from_number or to != event.to_number:
                    redacted_fields.append("telephony")
                out.append(event.model_copy(update={"from_number": frm, "to_number": to}))
            else:
                out.append(event)
        return RedactionResult(
            events=list(out),
            policy_version=policy.version,
            redacted_fields=redacted_fields,
        )
