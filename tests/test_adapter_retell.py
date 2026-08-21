from __future__ import annotations

from obsalt.adapters.retell import RetellAdapter
from obsalt.domain.enums import HangupReason, LatencyComponent, ToolStatus
from tests.conftest import load_fixture


def test_retell_call_ended_uses_raw_latency_values() -> None:
    result = RetellAdapter().parse(load_fixture("retell_call_ended.json"), org_id="org-1")
    assert result is not None and result.terminal
    call = result.call
    assert call.provider_call_id == "retell-call-happy-1"
    assert call.hangup and call.hangup.reason == HangupReason.AGENT_HANGUP
    asr = [s.duration_ms for s in call.latency_samples if s.component == LatencyComponent.STT]
    assert asr == [100, 140]
    llm = [s for s in call.latency_samples if s.component == LatencyComponent.LLM]
    assert llm[0].ttft_ms == 280
    tools = {t.name: t for t in call.tools}
    assert tools["lookup_invoice"].status == ToolStatus.SUCCESS
    assert tools["lookup_invoice"].payload_shape == {"invoice_id": "string"}
    assert call.cost_usd == 0.42
    assert any("INV-1001" in t.text for t in call.turns)
