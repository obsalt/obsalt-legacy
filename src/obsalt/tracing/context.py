from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject


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
