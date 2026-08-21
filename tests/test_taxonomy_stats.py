from __future__ import annotations

from obsalt.hangup.taxonomy import classify_provider_reason
from obsalt.latency.stats import percentile, summarize
from obsalt.domain.enums import HangupParty, HangupReason, LatencyComponent


def test_percentile_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10
    assert percentile(values, 100) == 50
    assert percentile(values, 50) == 30


def test_summarize_empty() -> None:
    stats = summarize(LatencyComponent.STT, [])
    assert stats.count == 0
    assert stats.p50_ms is None


def test_vapi_hangup_mapping() -> None:
    reason, party = classify_provider_reason("vapi", "customer-ended-call")
    assert reason == HangupReason.USER_HANGUP
    assert party == HangupParty.USER
    reason, party = classify_provider_reason("vapi", "pipeline-error-openai-llm-failed")
    assert reason == HangupReason.ERROR_LLM
    reason, party = classify_provider_reason("vapi", "pipeline-error-eleven-labs-voice-failed")
    assert reason == HangupReason.ERROR_TTS
    reason, party = classify_provider_reason("vapi", "silence-timed-out")
    assert reason == HangupReason.SILENCE_TIMEOUT


def test_retell_hangup_mapping() -> None:
    reason, party = classify_provider_reason("retell", "error_asr")
    assert reason == HangupReason.ERROR_STT
    reason, party = classify_provider_reason("retell", "inactivity")
    assert reason == HangupReason.INACTIVITY
    reason, party = classify_provider_reason("retell", "call_transfer")
    assert reason == HangupReason.TRANSFER


def test_bland_hangup_mapping() -> None:
    reason, party = classify_provider_reason("bland", "VOICEMAIL")
    assert reason == HangupReason.VOICEMAIL
    reason, party = classify_provider_reason("bland", "NO_ANSWER")
    assert reason == HangupReason.NO_ANSWER
