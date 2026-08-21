from __future__ import annotations

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.domain.enums import Provider
from obsalt.otel.instrumentation import setup_telemetry
from obsalt.pipeline import IngestPipeline
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def test_finalize_emits_voice_and_genai_spans() -> None:
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    _, _, telemetry = setup_telemetry(span_exporter=exporter, metric_reader=reader)
    store = MemoryStore()
    pipeline = IngestPipeline(store=store, telemetry=telemetry)
    pipeline.ingest(Provider.RETELL, load_fixture("retell_call_ended.json"), org_id="org")

    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert any(name.startswith("voice.call") for name in names)
    assert "voice.turn" in names
    assert any(name.startswith("execute_tool") for name in names)
    call_span = next(s for s in spans if s.name.startswith("voice.call"))
    assert call_span.attributes["voice.provider"] == "retell"
    assert call_span.attributes["voice.hangup.reason"] == "agent_hangup"
    assert call_span.attributes["gen_ai.operation.name"] == "invoke_agent"

    data = reader.get_metrics_data()
    metric_names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert "voice.latency.stt" in metric_names
    assert "voice.latency.llm" in metric_names
    assert "voice.latency.tts" in metric_names
    assert "voice.hangup.count" in metric_names
    assert "voice.tool.invocations" in metric_names
