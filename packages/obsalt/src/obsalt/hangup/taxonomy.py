"""Provider-agnostic hangup vocabulary and loss scoring.

Provider mapping tables live in plugins and are CI-checked against published
enums. Prefix rules for Vapi modern families are applied there so
``*-voice-failed`` resolves to TTS before a provider-name token steals it.
"""

from __future__ import annotations

from obsalt.domain.enums import HangupParty, HangupReason
from obsalt.domain.models import CallRevision

_NEGATIVE = (
    "nevermind",
    "never mind",
    "this is useless",
    "speak to a human",
    "real person",
    "cancel",
    "stupid",
    "waste of time",
    "wrong number",
    "stop calling",
    "don't call",
    "do not call",
    "refund",
    "supervisor",
    "manager",
    "frustrated",
    "ridiculous",
)


def customer_loss_score(revision: CallRevision) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    hangup = revision.hangup
    if hangup is None:
        return 0.0, reasons
    if hangup.reason is HangupReason.USER_HANGUP:
        score += 0.45
        reasons.append("user_hangup")
    elif hangup.reason in {HangupReason.INACTIVITY, HangupReason.SILENCE_TIMEOUT}:
        score += 0.25
        reasons.append("silence_or_inactivity")
    elif hangup.reason in {
        HangupReason.ERROR_STT,
        HangupReason.ERROR_LLM,
        HangupReason.ERROR_TTS,
        HangupReason.ERROR_TOOL,
    }:
        score += 0.35
        reasons.append("pipeline_error")

    last_user = ""
    for turn in reversed(revision.turns):
        if turn.speaker.value == "user" and turn.text:
            last_user = turn.text.lower()
            break
    if any(token in last_user for token in _NEGATIVE):
        score += 0.25
        reasons.append("negative_last_utterance")

    failed_tools = [t for t in revision.tools if t.status.value in {"error", "timeout"}]
    if failed_tools:
        score += 0.2
        reasons.append("tool_failure")

    last_e2e = None
    for sample in reversed(revision.stage_measurements):
        if sample.stage.value in {"e2e", "ttfa"}:
            last_e2e = sample.value_ms
            break
    if last_e2e is not None and last_e2e > 1500:
        score += 0.1
        reasons.append("slow_last_turn")

    duration = revision.lifecycle.duration_ms or 0
    if hangup.reason is HangupReason.USER_HANGUP and 0 < duration < 15_000:
        score += 0.15
        reasons.append("early_user_hangup")

    return min(1.0, round(score, 3)), reasons


def party_for_reason(reason: HangupReason) -> HangupParty:
    if reason is HangupReason.USER_HANGUP:
        return HangupParty.USER
    if reason in {HangupReason.AGENT_HANGUP, HangupReason.TRANSFER, HangupReason.COMPLETED}:
        return HangupParty.AGENT
    return HangupParty.SYSTEM
