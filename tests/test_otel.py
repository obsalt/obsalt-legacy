from __future__ import annotations

import time

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.domain.enums import Provider
from obsalt.pipeline import IngestPipeline
from obsalt.store import MemoryStore
from obsalt.tracing import conventions as c
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import to_unix_ns
from tests.conftest import load_fixture
from tests.span_helpers import attrs, span_forest


def _ingest_vapi():
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    _, meter_provider, metrics = setup_tracing(span_exporter=exporter, metric_reader=reader, batch=False)
    store = MemoryStore()
    pipeline = IngestPipeline(store=store, metrics=metrics)
    result = pipeline.ingest(Provider.VAPI, load_fixture("vapi_end_of_call.json"), org_id="org")
    if meter_provider is not None:
        meter_provider.force_flush()
    return result, store, exporter, reader


def test_vapi_webhook_reconstructs_span_tree_without_pii() -> None:
    result, store, exporter, reader = _ingest_vapi()
    spans = list(exporter.get_finished_spans())
    names = {s.name for s in spans}
    assert c.SPAN_CALL in names
    assert any(n.startswith("turn.") for n in names)
    assert c.SPAN_LLM in names
    assert any(n.startswith("llm.tool_call.") for n in names)
    assert c.SPAN_EVAL in names  # hallucination / rubrics
    assert c.SPAN_TRANSCRIPT_FINAL in names
    assert not any("fallback" in n for n in names)  # do not invent provider fallbacks

    by_id = {s.context.span_id: s for s in spans if s.context}
    tool = next(s for s in spans if s.name.startswith("llm.tool_call."))
    assert tool.parent is not None
    parent = by_id[tool.parent.span_id]
    assert parent.name == c.SPAN_LLM

    forest = span_forest(spans)
    assert c.SPAN_CALL in forest["__roots__"] or forest["__roots__"] == [c.SPAN_CALL]

    root = next(s for s in spans if s.name == c.SPAN_CALL)
    ra = attrs(root)
    assert ra[c.CALL_ID] == result.call_id
    assert ra[c.EVIDENCE_TRANSCRIPT_ID]
    assert ra[c.EVIDENCE_REDACTION] == "redacted"
    for span in spans:
        blob = str(attrs(span))
        assert "ada@example.com" not in blob
        assert "ORD-99999" not in blob  # fabricated id stays in evidence, not span attrs
        a = attrs(span)
        for key in c.JOIN_KEYS:
            assert key in a, f"{span.name} missing {key}"
        for forbidden in c.PII_FORBIDDEN_ATTR_KEYS:
            assert forbidden not in a

    call = store.get_call("org", result.call_id)
    assert call is not None and call.started_at is not None
    expected = to_unix_ns(call.started_at)
    assert expected is not None
    assert root.start_time == expected
    now = time.time_ns()
    assert root.start_time < now - 1_000_000_000  # historical, not "now"

    data = reader.get_metrics_data()
    assert data is not None
    metric_names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert c.METRIC_CALLS in metric_names
    assert c.METRIC_LATENCY in metric_names
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                points = getattr(metric.data, "data_points", [])
                for point in points:
                    keys = set((point.attributes or {}).keys())
                    assert "call_id" not in keys
                    assert c.CALL_ID not in keys
