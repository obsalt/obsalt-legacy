from __future__ import annotations

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.tracing.events import turn_completed_event
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import VoiceCallTracer


def test_turn_completed_envelope_keeps_ids_in_body() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    with VoiceCallTracer.start(call_id="c9", workspace_id="acme", agent_id="support"):
        event = turn_completed_event(
            call_id="c9",
            agent_id="support",
            turn_index=0,
            speaker="user",
            asr_confidence=0.4,
            transcript_id="tr_abc",
            recording_id="rec_xyz",
        )
    assert event["event_name"] == "voice.turn.completed"
    assert event["canonical_call_id"] == "c9"
    assert event["trace_id"]
    assert event["evidence"]["redaction_state"] == "redacted"
    assert "transcript" not in event
    assert event["turn"]["asr_confidence"] == 0.4
