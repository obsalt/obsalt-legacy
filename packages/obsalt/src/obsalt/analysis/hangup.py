"""Hangup classification. Mapping tables are generated from pinned provider enums."""

from __future__ import annotations

from obsalt.domain.enums import HangupParty, HangupReason
from obsalt.domain.models import CallRevision, Hangup

# Explicit high-value codes. Prefix rules cover the rest of a pinned enum.
EXPLICIT: dict[tuple[str, str], tuple[HangupReason, HangupParty]] = {}

VAPI_EXPLICIT: dict[str, tuple[HangupReason, HangupParty]] = {
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

RETELL_EXPLICIT: dict[str, tuple[HangupReason, HangupParty]] = {
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


def classify_provider_reason(provider: str, reason: str | None) -> tuple[HangupReason, HangupParty]:
    code = (reason or "").strip()
    if not code:
        return HangupReason.UNKNOWN, HangupParty.UNKNOWN
    provider = provider.lower()
    if provider == "vapi":
        hit = VAPI_EXPLICIT.get(code) or VAPI_EXPLICIT.get(code.lower())
        if hit:
            return hit
        return _prefix_vapi(code)
    if provider == "retell":
        hit = RETELL_EXPLICIT.get(code) or RETELL_EXPLICIT.get(code.lower())
        if hit:
            return hit
        lowered = code.lower()
        if lowered.startswith("error_llm"):
            return HangupReason.ERROR_LLM, HangupParty.SYSTEM
        if "asr" in lowered:
            return HangupReason.ERROR_STT, HangupParty.SYSTEM
        if lowered.startswith("error_"):
            return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    native = {
        "user_hangup": (HangupReason.USER_HANGUP, HangupParty.USER),
        "agent_hangup": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
        "completed": (HangupReason.COMPLETED, HangupParty.AGENT),
        "error": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
    }
    return native.get(code.lower(), (HangupReason.UNKNOWN, HangupParty.UNKNOWN))


def _prefix_vapi(code: str) -> tuple[HangupReason, HangupParty]:
    """Prefix rules. `*-voice-failed` is TTS *before* provider-name tokens (Deepgram Aura, etc.)."""
    lowered = code.lower()
    if (
        lowered.endswith("voice-failed")
        or "-voice-failed" in lowered
        or "tts" in lowered
        and "failed" in lowered
    ):
        return HangupReason.ERROR_TTS, HangupParty.SYSTEM
    if any(
        tok in lowered
        for tok in (
            "transcriber",
            "stt-",
            "-stt",
            "deepgram-transcriber",
            "asr",
            "speechmatics",
            "gladia",
            "assembly",
        )
    ):
        return HangupReason.ERROR_STT, HangupParty.SYSTEM
    if any(
        tok in lowered for tok in ("llm", "openai", "anthropic", "groq", "together", "model-failed")
    ):
        return HangupReason.ERROR_LLM, HangupParty.SYSTEM
    if "tool" in lowered or "function" in lowered:
        return HangupReason.ERROR_TOOL, HangupParty.SYSTEM
    if any(tok in lowered for tok in ("twilio", "vonage", "telnyx", "sip", "telephony")):
        return HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM
    if (
        lowered.startswith("call.start.error")
        or lowered.startswith("call-start-error")
        or lowered.startswith("assistant-request")
        or lowered.startswith("call.start.")
    ):
        if "busy" in lowered:
            return HangupReason.BUSY, HangupParty.USER
        return HangupReason.DIAL_FAILED, HangupParty.SYSTEM
    if lowered.startswith("call.ringing.") or lowered.startswith("call.ending"):
        if "transfer" in lowered:
            return HangupReason.TRANSFER, HangupParty.AGENT
        return HangupReason.COMPLETED, HangupParty.SYSTEM
    if "warm-transfer" in lowered or (
        lowered.startswith("call.in-progress.error-transfer") or "transfer-failed" in lowered
    ):
        return HangupReason.TRANSFER, HangupParty.AGENT
    if "microphone" in lowered:
        return HangupReason.NO_ANSWER, HangupParty.USER
    if "forwarding" in lowered and "busy" in lowered:
        return HangupReason.BUSY, HangupParty.USER
    if "forwarding" in lowered and "no-answer" in lowered:
        return HangupReason.NO_ANSWER, HangupParty.USER
    if (
        lowered.startswith("assistant-not")
        or lowered.startswith("worker-")
        or "pipeline-ws" in lowered
    ):
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if "scheduled-call-deleted" in lowered or lowered in {"call.deleted", "call.delete.failed"}:
        return HangupReason.CANCELLED, HangupParty.SYSTEM
    if (
        lowered.startswith("assistant-speaks")
        or lowered.startswith("assistant-waits")
        or lowered.startswith("assistant-join")
    ):
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if "did-not-receive-customer-audio" in lowered or "customer-audio" in lowered:
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if "pipeline-error" in lowered or "vapifault" in lowered or "providerfault" in lowered:
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if "silence" in lowered:
        return HangupReason.SILENCE_TIMEOUT, HangupParty.SYSTEM
    if "voicemail" in lowered:
        return HangupReason.VOICEMAIL, HangupParty.SYSTEM
    if "customer-ended" in lowered or lowered.endswith("user-hangup"):
        return HangupReason.USER_HANGUP, HangupParty.USER
    if "assistant-ended" in lowered or "assistant-said-end" in lowered:
        return HangupReason.AGENT_HANGUP, HangupParty.AGENT
    return HangupReason.UNKNOWN, HangupParty.UNKNOWN


def mapped_count(codes: list[str], provider: str) -> tuple[int, int]:
    mapped = 0
    for code in codes:
        reason, _party = classify_provider_reason(provider, code)
        if reason is not HangupReason.UNKNOWN:
            mapped += 1
    return mapped, len(codes)


def customer_loss_score(call: CallRevision) -> tuple[float, list[str]]:
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
    elif hangup.reason in {
        HangupReason.ERROR_STT,
        HangupReason.ERROR_LLM,
        HangupReason.ERROR_TTS,
        HangupReason.ERROR_TOOL,
    }:
        score += 0.35
        reasons.append("pipeline_error")
    last_user = ""
    for turn in reversed(call.turns):
        if turn.speaker.value == "user":
            last_user = (turn.text or "").lower()
            break
    if any(token in last_user for token in _NEGATIVE):
        score += 0.25
        reasons.append("negative_last_utterance")
    failed_tools = [t for t in call.tools if t.status.value in {"error", "timeout"}]
    if failed_tools:
        score += 0.2
        reasons.append("tool_failure")
    last_e2e = None
    for sample in reversed(call.stage_measurements):
        if sample.stage.value in {"e2e", "ttfa"}:
            last_e2e = sample.value_ms
            break
    if last_e2e is not None and last_e2e > 1500:
        score += 0.1
        reasons.append("slow_last_turn")
    duration = call.duration_ms or 0
    if hangup.reason == HangupReason.USER_HANGUP and 0 < duration < 15_000:
        score += 0.15
        reasons.append("early_user_hangup")
    return min(1.0, round(score, 3)), reasons


def annotate_hangup(call: CallRevision, hangup: Hangup) -> Hangup:
    last = call.turns[-1] if call.turns else None
    hangup.last_speaker = last.speaker if last else None
    return hangup
