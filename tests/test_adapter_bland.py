from __future__ import annotations

from obsalt.adapters.bland import BlandAdapter
from obsalt.domain.enums import HangupReason, LatencyComponent, Speaker
from tests.conftest import load_fixture


def test_bland_post_call_transfer() -> None:
    result = BlandAdapter().parse(load_fixture("bland_post_call.json"), org_id="org-1")
    assert result is not None and result.terminal
    call = result.call
    assert call.provider_call_id == "bland-call-transfer-1"
    assert call.hangup and call.hangup.reason == HangupReason.TRANSFER
    assert call.duration_ms == 30_000
    assert any(t.speaker == Speaker.USER and "human" in t.text.lower() for t in call.turns)
    assert any(t.name.lower().startswith("transferred") or "transfer" in t.name.lower() for t in call.tools)
    e2e = [s for s in call.latency_samples if s.component == LatencyComponent.E2E]
    assert e2e
    assert e2e[0].duration_ms == 1200


def test_bland_live_latency_event() -> None:
    result = BlandAdapter().parse(
        {"call_id": "live-1", "category": "latency", "message": "LLM: 266ms", "log_level": "performance"},
        org_id="org-1",
    )
    assert result is not None
    assert result.terminal is False
    samples = result.call.latency_samples
    assert samples[0].component == LatencyComponent.LLM
    assert samples[0].duration_ms == 266
    assert samples[0].ttft_ms == 266
