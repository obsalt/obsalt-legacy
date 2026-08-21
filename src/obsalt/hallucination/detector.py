from __future__ import annotations

import re

from obsalt.domain.enums import HallucinationKind, ToolStatus
from obsalt.domain.models import CanonicalCall, GroundingContext, HallucinationFlag

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d+\s?(?:dollars|usd)\b", re.I)
_ID_RE = re.compile(
    r"\b(?:order|confirmation|booking|ticket|invoice|reference|case)\s*(?:number|#|id)?\s*[:#]?\s*([A-Z0-9][-A-Z0-9]{4,})\b",
    re.I,
)
_NAKED_ID_RE = re.compile(r"\b[A-Z]{2,}\d{4,}[A-Z0-9-]*\b")
_ACTION_RE = re.compile(
    r"\bI(?:'ve| have|'m)\s+(?:just\s+)?(booked|scheduled|cancelled|canceled|sent|processed|refunded|updated|confirmed|placed|created|looked up)\b",
    re.I,
)
_POLICY_RE = re.compile(r"\b(?:our|the)\s+(?:return\s+)?policy\b|\b\d+[-\s]?day(?:s)?\s+return\b", re.I)
_COMMIT_RE = re.compile(
    r"\b(?:I(?:'ll| will)|we(?:'ll| will))\s+(?:call|email|send|follow up|reach out).{0,40}\b(?:tomorrow|monday|tuesday|wednesday|thursday|friday|next week|tonight)\b",
    re.I,
)
_PRIVATE_RE = re.compile(r"\b(?:ssn|social security|password|cvv|pin number)\b", re.I)

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


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text) if s and s.strip()]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def grounded(span: str, corpus: str) -> bool:
    if not span.strip():
        return True
    hay = _norm(corpus)
    needle = _norm(span)
    if needle and needle in hay:
        return True
    tokens = [t for t in re.findall(r"[a-z0-9$]+", needle) if len(t) > 2]
    if not tokens:
        return True
    hits = sum(1 for t in tokens if t in hay)
    return hits / len(tokens) >= 0.8


def _corpus(ctx: GroundingContext) -> str:
    parts = [ctx.system_prompt, ctx.user_text, *ctx.knowledge, *ctx.tool_results]
    return "\n".join(p for p in parts if p)


def _tool_matches(verb: str, names: list[str]) -> bool:
    hints = _VERB_TO_TOOLS.get(verb.lower(), ())
    lowered = [n.lower() for n in names]
    return any(any(h in n for h in hints) for n in lowered)


def detect_hallucinations(call: CanonicalCall) -> list[HallucinationFlag]:
    ctx = call.grounding
    user_text = " ".join(t.text for t in call.user_turns())
    ctx.user_text = ctx.user_text or user_text
    successful = [t for t in call.tools if t.status == ToolStatus.SUCCESS]
    failed = [t for t in call.tools if t.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}]
    tool_names = [t.name for t in call.tools]
    success_names = [t.name for t in successful]
    ctx.tool_results = ctx.tool_results or [t.result_preview or "" for t in successful]
    corpus = _corpus(ctx)
    flags: list[HallucinationFlag] = []

    for turn in call.agent_turns():
        for sentence in _sentences(turn.text):
            for match in _MONEY_RE.finditer(sentence):
                span = match.group(0)
                if not grounded(span, corpus):
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.PRICE_CLAIM,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.82,
                            rationale=f"Price {span} is not present in the system prompt, knowledge, or tool results.",
                            evidence=[span],
                        )
                    )
            for match in _ID_RE.finditer(sentence):
                ident = match.group(1)
                if not grounded(ident, corpus):
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.FABRICATED_ID,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.9,
                            rationale=f"Identifier {ident} was not returned by a tool or provided by the user.",
                            evidence=[ident],
                        )
                    )
            for match in _NAKED_ID_RE.finditer(sentence):
                ident = match.group(0)
                if ident in {"USD", "HTTP", "JSON"}:
                    continue
                if not grounded(ident, corpus):
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.FABRICATED_ID,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.7,
                            rationale=f"Opaque identifier {ident} is ungrounded.",
                            evidence=[ident],
                        )
                    )
            action = _ACTION_RE.search(sentence)
            if action:
                verb = action.group(1)
                related_success = _tool_matches(verb, success_names)
                related_failed = _tool_matches(verb, [t.name for t in failed])
                related_any = _tool_matches(verb, tool_names)
                if related_failed and not related_success:
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.PHANTOM_TOOL_SUCCESS,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.93,
                            rationale=f"Agent claimed to have {verb} after the matching tool failed.",
                            evidence=[verb],
                        )
                    )
                elif not related_any and not grounded(sentence, corpus):
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.PHANTOM_TOOL_SUCCESS,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.8,
                            rationale=f"Agent claimed to have {verb} but no matching tool ran this call.",
                            evidence=[verb],
                        )
                    )
            if _POLICY_RE.search(sentence) and not grounded(sentence, corpus):
                flags.append(
                    HallucinationFlag(
                        kind=HallucinationKind.POLICY_CLAIM,
                        span_text=sentence,
                        turn_index=turn.index,
                        confidence=0.75,
                        rationale="Policy claim is not present in grounding knowledge.",
                        evidence=[],
                    )
                )
            if _COMMIT_RE.search(sentence):
                calendar_tools = [n for n in success_names if any(h in n.lower() for h in ("calendar", "schedule", "follow"))]
                if not calendar_tools and not grounded(sentence, corpus):
                    flags.append(
                        HallucinationFlag(
                            kind=HallucinationKind.COMMITMENT,
                            span_text=sentence,
                            turn_index=turn.index,
                            confidence=0.6,
                            rationale="Temporal commitment without a successful scheduling tool.",
                            evidence=[],
                        )
                    )
            if _PRIVATE_RE.search(sentence) and not grounded(sentence, ctx.user_text):
                flags.append(
                    HallucinationFlag(
                        kind=HallucinationKind.PRIVATE_KNOWLEDGE,
                        span_text=sentence,
                        turn_index=turn.index,
                        confidence=0.88,
                        rationale="Agent claimed private credentials it should not know.",
                        evidence=[],
                    )
                )

    # Deduplicate identical span+kind
    unique: list[HallucinationFlag] = []
    seen: set[tuple[str, int, str]] = set()
    for flag in flags:
        key = (flag.kind.value, flag.turn_index, flag.span_text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(flag)
    return unique
