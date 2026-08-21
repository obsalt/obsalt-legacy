from __future__ import annotations

from obsalt.domain.enums import Provider, Speaker
from obsalt.pipeline import IngestPipeline
from obsalt.sdk import VoiceTracer
from obsalt.store import MemoryStore


def test_sdk_snapshot_roundtrip() -> None:
    tracer = VoiceTracer(org_id="org", provider=Provider.OPENAI_REALTIME, call_id="sdk-1", agent_id="concierge")
    with tracer.turn(Speaker.USER, "book Friday") as turn:
        turn.stt_ms = 120
    with tracer.tool("create_booking", {"night": "Friday"}) as tool:
        tool.set_result({"confirmation": "HTL-1"})
    with tracer.turn(Speaker.AGENT, "Booked HTL-1") as turn:
        turn.llm_ms = 300
        turn.llm_ttft_ms = 220
        turn.tts_ms = 90
        turn.tts_ttfb_ms = 40
    payload = tracer.snapshot(hangup_reason="completed")
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    result = pipeline.ingest(Provider.NATIVE, payload, org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    assert call.provider == Provider.OPENAI_REALTIME
    assert call.tools[0].name == "create_booking"
    assert call.tools[0].status.value == "success"
    assert any(s.component.value == "llm" for s in call.latency_samples)
    assert "HTL-1" in call.transcript_text
