"""Vapi endedReason mapping.

The denominator is the vendored schema enum. Prefix families cover the modern
``call.in-progress.error-vapifault-*``, ``call.start.error-*``, ``call.ringing.*``,
and ``call.ending.*`` codes. ``*-voice-failed`` is classified as TTS *before*
a provider-name token can steal it for STT/LLM.
"""

from __future__ import annotations

from obsalt.domain.enums import HangupParty, HangupReason

EXPLICIT: dict[str, tuple[HangupReason, HangupParty]] = {
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


def classify_ended_reason(code: str | None) -> tuple[HangupReason, HangupParty]:
    if not code:
        return HangupReason.UNKNOWN, HangupParty.UNKNOWN
    hit = EXPLICIT.get(code) or EXPLICIT.get(code.lower())
    if hit:
        return hit
    return _prefix(code.lower())


def _prefix(code: str) -> tuple[HangupReason, HangupParty]:
    # Voice/TTS first so *-voice-failed is not stolen by a provider-name token.
    if "voice-failed" in code or "-voice-" in code and "failed" in code:
        return HangupReason.ERROR_TTS, HangupParty.SYSTEM
    if "transcriber-failed" in code or "stt" in code and "failed" in code:
        return HangupReason.ERROR_STT, HangupParty.SYSTEM
    if "llm-failed" in code or "-llm-" in code and "failed" in code:
        return HangupReason.ERROR_LLM, HangupParty.SYSTEM
    if "pipeline-error" in code or "error-vapifault" in code or code.startswith("call.in-progress.error"):
        if any(tok in code for tok in ("voice", "tts", "eleven", "cartesia")):
            return HangupReason.ERROR_TTS, HangupParty.SYSTEM
        if any(tok in code for tok in ("transcriber", "stt", "deepgram", "asr")):
            return HangupReason.ERROR_STT, HangupParty.SYSTEM
        if any(tok in code for tok in ("llm", "openai", "anthropic", "model")):
            return HangupReason.ERROR_LLM, HangupParty.SYSTEM
        if "tool" in code or "function" in code:
            return HangupReason.ERROR_TOOL, HangupParty.SYSTEM
        return HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM
    if code.startswith("call.start.error") or code.startswith("call.ringing"):
        if "busy" in code:
            return HangupReason.BUSY, HangupParty.USER
        if "no-answer" in code or "did-not-answer" in code:
            return HangupReason.NO_ANSWER, HangupParty.USER
        return HangupReason.DIAL_FAILED, HangupParty.SYSTEM
    if code.startswith("call.ending"):
        if "customer" in code or "user" in code:
            return HangupReason.USER_HANGUP, HangupParty.USER
        if "assistant" in code:
            return HangupReason.AGENT_HANGUP, HangupParty.AGENT
        return HangupReason.COMPLETED, HangupParty.SYSTEM
    if any(tok in code for tok in ("twilio", "vonage", "telnyx", "sip")):
        return HangupReason.ERROR_TELEPHONY, HangupParty.SYSTEM
    return HangupReason.UNKNOWN, HangupParty.UNKNOWN
