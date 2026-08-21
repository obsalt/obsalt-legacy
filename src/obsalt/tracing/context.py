from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


def inject_traceparent(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Write W3C traceparent/tracestate into a header map."""
    headers: dict[str, str] = dict(carrier or {})
    inject(headers)
    return headers


def extract_traceparent(carrier: dict[str, str] | None) -> Any:
    """Return an OTel context from inbound headers. Missing header → current context."""
    if not carrier:
        return otel_context.get_current()
    lowered = {str(k).lower(): str(v) for k, v in carrier.items()}
    if "traceparent" not in lowered:
        return otel_context.get_current()
    return extract(lowered)


def parse_traceparent(header: str) -> tuple[str, str] | None:
    parts = header.split("-")
    if len(parts) < 4 or parts[0] != "00":
        return None
    return parts[1], parts[2]
