from __future__ import annotations

from obsalt.adapters.openai_realtime import OpenAIRealtimeAdapter
from obsalt.domain.enums import HangupReason, LatencyComponent, ToolStatus
from tests.conftest import load_fixture


def test_openai_realtime_session_derives_stt_llm_tts() -> None:
    result = OpenAIRealtimeAdapter().parse(load_fixture("openai_realtime_session.json"), org_id="org-1")
    assert result is not None and result.terminal
    call = result.call
    assert call.provider_call_id == "rt-session-1"
    assert call.hangup and call.hangup.reason == HangupReason.USER_HANGUP
    user = call.user_turns()
    agent = call.agent_turns()
    assert user[0].text.startswith("Can you book")
    assert "HTL-4421" in agent[0].text
    assert user[0].stt_ms == 170  # 1920 - 1750
    assert any(s.component == LatencyComponent.TTFA for s in call.latency_samples)
    tool = call.tools[0]
    assert tool.name == "create_booking"
    assert tool.status == ToolStatus.SUCCESS
    assert "HTL-4421" in (tool.result_preview or "")
    # Barge-in / second user turn after agent audio
    assert len(user) == 2
    assert "Hotel concierge" in call.grounding.system_prompt
