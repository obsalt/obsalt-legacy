from __future__ import annotations

from obsalt.adapters.vapi import VapiAdapter
from obsalt.domain.enums import HangupReason, LatencyComponent, Speaker, ToolStatus
from tests.conftest import load_fixture


def test_vapi_end_of_call_normalizes() -> None:
    result = VapiAdapter().parse(load_fixture("vapi_end_of_call.json"), org_id="org-1")
    assert result is not None
    assert result.terminal is True
    call = result.call
    assert call.provider_call_id == "vapi-call-refund-1"
    assert call.agent_id == "support-agent"
    assert call.direction.value == "inbound"
    assert call.hangup is not None
    assert call.hangup.reason == HangupReason.USER_HANGUP
    assert any(t.speaker == Speaker.USER and "refund" in t.text.lower() for t in call.turns)
    tools = {t.name: t for t in call.tools}
    assert "lookup_order" in tools
    assert tools["lookup_order"].status == ToolStatus.ERROR
    e2e = [s for s in call.latency_samples if s.component == LatencyComponent.E2E]
    assert e2e
    assert call.cost_usd == 0.18
    assert "Never invent order numbers" in call.grounding.system_prompt
    assert "ada@example.com" not in (tools["lookup_order"].result_preview or "")
