"""Minimal in-process instrumentation. Requires an OTLP collector on :4318."""

from __future__ import annotations

from obsalt import VoiceCallTracer, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")

with VoiceCallTracer.start(call_id="call-1", workspace_id="acme", agent_id="support") as call:
    with call.turn(0, "user") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80)
            with llm.tool("lookup_order") as tool:
                tool.set(execution_ms=120, status_code=200)
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
    call.set_call_outcome(duration_ms=12_400, status="ended")
