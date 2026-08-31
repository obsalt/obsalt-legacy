"""Deterministic tool-call integrity flags from hashes and clocks. No judge.

These are operational signals. Only `double_invocation` is additionally a
detector-settled `tool_use` contradiction, because a repeated mutating call
with the same argument hash is a potential double charge.
"""

from __future__ import annotations

import re
from collections import defaultdict

from obsalt.analysis.hallucination import tool_effectively_failed, tool_effectively_succeeded
from obsalt.domain.models import CallRevision, ToolInvocation

RETRY_STORM_MIN_ATTEMPTS = 3

_MUTATING_TOOL_PARTS = (
    "refund",
    "charge",
    "payment",
    "pay",
    "send",
    "email",
    "sms",
    "book",
    "reserve",
    "create",
    "cancel",
    "update",
    "schedule",
    "place",
    "submit",
    "transfer",
    "delete",
)
_PROMISE_RE = re.compile(
    r"\bI(?:'ll| will|'m going to| can)\s+(send|email|text|message|book|schedule|"
    r"refund|process|transfer|update|submit)\b",
    re.I,
)
_PROMISE_TOOL_PARTS: dict[str, tuple[str, ...]] = {
    "send": ("send", "email", "sms", "message", "notify", "text"),
    "email": ("send", "email", "notify"),
    "text": ("send", "sms", "message", "text"),
    "message": ("send", "sms", "message", "notify"),
    "book": ("book", "reserve", "create", "schedule", "place"),
    "schedule": ("schedule", "book", "create", "reserve"),
    "refund": ("refund",),
    "process": ("process", "refund", "charge", "submit", "place"),
    "transfer": ("transfer", "route", "forward"),
    "update": ("update", "modify", "change"),
    "submit": ("submit", "create", "place", "process"),
}
_TRANSFER_PROMISE_RE = re.compile(
    r"\btransfer(?:ring)?\s+(?:you|this call|the call)\b"
    r"|\b(?:connect|connecting)\s+(?:you|this call|the call)\b"
    r"|\bput(?:ting)?\s+you\s+through\b",
    re.I,
)


def is_mutating_tool(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _MUTATING_TOOL_PARTS)


def tool_integrity_flags(call: CallRevision) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for dup in double_invocations(call):
        out.append(
            {
                "kind": "double_invocation",
                "tool": dup["tool"],
                "invocations": dup["invocations"],
            }
        )
    storm = retry_storms(call)
    for name, attempts, failures in storm:
        out.append(
            {"kind": "retry_storm", "tool": name, "attempts": attempts, "failures": failures}
        )
    for promise in unfulfilled_promises(call):
        out.append(promise)
    if broken_transfer_promise(call):
        out.append({"kind": "broken_transfer_promise"})
    return out


def double_invocations(call: CallRevision) -> list[dict[str, object]]:
    """Same mutating tool + argument hash, more than one effective success."""
    groups: dict[tuple[str, str], list[ToolInvocation]] = defaultdict(list)
    for tool in call.tools:
        if not is_mutating_tool(tool.name):
            continue
        if not tool_effectively_succeeded(tool):
            continue
        groups[(tool.name, tool.argument_hash or "")].append(tool)
    return [
        {"tool": name, "invocations": len(tools)}
        for (name, _), tools in sorted(groups.items())
        if len(tools) >= 2
    ]


def retry_storms(call: CallRevision) -> list[tuple[str, int, int]]:
    """Same tool + argument hash attempted three or more times, mostly failing."""
    groups: dict[tuple[str, str], list[ToolInvocation]] = defaultdict(list)
    for tool in call.tools:
        groups[(tool.name, tool.argument_hash or "")].append(tool)
    storms: list[tuple[str, int, int]] = []
    for (name, _), tools in sorted(groups.items()):
        if len(tools) < RETRY_STORM_MIN_ATTEMPTS:
            continue
        failures = sum(1 for tool in tools if tool_effectively_failed(tool))
        if failures >= len(tools) - failures and failures >= 2:
            storms.append((name, len(tools), failures))
    return storms


def unfulfilled_promises(call: CallRevision) -> list[dict[str, object]]:
    """Agent promised an action; no tool of that class ran after the promise."""
    if not call.tools:
        return []
    out: list[dict[str, object]] = []
    for turn in call.agent_turns():
        if not turn.text:
            continue
        for match in _PROMISE_RE.finditer(turn.text):
            verb = match.group(1).lower()
            parts = _PROMISE_TOOL_PARTS.get(verb)
            if not parts:
                continue
            fulfilled = any(
                (tool_index := _tool_index(tool)) is not None
                and tool_index > turn.index
                and any(part in tool.name.lower() for part in parts)
                for tool in call.tools
            )
            if not fulfilled:
                out.append(
                    {
                        "kind": "unfulfilled_promise",
                        "verb": verb,
                        "span_text": match.group(0),
                        "turn_index": turn.index,
                    }
                )
    return out


def broken_transfer_promise(call: CallRevision) -> bool:
    """Agent said it was transferring the caller; the call did not end in transfer."""
    if call.hangup is not None and call.hangup.reason.value == "transfer":
        return False
    return any(_TRANSFER_PROMISE_RE.search(turn.text) for turn in call.agent_turns() if turn.text)


def _tool_index(tool: ToolInvocation) -> int | None:
    index = tool.turn_index
    return index if isinstance(index, int) else None
