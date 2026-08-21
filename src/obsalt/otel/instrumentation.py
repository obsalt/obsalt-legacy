from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from obsalt.domain.models import CanonicalCall
from obsalt.otel import conventions as c

_configured = False


class VoiceTelemetry:
    def __init__(self, tracer: Tracer | None = None, meter: Meter | None = None) -> None:
        self.tracer = tracer or trace.get_tracer("obsalt")
        self.meter = meter or metrics.get_meter("obsalt")
        self._stt = self.meter.create_histogram(c.METRIC_STT, unit="s", description="STT latency")
        self._llm = self.meter.create_histogram(c.METRIC_LLM, unit="s", description="LLM latency")
        self._tts = self.meter.create_histogram(c.METRIC_TTS, unit="s", description="TTS latency")
        self._e2e = self.meter.create_histogram(c.METRIC_E2E, unit="s", description="End-to-end turn latency")
        self._ttfa = self.meter.create_histogram(c.METRIC_TTFA, unit="s", description="Time to first audio")
        self._duration = self.meter.create_histogram(c.METRIC_CALL_DURATION, unit="s", description="Call duration")
        self._hangup = self.meter.create_counter(c.METRIC_HANGUP, description="Hangup count")
        self._tool_duration = self.meter.create_histogram(c.METRIC_TOOL_DURATION, unit="s")
        self._tool_count = self.meter.create_counter(c.METRIC_TOOL_COUNT)
        self._hallucination = self.meter.create_counter(c.METRIC_HALLUCINATION)
        self._eval = self.meter.create_histogram(c.METRIC_EVAL, unit="1")

    def emit_call(self, call: CanonicalCall) -> None:
        attrs = {
            c.ATTR_PROVIDER: call.provider.value,
            c.ATTR_CALL_ID: call.id,
            c.ATTR_PROVIDER_CALL_ID: call.provider_call_id,
            c.ATTR_AGENT_ID: call.agent_id,
            c.ATTR_DIRECTION: call.direction.value,
            c.ATTR_GENAI_OPERATION: "invoke_agent",
            c.ATTR_GENAI_PROVIDER: call.provider.value,
        }
        if call.agent_name:
            attrs[c.ATTR_AGENT_NAME] = call.agent_name
        if call.hangup:
            attrs[c.ATTR_HANGUP_REASON] = call.hangup.reason.value
            attrs[c.ATTR_HANGUP_PARTY] = call.hangup.party.value
            attrs[c.ATTR_HANGUP_PROVIDER_REASON] = call.hangup.provider_reason
            attrs[c.ATTR_LOSS_SCORE] = call.hangup.loss_score

        start_ns = int(call.started_at.timestamp() * 1e9) if call.started_at else None
        end_ns = int(call.ended_at.timestamp() * 1e9) if call.ended_at else None
        with self.tracer.start_as_current_span(
            f"{c.SPAN_CALL} {call.agent_id}",
            kind=SpanKind.SERVER,
            start_time=start_ns,
            end_on_exit=False,
            attributes=attrs,
        ) as span:
            if call.status.value == "error":
                span.set_status(Status(StatusCode.ERROR, call.hangup.provider_reason if call.hangup else "error"))
            for turn in call.turns:
                self._emit_turn(turn)
            for tool in call.tools:
                tool_attrs = {
                    **attrs,
                    c.ATTR_TOOL_NAME: tool.name,
                    c.ATTR_TOOL_STATUS: tool.status.value,
                    c.ATTR_TOOL_RETRY: tool.retry_count,
                }
                tool_start = int(tool.started_at.timestamp() * 1e9) if tool.started_at else None
                with self.tracer.start_as_current_span(
                    f"{c.SPAN_TOOL_PREFIX} {tool.name}",
                    kind=SpanKind.CLIENT,
                    start_time=tool_start,
                    end_on_exit=False,
                    attributes=tool_attrs,
                ) as tool_span:
                    if tool.status.value in {"error", "timeout"}:
                        tool_span.set_status(Status(StatusCode.ERROR, tool.error or tool.status.value))
                    tool_end = int(tool.ended_at.timestamp() * 1e9) if tool.ended_at else None
                    tool_span.end(tool_end)
                self._tool_count.add(1, {c.ATTR_TOOL_NAME: tool.name, c.ATTR_TOOL_STATUS: tool.status.value, c.ATTR_AGENT_ID: call.agent_id})
                if tool.duration_ms is not None:
                    self._tool_duration.record(
                        tool.duration_ms / 1000.0,
                        {c.ATTR_TOOL_NAME: tool.name, c.ATTR_AGENT_ID: call.agent_id},
                    )
            for flag in call.hallucinations:
                self._hallucination.add(
                    1,
                    {
                        c.ATTR_HALLUCINATION_KIND: flag.kind.value,
                        c.ATTR_AGENT_ID: call.agent_id,
                        c.ATTR_PROVIDER: call.provider.value,
                    },
                )
            for result in call.evals:
                self._eval.record(result.score, {c.ATTR_EVAL_RUBRIC: result.rubric_name, c.ATTR_AGENT_ID: call.agent_id})
            span.end(end_ns)

        labels = {c.ATTR_PROVIDER: call.provider.value, c.ATTR_AGENT_ID: call.agent_id}
        if call.duration_ms is not None:
            self._duration.record(call.duration_ms / 1000.0, labels)
        if call.hangup:
            self._hangup.add(
                1,
                {
                    **labels,
                    c.ATTR_HANGUP_REASON: call.hangup.reason.value,
                    c.ATTR_HANGUP_PARTY: call.hangup.party.value,
                },
            )
        hist = {
            "stt": self._stt,
            "llm": self._llm,
            "tts": self._tts,
            "e2e": self._e2e,
            "ttfa": self._ttfa,
        }
        for sample in call.latency_samples:
            instrument = hist.get(sample.component.value)
            if instrument is None:
                continue
            instrument.record(
                sample.duration_ms / 1000.0,
                {**labels, c.ATTR_COMPONENT: sample.component.value},
            )

    def _emit_turn(self, turn) -> None:
        attrs = {
            c.ATTR_TURN_INDEX: turn.index,
            c.ATTR_TURN_SPEAKER: turn.speaker.value,
        }
        start_ns = int(turn.started_at.timestamp() * 1e9) if turn.started_at else None
        end_ns = int(turn.ended_at.timestamp() * 1e9) if turn.ended_at else None
        with self.tracer.start_as_current_span(
            c.SPAN_TURN,
            kind=SpanKind.INTERNAL,
            start_time=start_ns,
            end_on_exit=False,
            attributes=attrs,
        ) as span:
            for name, duration, extra in (
                (c.SPAN_STT, turn.stt_ms, {}),
                (c.SPAN_LLM, turn.llm_ms, {c.ATTR_GENAI_OPERATION: "chat"}),
                (c.SPAN_TTS, turn.tts_ms, {}),
            ):
                if duration is None:
                    continue
                with self.tracer.start_as_current_span(
                    name,
                    kind=SpanKind.CLIENT,
                    attributes={**attrs, **extra, c.ATTR_COMPONENT: name.split(".")[-1]},
                ):
                    pass
            span.end(end_ns)


def setup_telemetry(
    *,
    service_name: str = "obsalt",
    span_exporter: SpanExporter | None = None,
    metric_reader: InMemoryMetricReader | PeriodicExportingMetricReader | None = None,
    otlp_endpoint: str | None = None,
) -> tuple[TracerProvider, MeterProvider | None, VoiceTelemetry]:
    """Configure a process-wide provider. Tests pass in-memory exporters."""
    global _configured
    resource = Resource.create({"service.name": service_name, "service.version": "0.1.0"})
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
        )
    trace.set_tracer_provider(tracer_provider)

    meter_provider: MeterProvider | None = None
    readers = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics")))
    if readers:
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(meter_provider)

    _configured = True
    telemetry = VoiceTelemetry(trace.get_tracer("obsalt"), metrics.get_meter("obsalt"))
    return tracer_provider, meter_provider, telemetry
