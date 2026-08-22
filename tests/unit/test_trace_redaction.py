"""Trace assembly redacts PII before the buffer and keeps the caller token."""

from __future__ import annotations

from obsalt.domain.events import CallObserved, TurnObserved
from obsalt.domain.enums import Speaker
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.types import ReadableSpan


def test_trace_assembly_redacts_before_buffer_and_keeps_caller_token() -> None:
    assembler = MemoryTraceAssembler()
    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
    )
    record = assembler.ingest(
        "acme",
        [span],
        [CallObserved(source_call_id="c1", from_number="+15551230001")],
    )
    assert record.caller_token
    stored = next(event for event in record.events if isinstance(event, CallObserved))
    assert stored.from_number == "<phone>"


def test_late_spans_after_finalize_rebuild() -> None:
    assembler = MemoryTraceAssembler()
    span = ReadableSpan(
        name="turn",
        trace_id="t1",
        span_id="s1",
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
    )
    record = assembler.ingest("acme", [span], [CallObserved(source_call_id="c1")])
    assembler.mark_finalized(record)
    late = ReadableSpan(
        name="turn",
        trace_id="t1",
        span_id="s2",
        start_unix_nano=3_000_000_000,
        end_unix_nano=4_000_000_000,
        parent_span_id="",
        attributes={"obsalt.as_root": True},
    )
    again = assembler.ingest("acme", [late], [TurnObserved(turn_index=0, speaker=Speaker.USER, text="late")])
    assert again.late_after_finalize is True
    assert again.finalized is False
    assert assembler.ready(again) is True
