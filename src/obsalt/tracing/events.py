from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obsalt.tracing.context import inject_traceparent


def turn_completed_event(
    *,
    call_id: str,
    agent_id: str,
    turn_index: int,
    speaker: str,
    environment: str = "prod",
    asr_confidence: float | None = None,
    intent: str | None = None,
    failure_reason: str | None = None,
    transcript_id: str | None = None,
    recording_id: str | None = None,
    providers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Loki-safe envelope. High-cardinality ids live in the body, not labels."""
    headers = inject_traceparent({})
    traceparent = headers.get("traceparent", "")
    parts = traceparent.split("-")
    trace_id = parts[1] if len(parts) >= 4 else ""
    span_id = parts[2] if len(parts) >= 4 else ""
    return {
        "event_name": "voice.turn.completed",
        "event_version": "2026-08-21",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "canonical_call_id": call_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": agent_id,
        "environment": environment,
        "provider": providers or {},
        "turn": {
            "turn_index": turn_index,
            "speaker": speaker,
            "intent": intent,
            "asr_confidence": asr_confidence,
        },
        "quality": {"failure_reason": failure_reason},
        "evidence": {
            "transcript_turn_id": transcript_id,
            "recording_segment_id": recording_id,
            "redaction_state": "redacted",
        },
    }
