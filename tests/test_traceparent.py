from __future__ import annotations

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.tracing.context import inject_traceparent
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import VoiceCallTracer
from obsalt.util import call_id_for
from tests.span_helpers import attrs


def test_traceparent_round_trip_continues_the_same_trace() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)

    with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="web") as web:
        headers = inject_traceparent({})
        assert headers["traceparent"].startswith("00-")
        web_trace = web.span.get_span_context().trace_id

    with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="worker", headers=headers) as worker:
        worker_trace = worker.span.get_span_context().trace_id
        with worker.turn(0) as turn:
            with turn.stt("deepgram"):
                pass

    assert web_trace == worker_trace
    stt = next(s for s in exporter.get_finished_spans() if s.name == "stt.transcription")
    assert stt.context.trace_id == web_trace
    assert attrs(stt)["call.id"] == call_id_for("acme", "native", "c1")
    assert attrs(stt)["call.provider_id"] == "c1"
    assert attrs(stt)["workspace.id"] == "acme"


def test_start_accepts_extracted_context() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    from obsalt.tracing.context import extract_traceparent

    with VoiceCallTracer.start(call_id="c2", workspace_id="acme", agent_id="web") as web:
        headers = inject_traceparent({})
        trace_id = web.span.get_span_context().trace_id
    ctx = extract_traceparent(headers)
    with VoiceCallTracer.start(call_id="c2", workspace_id="acme", agent_id="worker", context=ctx) as worker:
        assert worker.span.get_span_context().trace_id == trace_id
