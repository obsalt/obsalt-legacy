from __future__ import annotations

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.api import create_app
from obsalt.client import ObsaltClient
from obsalt.config import Settings
from obsalt.pipeline import IngestPipeline
from obsalt.session import VoiceCall
from obsalt.store import MemoryStore
from obsalt.tracing import conventions as c
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.timeline import build_call_view
from tests.conftest import load_fixture


def _client() -> TestClient:
    store = MemoryStore()
    settings = Settings(api_keys="acme:test-key", require_auth=True)
    app = create_app(store=store, pipeline=IngestPipeline(store=store), settings=settings)
    return TestClient(app)


def test_vapi_view_joins_tree_and_transcript_without_pii_on_spans() -> None:
    client = _client()
    headers = {"X-API-Key": "test-key"}
    ingested = client.post("/v1/ingest/vapi", headers=headers, json=load_fixture("vapi_end_of_call.json"))
    call_id = ingested.json()["call_id"]
    view = client.get(f"/v1/calls/{call_id}/view", headers=headers).json()

    names = {s["name"] for s in view["trace"]["spans"]}
    assert c.SPAN_CALL in names
    assert any(n.startswith("turn.") for n in names)
    assert c.SPAN_LLM in names
    assert any(n.startswith("llm.tool_call.") for n in names)
    assert c.SPAN_EVAL in names
    assert not any("fallback" in n for n in view["trace"]["spans"])

    span_blob = str(view["trace"])
    assert "ada@example.com" not in span_blob
    assert "ORD-99999" not in span_blob
    assert "+15551230001" not in span_blob
    for span in view["trace"]["spans"]:
        for key in c.JOIN_KEYS:
            assert key in span["attributes"], f"{span['name']} missing {key}"
        for forbidden in c.PII_FORBIDDEN_ATTR_KEYS:
            assert forbidden not in span["attributes"]

    assert "ORD-99999" in view["evidence"]["transcript_text"]
    assert view["evidence"]["recording_url"]
    assert view["coverage"]["signals"]["transcript"] is True
    assert view["coverage"]["signals"]["recording"] is True
    assert view["coverage"]["signals"]["stt_fallback"] is False
    assert view["coverage"]["signals"]["live_spans"] is False
    gap_ids = {g["id"] for g in view["coverage"]["gaps"]}
    assert "stt_fallback" in gap_ids
    assert "live_spans" in gap_ids
    stt_gap = next(g for g in view["coverage"]["gaps"] if g["id"] == "stt_fallback")
    assert stt_gap["kind"] == "structural"

    listed = client.get("/v1/calls", headers=headers).json()["calls"][0]
    assert listed["view_path"] == f"/v1/calls/{call_id}/ui"
    assert "stt_fallback" in listed["coverage"]["gaps"]

    page = client.get(f"/v1/calls/{call_id}/ui", headers=headers)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    waterfall, transcript = html.split('id="transcript"', 1)
    assert "call.lifecycle" in waterfall
    assert "ORD-99999" not in waterfall
    assert "ada@example.com" not in waterfall
    assert "ORD-99999" in transcript
    assert "Hosted platforms do not send STT provider fallback hops" in html


def test_ui_requires_key_and_isolates_tenants() -> None:
    store = MemoryStore()
    settings = Settings(api_keys="acme:test-key,beta:other-key", require_auth=True)
    app = create_app(store=store, pipeline=IngestPipeline(store=store), settings=settings)
    client = TestClient(app)
    ingested = client.post(
        "/v1/ingest/vapi",
        headers={"X-API-Key": "test-key"},
        json=load_fixture("vapi_end_of_call.json"),
    )
    call_id = ingested.json()["call_id"]
    login = client.get(f"/v1/calls/{call_id}/ui")
    assert login.status_code == 401
    assert "API key" in login.text
    missing = client.get(f"/v1/calls/{call_id}/view", headers={"X-API-Key": "other-key"})
    assert missing.status_code == 404


def test_voicecall_fallback_survives_into_unified_view() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    settings = Settings(api_keys="acme:secret", require_auth=True)
    app = create_app(store=store, pipeline=pipeline, settings=settings)
    http = TestClient(app)
    client = ObsaltClient(http_client=http, api_key="secret")

    with VoiceCall.start(
        call_id="room-fb",
        workspace_id="acme",
        agent_id="support",
        client=client,
        recording_url="https://example.com/room-fb.wav",
    ) as call:
        with call.turn(0, "user", text="hello") as turn:
            with turn.stt("deepgram") as stt:
                with stt.provider_attempt("deepgram") as attempt:
                    attempt.fail("timeout")
                with stt.provider_attempt("azure", fallback=True) as attempt:
                    attempt.set(latency_ms=400, confidence=0.91)
        with call.turn(1, "assistant", text="hi") as turn:
            with turn.llm("gpt-4o", provider="openai") as llm:
                llm.set(ttft_ms=200)
            with turn.tts("elevenlabs") as tts:
                tts.set(synthesis_ms=80, first_audio_ms=20)
            with turn.playout() as play:
                play.set(playout_ms=300)
        call.set_call_outcome(duration_ms=3_000, status="ended")

    stored = store.get_call("acme", call.obsalt_call_id)
    assert stored is not None
    attempts = stored.turns[0].metadata["stt_attempts"]
    assert attempts[0]["provider"] == "deepgram"
    assert attempts[0]["error"] == "timeout"
    assert attempts[1]["fallback"] is True
    assert stored.turns[1].metadata.get("audio.playout_ms") is not None

    view = client.view_call(call.obsalt_call_id)
    names = [s["name"] for s in view["trace"]["spans"]]
    assert "stt.provider.deepgram" in names
    assert "stt.provider.fallback.azure" in names
    assert c.SPAN_PLAYOUT in names
    assert view["coverage"]["signals"]["stt_fallback"] is True
    assert view["coverage"]["signals"]["live_spans"] is True
    assert view["coverage"]["signals"]["playout"] is True
    assert "hello" in view["evidence"]["transcript_text"]
    assert "hello" not in str(view["trace"])


def test_native_snapshot_preserves_confidence_and_attempts() -> None:
    from obsalt.domain.enums import Provider
    from obsalt.pipeline import IngestPipeline as Pipe
    from obsalt.store import MemoryStore as Mem

    store = Mem()
    payload = load_fixture("native_snapshot.json")
    payload["turns"][0]["confidence"] = 0.91
    payload["turns"][0]["metadata"] = {
        "stt_attempts": [
            {"provider": "deepgram", "fallback": False, "error": "timeout", "latency_ms": 800},
            {"provider": "azure", "fallback": True, "confidence": 0.91, "latency_ms": 400},
        ]
    }
    payload["tools"][0]["turn_index"] = 1
    result = Pipe(store=store).ingest(Provider.NATIVE, payload, org_id="org")
    call = store.get_call("org", result.call_id)
    assert call is not None
    assert call.turns[0].confidence == 0.91
    assert call.tools[0].turn_index == 1
    view = build_call_view(call)
    names = {s["name"] for s in view.trace["spans"]}
    assert "stt.provider.fallback.azure" in names
