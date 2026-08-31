from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.hangup import (
    ENDING_NOT_REPORTED,
    ENDING_UNROOTED,
    classify_provider_reason,
    hangup_bucket,
    mapped_count,
)
from obsalt.domain.enums import CallStatus, HangupParty, HangupReason
from obsalt.domain.models import CallRevision, Hangup


def test_voice_failed_is_tts_before_provider_name() -> None:
    reason, party = classify_provider_reason("vapi", "pipeline-error-deepgram-voice-failed")
    assert reason == HangupReason.ERROR_TTS
    assert party == HangupParty.SYSTEM
    reason, _ = classify_provider_reason(
        "vapi", "call.in-progress.error-vapifault-eleven-labs-voice-failed"
    )
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


def test_hangup_bucket_does_not_call_missing_unknown() -> None:
    base = dict(
        org_id="o",
        call_id="c1",
        revision="r1",
        source="openai_realtime",
        source_call_id="s1",
        started_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    missing = CallRevision(**base)
    assert hangup_bucket(missing) == ENDING_NOT_REPORTED
    unrooted = CallRevision(**base, rooted=False, status=CallStatus.UNROOTED)
    assert hangup_bucket(unrooted) == ENDING_UNROOTED
    unmapped = CallRevision(
        **base,
        hangup=Hangup(
            reason=HangupReason.UNKNOWN,
            party=HangupParty.UNKNOWN,
            provider_code="mystery",
        ),
    )
    assert hangup_bucket(unmapped) == HangupReason.UNKNOWN.value
    blank_unknown = CallRevision(
        **base,
        hangup=Hangup(reason=HangupReason.UNKNOWN, party=HangupParty.UNKNOWN, provider_code=""),
    )
    assert hangup_bucket(blank_unknown) == ENDING_NOT_REPORTED


def test_mapped_count_helper() -> None:
    mapped, total = mapped_count(["customer-ended-call", "not-a-real-code-xyz"], "vapi")
    assert total == 2
    assert mapped == 1
