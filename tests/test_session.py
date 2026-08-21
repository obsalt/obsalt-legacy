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
from obsalt.util import call_id_for
from tests.span_helpers import attrs, span_forest


def test_voicecall_join_keys_match_evidence_and_skip_duplicate_trace() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    settings = Settings(api_keys="acme:secret", require_auth=True, otlp_endpoint="")
    app = create_app(store=store, pipeline=pipeline, settings=settings)
    http = TestClient(app)
    client = ObsaltClient(http_client=http, api_key="secret")

    expected_id = call_id_for("acme", "native", "room-1")
    with VoiceCall.start(
        call_id="room-1",
        workspace_id="acme",
        agent_id="support",
        client=client,
        system_prompt="Rooms are $189.",
    ) as call:
        assert call.obsalt_call_id == expected_id
        with call.turn(0, "user", text="book Friday") as turn:
            with turn.stt("deepgram") as stt:
                stt.set(confidence=0.91, latency_ms=120)
        with call.turn(1, "assistant", text="Booked HTL-1") as turn:
            with turn.llm("gpt-4o", provider="openai") as llm:
                llm.set(ttft_ms=300, tokens_in=20, tokens_out=8)
                with llm.tool("create_booking", {"night": "Friday"}) as tool:
                    tool.set(execution_ms=40, status_code=200)
                    tool.set_result({"confirmation": "HTL-1"})
            with turn.tts("elevenlabs") as tts:
                tts.set(synthesis_ms=90, first_audio_ms=30)
        call.set_call_outcome(duration_ms=4_000, status="ended")

    stored = store.get_call("acme", expected_id)
    assert stored is not None
    assert stored.provider_call_id == "room-1"
    assert "book Friday" in stored.transcript_text
    assert stored.tools[0].name == "create_booking"
    assert stored.metadata.get("spans_exported") is True
    assert stored.metadata.get("traceparent")

    spans = list(exporter.get_finished_spans())
    roots = [s for s in spans if s.name == c.SPAN_CALL]
    assert len(roots) == 1
    forest = span_forest(spans)
    assert forest["__roots__"] == [c.SPAN_CALL]
    for span in spans:
        a = attrs(span)
        assert a[c.CALL_ID] == expected_id
        assert a[c.PROVIDER_CALL_ID] == "room-1"

    listed = client.list_calls(provider_call_id="room-1", provider="native")
    assert listed["count"] == 1
    assert listed["calls"][0]["id"] == expected_id


def test_voicecall_snapshot_schema_flags() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    with VoiceCall.start(call_id="s1", workspace_id="acme", agent_id="bot") as call:
        with call.turn(0, "user", text="hi"):
            pass
        payload = call.snapshot(hangup_reason="completed")
    assert payload["call_id"] == "s1"
    assert payload["spans_exported"] is True
    assert payload["traceparent"]
    assert payload["turns"][0]["text"] == "hi"
    assert payload["provider"] == "native"
