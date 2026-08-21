"""Live traces + evidence from one VoiceCall. Requires OTLP collector on :4318
and (for the POST) ``obsalt serve`` on :8080. Omit client= to only export spans.
"""

from __future__ import annotations

from obsalt import ObsaltClient, VoiceCall, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")

client = ObsaltClient(base_url="http://127.0.0.1:8080", api_key="change-me")

with VoiceCall.start(
    call_id="call-1",
    workspace_id="acme",
    agent_id="support",
    client=client,
    system_prompt="Never invent order numbers.",
) as call:
    with call.turn(0, "user", text="I need a refund") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80)
            with llm.tool("lookup_order", {"order_id": "ORD-1"}) as tool:
                tool.set(execution_ms=120, status_code=200)
                tool.set_result({"status": "not_found"})
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
    call.set_call_outcome(duration_ms=12_400, status="ended")

print("obsalt call.id", call.obsalt_call_id)
print("your id     ", call.provider_call_id)
