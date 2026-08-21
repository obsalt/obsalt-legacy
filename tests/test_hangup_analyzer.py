from __future__ import annotations

from obsalt.domain.enums import HangupParty, HangupReason, LatencyComponent, Provider, Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, Hangup, LatencySample, ToolInvocation, Turn
from obsalt.hangup.analyzer import HangupAnalyzer
from obsalt.hangup.taxonomy import customer_loss_score
from obsalt.util import call_id_for


def _call(cid: str, hangup: Hangup, **kwargs) -> CanonicalCall:
    call = CanonicalCall(
        id=call_id_for("org", "vapi", cid),
        org_id="org",
        provider=Provider.VAPI,
        provider_call_id=cid,
        agent_id="support",
        hangup=hangup,
        duration_ms=8_000,
        turns=[
            Turn(index=0, speaker=Speaker.AGENT, text="How can I help?"),
            Turn(index=1, speaker=Speaker.USER, text=hangup.last_user_text or ""),
        ],
    )
    for key, value in kwargs.items():
        setattr(call, key, value)
    hangup.last_speaker = Speaker.USER
    return call


def test_loss_score_penalizes_early_frustrated_hangup() -> None:
    hangup = Hangup(
        reason=HangupReason.USER_HANGUP,
        party=HangupParty.USER,
        provider_reason="customer-ended-call",
        last_user_text="This is useless. I want a refund.",
    )
    call = _call(
        "lost",
        hangup,
        tools=[ToolInvocation(id="t", name="lookup_order", status=ToolStatus.ERROR)],
        latency_samples=[LatencySample(component=LatencyComponent.E2E, duration_ms=1800)],
        duration_ms=8_000,
    )
    score, reasons = customer_loss_score(call)
    assert score >= 0.7
    assert "user_hangup" in reasons
    assert "negative_last_utterance" in reasons
    assert "tool_failure" in reasons


def test_clusters_refund_theme_and_picks_lost_customer() -> None:
    analyzer = HangupAnalyzer()
    lost = _call(
        "lost",
        Hangup(
            reason=HangupReason.USER_HANGUP,
            party=HangupParty.USER,
            provider_reason="customer-ended-call",
            last_user_text="I want a refund on this charge, this is useless.",
            loss_score=0.8,
        ),
    )
    quiet = _call(
        "quiet",
        Hangup(
            reason=HangupReason.AGENT_HANGUP,
            party=HangupParty.AGENT,
            provider_reason="assistant-ended-call",
            last_user_text="Thanks, bye.",
            loss_score=0.1,
        ),
    )
    analyzer.analyze_call(lost)
    analyzer.analyze_call(quiet)
    clusters = analyzer.cluster([lost, quiet])
    assert clusters
    refund = next(c for c in clusters if c.reason == "user_hangup")
    assert refund.lost_customer_call_id == lost.id
    assert refund.last_utterance_theme in {"refund_billing", "cancel"} or "refund" in refund.last_utterance_theme
