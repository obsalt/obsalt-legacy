from __future__ import annotations

from obsalt.domain.enums import HangupParty, HangupReason, Speaker
from obsalt.domain.models import CanonicalCall, Hangup, Turn

# Explicit provider code → taxonomy. Prefix rules fill gaps.
VAPI_REASONS: dict[str, tuple[HangupReason, HangupParty]] = {
    "customer-ended-call": (HangupReason.USER_HANGUP, HangupParty.USER),
    "hangup": (HangupReason.USER_HANGUP, HangupParty.USER),
    "customer-ended-call-before-warm-transfer": (HangupReason.USER_HANGUP, HangupParty.USER),
    "assistant-ended-call": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "assistant-ended-call-after-message-spoken": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "assistant-ended-call-with-hangup-task": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "assistant-said-end-call-phrase": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "assistant-forwarded-call": (HangupReason.TRANSFER, HangupParty.AGENT),
    "assistant-forwarded-call-after-message-spoken": (HangupReason.TRANSFER, HangupParty.AGENT),
    "voicemail": (HangupReason.VOICEMAIL, HangupParty.SYSTEM),
    "voicemail-reached": (HangupReason.VOICEMAIL, HangupParty.SYSTEM),
    "silence-timed-out": (HangupReason.SILENCE_TIMEOUT, HangupParty.SYSTEM),
    "exceeded-max-duration": (HangupReason.MAX_DURATION, HangupParty.SYSTEM),
    "max-duration-exceeded": (HangupReason.MAX_DURATION, HangupParty.SYSTEM),
    "customer-busy": (HangupReason.BUSY, HangupParty.USER),
    "customer-did-not-answer": (HangupReason.NO_ANSWER, HangupParty.USER),
    "manually-canceled": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "vonage-completed": (HangupReason.COMPLETED, HangupParty.SYSTEM),
}

RETELL_REASONS: dict[str, tuple[HangupReason, HangupParty]] = {
    "user_hangup": (HangupReason.USER_HANGUP, HangupParty.USER),
    "agent_hangup": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "call_transfer": (HangupReason.TRANSFER, HangupParty.AGENT),
    "transfer_bridged": (HangupReason.TRANSFER, HangupParty.AGENT),
    "transfer_cancelled": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "voicemail_reached": (HangupReason.VOICEMAIL, HangupParty.SYSTEM),
    "ivr_reached": (HangupReason.COMPLETED, HangupParty.SYSTEM),
    "inactivity": (HangupReason.INACTIVITY, HangupParty.SYSTEM),
    "max_duration_reached": (HangupReason.MAX_DURATION, HangupParty.SYSTEM),
    "concurrency_limit_reached": (HangupReason.CONCURRENCY, HangupParty.SYSTEM),
    "dial_busy": (HangupReason.BUSY, HangupParty.USER),
    "dial_failed": (HangupReason.DIAL_FAILED, HangupParty.SYSTEM),
    "dial_no_answer": (HangupReason.NO_ANSWER, HangupParty.USER),
    "invalid_destination": (HangupReason.DIAL_FAILED, HangupParty.SYSTEM),
    "telephony_provider_permission_denied": (HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM),
    "telephony_provider_unavailable": (HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM),
    "sip_routing_error": (HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM),
    "marked_as_spam": (HangupReason.SPAM, HangupParty.SYSTEM),
    "scam_detected": (HangupReason.SPAM, HangupParty.SYSTEM),
    "user_declined": (HangupReason.USER_HANGUP, HangupParty.USER),
    "error_llm_websocket_open": (HangupReason.ERROR_LLM, HangupParty.SYSTEM),
    "error_llm_websocket_lost_connection": (HangupReason.ERROR_LLM, HangupParty.SYSTEM),
    "error_llm_websocket_runtime": (HangupReason.ERROR_LLM, HangupParty.SYSTEM),
    "error_llm_websocket_corrupt_payload": (HangupReason.ERROR_LLM, HangupParty.SYSTEM),
    "error_no_audio_received": (HangupReason.ERROR_TTS, HangupParty.SYSTEM),
    "error_asr": (HangupReason.ERROR_STT, HangupParty.SYSTEM),
    "error_retell": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
    "error_unknown": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
    "error_user_not_joined": (HangupReason.NO_ANSWER, HangupParty.USER),
    "registered_call_timeout": (HangupReason.NO_ANSWER, HangupParty.SYSTEM),
    "no_valid_payment": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "manual_stopped": (HangupReason.CANCELLED, HangupParty.SYSTEM),
}

BLAND_DISPOSITIONS: dict[str, tuple[HangupReason, HangupParty]] = {
    "COMPLETED_ACTION": (HangupReason.COMPLETED, HangupParty.AGENT),
    "NO_ANSWER": (HangupReason.NO_ANSWER, HangupParty.USER),
    "NO_CONTACT": (HangupReason.NO_ANSWER, HangupParty.USER),
    "BUSY": (HangupReason.BUSY, HangupParty.USER),
    "VOICEMAIL": (HangupReason.VOICEMAIL, HangupParty.SYSTEM),
    "FAILED": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
    "CANCELLED": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "CANCELED": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "TRANSFERRED": (HangupReason.TRANSFER, HangupParty.AGENT),
    "TRANSFER": (HangupReason.TRANSFER, HangupParty.AGENT),
    "UNKNOWN": (HangupReason.UNKNOWN, HangupParty.UNKNOWN),
}

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


def _prefix_vapi(code: str) -> tuple[HangupReason, HangupParty] | None:
    lowered = code.lower()
    if "pipeline-error" in lowered or lowered.startswith("pipeline-error"):
        if any(token in lowered for token in ("stt", "transcriber", "deepgram", "asr")):
            return HangupReason.ERROR_STT, HangupParty.SYSTEM
        if any(token in lowered for token in ("llm", "openai", "anthropic", "model")):
            return HangupReason.ERROR_LLM, HangupParty.SYSTEM
        if any(token in lowered for token in ("tts", "voice", "eleven", "cartesia")):
            return HangupReason.ERROR_TTS, HangupParty.SYSTEM
        if "tool" in lowered or "function" in lowered:
            return HangupReason.ERROR_TOOL, HangupParty.SYSTEM
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if "twilio" in lowered or "vonage" in lowered or "telnyx" in lowered or "sip" in lowered:
        return HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM
    return None


def classify_provider_reason(provider: str, reason: str | None) -> tuple[HangupReason, HangupParty]:
    code = (reason or "").strip()
    if not code:
        return HangupReason.UNKNOWN, HangupParty.UNKNOWN
    key = code
    provider = provider.lower()
    table: dict[str, tuple[HangupReason, HangupParty]]
    if provider == "vapi":
        table = VAPI_REASONS
        hit = table.get(key) or table.get(key.lower())
        if hit:
            return hit
        prefixed = _prefix_vapi(key)
        if prefixed:
            return prefixed
    elif provider == "retell":
        table = RETELL_REASONS
        hit = table.get(key) or table.get(key.lower())
        if hit:
            return hit
        if key.lower().startswith("error_llm"):
            return HangupReason.ERROR_LLM, HangupParty.SYSTEM
        if key.lower().startswith("error_asr") or "asr" in key.lower():
            return HangupReason.ERROR_STT, HangupParty.SYSTEM
        if key.lower().startswith("error_"):
            return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    elif provider == "bland":
        table = BLAND_DISPOSITIONS
        hit = table.get(key) or table.get(key.upper())
        if hit:
            return hit
    native = {
        "user_hangup": (HangupReason.USER_HANGUP, HangupParty.USER),
        "agent_hangup": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
        "completed": (HangupReason.COMPLETED, HangupParty.AGENT),
        "error": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
    }
    return native.get(key.lower(), (HangupReason.UNKNOWN, HangupParty.UNKNOWN))


def last_of(turns: list[Turn], speaker: Speaker) -> Turn | None:
    for turn in reversed(turns):
        if turn.speaker == speaker:
            return turn
    return None


def annotate_hangup(call: CanonicalCall, hangup: Hangup) -> Hangup:
    user = last_of(call.turns, Speaker.USER)
    agent = last_of(call.turns, Speaker.AGENT)
    last = call.turns[-1] if call.turns else None
    hangup.last_speaker = last.speaker if last else None
    hangup.last_user_text = user.text if user else None
    hangup.last_agent_text = agent.text if agent else None
    return hangup


def customer_loss_score(call: CanonicalCall) -> tuple[float, list[str]]:
    """Score how likely this hangup lost a customer. 0–1, explainable."""
    score = 0.0
    reasons: list[str] = []
    hangup = call.hangup
    if hangup is None:
        return 0.0, reasons

    if hangup.reason == HangupReason.USER_HANGUP:
        score += 0.45
        reasons.append("user_hangup")
    elif hangup.reason in {HangupReason.INACTIVITY, HangupReason.SILENCE_TIMEOUT}:
        score += 0.25
        reasons.append("silence_or_inactivity")
    elif hangup.reason in {HangupReason.ERROR_STT, HangupReason.ERROR_LLM, HangupReason.ERROR_TTS, HangupReason.ERROR_TOOL}:
        score += 0.35
        reasons.append("pipeline_error")

    last_user = (hangup.last_user_text or "").lower()
    if any(token in last_user for token in _NEGATIVE):
        score += 0.25
        reasons.append("negative_last_utterance")

    if call.hallucinations:
        score += min(0.2, 0.08 * len(call.hallucinations))
        reasons.append("hallucination")

    failed_tools = [t for t in call.tools if t.status.value in {"error", "timeout"}]
    if failed_tools:
        score += 0.2
        reasons.append("tool_failure")

    last_turn_e2e = None
    for sample in reversed(call.latency_samples):
        if sample.component.value in {"e2e", "ttfa"}:
            last_turn_e2e = sample.duration_ms
            break
    if last_turn_e2e is not None and last_turn_e2e > 1500:
        score += 0.1
        reasons.append("slow_last_turn")

    duration = call.duration_ms or 0
    if hangup.reason == HangupReason.USER_HANGUP and 0 < duration < 15_000:
        score += 0.15
        reasons.append("early_user_hangup")

    return min(1.0, round(score, 3)), reasons
