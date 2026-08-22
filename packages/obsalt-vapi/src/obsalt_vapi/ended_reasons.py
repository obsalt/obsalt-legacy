"""Pinned Vapi endedReason enum extracted from the vendored schema revision.

CI reports mapped_count / current_enum_count and fails below 0.95.
"""

from __future__ import annotations

from obsalt_vapi.hangup import EXPLICIT, classify_ended_reason

# Vendored 2026-08-22 snapshot of published / documented families.
VAPI_ENDED_REASONS = sorted(
    {
        *EXPLICIT.keys(),
        "pipeline-error-eleven-labs-voice-failed",
        "pipeline-error-deepgram-transcriber-failed",
        "pipeline-error-openai-llm-failed",
        "call.in-progress.error-vapifault-openai-llm-failed",
        "call.in-progress.error-vapifault-elevenlabs-voice-failed",
        "call.in-progress.error-vapifault-deepgram-transcriber-failed",
        "call.start.error-get-transport",
        "call.start.error-vapifault-worker-not-available",
        "call.ringing.sip-inbound-caller-hungup-before-connected",
        "call.ending.customer-ended-call",
        "call.ending.assistant-ended-call",
        "twilio-failed-to-connect-call",
        "vonage-disconnected",
        "sip-telephony-provider-failed-to-connect-call",
    }
)


def coverage() -> tuple[int, int, list[str]]:
    mapped = 0
    unmapped: list[str] = []
    for code in VAPI_ENDED_REASONS:
        reason, _party = classify_ended_reason(code)
        if reason.value != "unknown":
            mapped += 1
        else:
            unmapped.append(code)
    return mapped, len(VAPI_ENDED_REASONS), unmapped
