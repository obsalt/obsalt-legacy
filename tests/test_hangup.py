from __future__ import annotations

from obsalt.analysis.hangup import classify_provider_reason, mapped_count
from obsalt.domain.enums import HangupParty, HangupReason


def test_voice_failed_is_tts_before_provider_name() -> None:
    reason, party = classify_provider_reason("vapi", "pipeline-error-deepgram-voice-failed")
    assert reason == HangupReason.ERROR_TTS
    assert party == HangupParty.SYSTEM
    reason, _ = classify_provider_reason("vapi", "call.in-progress.error-vapifault-eleven-labs-voice-failed")
    assert reason == HangupReason.ERROR_TTS


def test_transcriber_is_stt() -> None:
    reason, _ = classify_provider_reason("vapi", "pipeline-error-deepgram-transcriber-failed")
    assert reason == HangupReason.ERROR_STT


def test_llm_prefix() -> None:
    reason, _ = classify_provider_reason("vapi", "pipeline-error-openai-llm-failed")
    assert reason == HangupReason.ERROR_LLM


def test_explicit_customer_hangup() -> None:
    reason, party = classify_provider_reason("vapi", "customer-ended-call")
    assert reason == HangupReason.USER_HANGUP
    assert party == HangupParty.USER


def test_retell_asr() -> None:
    reason, _ = classify_provider_reason("retell", "error_asr")
    assert reason == HangupReason.ERROR_STT


def test_mapped_count_helper() -> None:
    mapped, total = mapped_count(["customer-ended-call", "not-a-real-code-xyz"], "vapi")
    assert total == 2
    assert mapped == 1
