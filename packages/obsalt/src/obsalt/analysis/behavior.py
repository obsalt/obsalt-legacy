"""Deterministic behavioral flags from clocks and closed lexicons. No judge.

These are operational signals, never eval verdicts. Every rule must be
explainable from the transcript clocks alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from obsalt.domain.enums import HangupParty, HangupReason, Speaker
from obsalt.domain.models import CallRevision, Turn

AGENT_MONOLOGUE_SECONDS = 60.0
TALK_RATIO = 3.0
MIN_USER_SPEECH_SECONDS = 5.0
DEAD_AIR_BEFORE_HANGUP_SECONDS = 8.0
USER_REPEAT_MIN_CHARS = 8

_FAREWELL_STRONG_RE = re.compile(
    r"\b(?:goodbye|bye(?: bye)?|see (?:you|ya)|take care|"
    r"have a (?:good|great|nice) (?:day|one)|"
    r"that(?:'s| will be| is) all(?: for today)?|no thanks|thanks,? (?:that'?s|that is) (?:all|it)|"
    r"i'?m (?:all )?set)\b",
    re.I,
)
_ESCALATION_RE = re.compile(
    r"\b(?:(?:speak|talk|transfer)\s+(?:to|me\s+to|with)\s+)?"
    r"(?:a\s+|the\s+)?(?:human|real person|actual person|live (?:agent|person)|"
    r"manager|supervisor|representative|agent)\b",
    re.I,
)
_TRANSFER_PROMISE_RE = re.compile(
    r"\btransfer(?:ring)?\s+(?:you|this call|the call)\b"
    r"|\b(?:connect|connecting)\s+(?:you|this call|the call)\b"
    r"|\bput(?:ting)?\s+you\s+through\b"
    r"|\bhold\s+while\s+I\s+(?:transfer|connect|route)\b",
    re.I,
)


def behavior_flags(call: CallRevision) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    loops = _loop_detected(call)
    if loops:
        out.append({"kind": "loop_detected", "repeats": loops})
    repeats = _user_repeated(call)
    if repeats:
        out.append({"kind": "user_repeated", "text": repeats})
    if _farewell_missed(call):
        out.append({"kind": "farewell_missed"})
    if _escalation_unmet(call):
        out.append({"kind": "escalation_unmet"})
    monologue = _monologue(call)
    if monologue:
        out.append(monologue)
    if _barge_in_no_recovery(call):
        out.append({"kind": "barge_in_no_recovery"})
    gap = _dead_air_before_hangup(call)
    if gap is not None:
        out.append({"kind": "dead_air_hangup", "gap_seconds": round(gap, 1)})
    return out


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _loop_detected(call: CallRevision) -> int | None:
    """Same agent utterance three or more times is a stuck loop."""
    counts: dict[str, int] = {}
    for turn in call.agent_turns():
        text = _normalized(turn.text)
        if len(text) < 8:
            continue
        counts[text] = counts.get(text, 0) + 1
    widest = max(counts.values(), default=0)
    return widest if widest >= 3 else None


def _user_repeated(call: CallRevision) -> str | None:
    """Same caller utterance twice with an agent turn between: not acted on."""
    first_at: dict[str, int] = {}
    for turn in call.turns:
        if turn.speaker is not Speaker.USER or not turn.text:
            continue
        text = _normalized(turn.text)
        if len(text) < USER_REPEAT_MIN_CHARS:
            continue
        if text not in first_at:
            first_at[text] = turn.index
            continue
        first = first_at[text]
        if any(first < agent.index < turn.index for agent in call.agent_turns()):
            return turn.text
    return None


def _farewell_missed(call: CallRevision) -> bool:
    """Caller said goodbye; the agent kept talking without a caller reply."""
    last_farewell = None
    for turn in call.user_turns():
        if turn.text and _FAREWELL_STRONG_RE.search(turn.text):
            last_farewell = turn
    if last_farewell is None:
        return False
    trailing = [turn for turn in call.agent_turns() if turn.index > last_farewell.index]
    return len(trailing) >= 2


def _escalation_unmet(call: CallRevision) -> bool:
    """Caller asked for a human; the call did not end in a transfer."""
    if not any(_ESCALATION_RE.search(turn.text) for turn in call.user_turns() if turn.text):
        return False
    return call.hangup is None or call.hangup.reason is not HangupReason.TRANSFER


def _monologue(call: CallRevision) -> dict[str, object] | None:
    """Talk-time ratio or a single agent turn that never yields the floor."""
    agent_seconds = _speech_seconds(call.agent_turns())
    user_seconds = _speech_seconds(call.user_turns())
    longest = 0.0
    for turn in call.agent_turns():
        if turn.started_at and turn.ended_at:
            longest = max(longest, (turn.ended_at - turn.started_at).total_seconds())
    if longest > AGENT_MONOLOGUE_SECONDS:
        return {"kind": "monologue", "longest_turn_seconds": round(longest, 1)}
    if user_seconds >= MIN_USER_SPEECH_SECONDS and agent_seconds > TALK_RATIO * user_seconds:
        return {
            "kind": "monologue",
            "ratio": round(agent_seconds / user_seconds, 1),
        }
    return None


def _barge_in_no_recovery(call: CallRevision) -> bool:
    """Interrupted agent speech followed by agent speech again: restart without caller input."""
    turns = call.turns
    for prev, nxt in zip(turns, turns[1:], strict=False):
        if prev.speaker is Speaker.AGENT and prev.interrupted and nxt.speaker is Speaker.AGENT:
            return True
    return False


def _dead_air_before_hangup(call: CallRevision) -> float | None:
    """Caller waited in silence, then hung up. The classic abandonment signature."""
    if call.hangup is None or call.hangup.party is not HangupParty.USER:
        return None
    if call.hangup.reason in {HangupReason.SILENCE_TIMEOUT, HangupReason.INACTIVITY}:
        return None  # provider already reported the silence; tier1 flags it as silence
    ended = call.ended_at
    last_end: datetime | None = None
    for turn in call.turns:
        if turn.ended_at and (last_end is None or turn.ended_at > last_end):
            last_end = turn.ended_at
    if ended is None or last_end is None:
        return None
    gap = (ended - last_end).total_seconds()
    return gap if gap > DEAD_AIR_BEFORE_HANGUP_SECONDS else None


def _speech_seconds(turns: Sequence[Turn]) -> float:
    total = 0.0
    for turn in turns:
        if turn.started_at and turn.ended_at:
            total += max(0.0, (turn.ended_at - turn.started_at).total_seconds())
    return total
