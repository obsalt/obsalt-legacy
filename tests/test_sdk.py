from __future__ import annotations

from obsalt.domain.enums import Provider, Speaker
from obsalt.pipeline import IngestPipeline
from obsalt.sdk import CallRecorder
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def test_sdk_snapshot_roundtrip() -> None:
    recorder = CallRecorder(
        org_id="org", provider="openai-realtime", call_id="sdk-1", agent_id="concierge"
    )
    with recorder.turn("user", "book Friday") as turn:
        turn.stt_ms = 120
    with recorder.tool("create_booking", {"night": "Friday", "email": "ada@example.com"}) as tool:
        tool.set_result({"confirmation": "HTL-1"})
    with recorder.turn("assistant", "Booked HTL-1") as turn:
        turn.llm_ms = 300
        turn.llm_ttft_ms = 220
        turn.tts_ms = 90
        turn.tts_ttfb_ms = 40
    payload = recorder.snapshot(hangup_reason="completed")
    assert payload["duration_ms"] is not None
    assert payload["spans_exported"] is False
    assert payload["call_id"] == "sdk-1"
    assert payload["tools"][0]["payload_shape"] == {"email": "string", "night": "string"}
    assert "ada@example.com" not in str(payload["tools"][0]["metadata"])
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    result = pipeline.ingest(Provider.NATIVE, payload, org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    assert call.provider == Provider.OPENAI_REALTIME
    assert call.duration_ms is not None
    assert call.tools[0].name == "create_booking"
    assert call.tools[0].status.value == "success"
    assert call.tools[0].payload_shape == {"email": "string", "night": "string"}
    assert "ada@example.com" not in str(call.model_dump())
    assert any(s.component.value == "llm" for s in call.latency_samples)
    assert "HTL-1" in call.transcript_text
    assert call.turns[1].speaker == Speaker.AGENT


def test_native_fixture_ingests() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    result = pipeline.ingest(Provider.NATIVE, load_fixture("native_snapshot.json"), org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    assert call.provider_call_id == "room-42"
    assert "HTL-1" in call.transcript_text
    assert call.tools[0].payload_shape["email"] == "string"
