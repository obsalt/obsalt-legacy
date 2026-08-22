"""Deterministic claim pre-filter (Tier 1). LLM entailment is Tier 2."""

from __future__ import annotations

import re

from obsalt.domain.enums import HallucinationKind, ToolStatus
from obsalt.domain.models import CallRevision

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d+\s?(?:dollars|usd)\b", re.I)
_ID_RE = re.compile(
    r"\b(?:order|confirmation|booking|ticket|invoice|reference|case)\s*(?:number|#|id)?\s*[:#]?\s*([A-Z0-9][-A-Z0-9]{4,})\b",
    re.I,
)
_ACTION_RE = re.compile(
    r"\bI(?:'ve| have|'m)\s+(?:just\s+)?(booked|scheduled|cancelled|canceled|sent|processed|refunded|updated|confirmed|placed|created|looked up)\b",
    re.I,
)

_VERB_TO_TOOLS = {
    "booked": ("book", "schedule", "create_appointment", "reservation"),
    "scheduled": ("book", "schedule", "create_appointment"),
    "cancelled": ("cancel",),
    "canceled": ("cancel",),
    "sent": ("send", "email", "sms", "notify"),
    "processed": ("process", "charge", "pay"),
    "refunded": ("refund",),
    "updated": ("update", "modify"),
    "confirmed": ("confirm", "lookup", "get_"),
    "placed": ("create", "place", "order"),
    "created": ("create",),
    "looked up": ("lookup", "get_", "fetch", "find"),
}


def extract_candidate_claims(call: CallRevision) -> list[dict[str, object]]:
    """Cheap candidates that may later reach the LLM entailment judge."""
    grounding = "\n".join(grounding_corpus(call))
    flags: list[dict[str, object]] = []
    successful = [t.name.lower() for t in call.tools if t.status == ToolStatus.SUCCESS]
    failed = [
        t.name.lower() for t in call.tools if t.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}
    ]
    for turn in call.agent_turns():
        for sentence in [s.strip() for s in _SENTENCE_RE.split(turn.text) if s.strip()]:
            for match in _MONEY_RE.finditer(sentence):
                if match.group(0).lower() not in grounding.lower():
                    flags.append(
                        _flag(HallucinationKind.PRICE_CLAIM, sentence, turn.index, match.group(0))
                    )
            for match in _ID_RE.finditer(sentence):
                if match.group(1).lower() not in grounding.lower():
                    flags.append(
                        _flag(HallucinationKind.FABRICATED_ID, sentence, turn.index, match.group(1))
                    )
            action = _ACTION_RE.search(sentence)
            if action:
                verb = action.group(1)
                hints = _VERB_TO_TOOLS.get(verb.lower(), ())
                related_success = any(any(h in n for h in hints) for n in successful)
                related_failed = any(any(h in n for h in hints) for n in failed)
                if related_failed and not related_success:
                    flags.append(
                        _flag(HallucinationKind.PHANTOM_TOOL_SUCCESS, sentence, turn.index, verb)
                    )
    return flags


def grounding_corpus(call: CallRevision) -> list[str]:
    """Prompt, knowledge, tool results/errors, and caller statements (§9.4)."""
    parts = [turn.text for turn in call.user_turns() if turn.text]
    parts.extend(item.content for item in call.grounding if item.content)
    for tool in call.tools:
        status = tool.status.value
        result = getattr(tool, "result", None)
        if tool.error:
            parts.append(f"{tool.name} {status}: {tool.error}")
        elif result not in (None, ""):
            parts.append(
                f"{tool.name} {status}: {result if isinstance(result, str) else str(result)}"
            )
        elif tool.result_ref:
            parts.append(f"{tool.name} {status}: {tool.result_ref}")
        else:
            parts.append(f"{tool.name} {status}")
    return parts


def _grounding_corpus(call: CallRevision) -> list[str]:
    return grounding_corpus(call)


def _flag(kind: HallucinationKind, span: str, turn_index: int, evidence: str) -> dict[str, object]:
    return {
        "kind": kind.value,
        "span_text": span,
        "turn_index": turn_index,
        "evidence": [evidence],
        "needs_llm": True,
    }
