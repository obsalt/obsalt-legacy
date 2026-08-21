from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from obsalt._version import __version__
from obsalt.tracing.metrics import VoiceMetrics


def _install_tracer_provider(provider: TracerProvider) -> None:
    # OpenTelemetry forbids a second set_tracer_provider(); tests need a fresh exporter.
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    proxy = getattr(trace, "_PROXY_TRACER_PROVIDER", None)
    setter = getattr(proxy, "on_set_tracer_provider", None)
    if callable(setter):
        setter(provider)


def _install_meter_provider(provider: MeterProvider) -> None:
    metrics_internal._METER_PROVIDER = provider
    proxy = getattr(metrics_internal, "_PROXY_METER_PROVIDER", None)
    setter = getattr(proxy, "on_set_meter_provider", None)
    if callable(setter):
        setter(provider)


def setup_tracing(
    *,
    service_name: str = "obsalt",
    span_exporter: SpanExporter | None = None,
    metric_reader: InMemoryMetricReader | PeriodicExportingMetricReader | None = None,
    otlp_endpoint: str | None = None,
    batch: bool = True,
    environment: str | None = None,
) -> tuple[TracerProvider, MeterProvider | None, VoiceMetrics]:
    """Configure process-wide providers.

    Production uses BatchSpanProcessor + OTLP. Tests pass in-memory exporters
    and batch=False (SimpleSpanProcessor) so spans flush immediately.
    """
    resource_attrs = {"service.name": service_name, "service.version": __version__}
    if environment:
        resource_attrs["deployment.environment"] = environment
    resource = Resource.create(resource_attrs)
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        processor = BatchSpanProcessor(span_exporter) if batch else SimpleSpanProcessor(span_exporter)
        tracer_provider.add_span_processor(processor)
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
        )
    _install_tracer_provider(tracer_provider)

    meter_provider: MeterProvider | None = None
    readers = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics"))
        )
    if readers:
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        _install_meter_provider(meter_provider)

    return tracer_provider, meter_provider, VoiceMetrics(metrics.get_meter("obsalt"))
