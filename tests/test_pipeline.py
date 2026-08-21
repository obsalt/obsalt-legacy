from __future__ import annotations

from obsalt.domain.enums import Provider
from obsalt.pipeline import IngestPipeline
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def test_idempotent_reingest_same_call_id() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    payload = load_fixture("vapi_end_of_call.json")
    first = pipeline.ingest(Provider.VAPI, payload, org_id="org")
    second = pipeline.ingest(Provider.VAPI, payload, org_id="org")
    assert first.call_id == second.call_id
    assert second.created is False
    assert len(store.list_calls("org")) == 1


def test_live_then_terminal_merges_latency() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    live = pipeline.ingest(
        Provider.BLAND,
        {"call_id": "bland-call-transfer-1", "category": "latency", "message": "TTS: 218ms"},
        org_id="org",
    )
    assert live.status in {"accepted", "merged"}
    final = pipeline.ingest(Provider.BLAND, load_fixture("bland_post_call.json"), org_id="org")
    assert final.call_id == live.call_id
    call = store.get_call("org", final.call_id)
    assert call is not None and call.finalized
    assert any(s.component.value == "tts" and s.duration_ms == 218 for s in call.latency_samples)
    assert call.hangup is not None


def test_org_isolation() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    a = pipeline.ingest(Provider.RETELL, load_fixture("retell_call_ended.json"), org_id="a")
    pipeline.ingest(Provider.RETELL, load_fixture("retell_call_ended.json"), org_id="b")
    assert store.get_call("b", a.call_id) is None
    assert len(store.list_calls("a")) == 1


def test_tool_arguments_redacted_on_ingest() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    result = pipeline.ingest(Provider.VAPI, load_fixture("vapi_end_of_call.json"), org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    blob = str(call.model_dump(mode="json"))
    assert "ada@example.com" not in blob
    lookup = next(t for t in call.tools if t.name == "lookup_order")
    assert lookup.payload_shape == {"email": "string", "order_id": "string"}
