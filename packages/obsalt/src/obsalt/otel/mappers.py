"""Priority-ordered mapper registry. ``obsalt.*`` wins."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from dataclasses import dataclass, field
from datetime import datetime, timezone

from obsalt.domain.events import NormalizedEvent
from obsalt.plugin.host import PluginHost


@dataclass
class SpanView:
    """Mapper-facing span. Works for both OTLP JSON and protobuf extracts."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    start_time: float | None = None
    end_time: float | None = None
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    resource: dict[str, Any] = field(default_factory=dict)

    def as_datetime(self, value: float | None) -> datetime | None:
        if value is None:
            return None
        ns = float(value)
        if ns > 1e14:
            return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
        if ns > 1e10:
            return datetime.fromtimestamp(ns / 1e3, tz=timezone.utc)
        return datetime.fromtimestamp(ns, tz=timezone.utc)


def span_from_mapping(row: dict[str, Any]) -> SpanView:
    attrs = row.get("attributes") or {}
    if isinstance(attrs, list):
        attrs = {item.get("key"): (item.get("value") or {}).get("stringValue") or item.get("value") for item in attrs}
    start = row.get("startTimeUnixNano") or row.get("start_time_unix_nano") or row.get("start_time")
    end = row.get("endTimeUnixNano") or row.get("end_time_unix_nano") or row.get("end_time")
    return SpanView(
        name=str(row.get("name") or ""),
        attributes={str(k): v for k, v in (attrs or {}).items()},
        start_time=float(start) if start is not None else None,
        end_time=float(end) if end is not None else None,
        trace_id=str(row.get("traceId") or row.get("trace_id") or ""),
        span_id=str(row.get("spanId") or row.get("span_id") or ""),
        parent_span_id=str(row.get("parentSpanId") or row.get("parent_span_id") or ""),
        resource=dict(row.get("_resource") or {}),
    )


def select_mapper(host: PluginHost, span: Any) -> Any | None:
    best = None
    best_score = 0
    attrs = getattr(span, "attributes", {}) or {}
    if any(str(k).startswith("obsalt.") for k in attrs):
        boost = 1000
    else:
        boost = 0
    for name, mapper in host.mappers():
        score = int(mapper.claims(span))
        if name.startswith("obsalt") or "obsalt" in name:
            score += 100
        score += boost
        if score > best_score:
            best_score = score
            best = mapper
    return best if best_score > 0 else None


def decode_spans(host: PluginHost, spans: Sequence[Any]) -> list[NormalizedEvent]:
    grouped: dict[Any, list[Any]] = {}
    for span in spans:
        mapper = select_mapper(host, span)
        grouped.setdefault(mapper, []).append(span)
    events: list[NormalizedEvent] = []
    for mapper, group in grouped.items():
        if mapper is None:
            continue
        events.extend(list(mapper.decode(group)))
    return events
