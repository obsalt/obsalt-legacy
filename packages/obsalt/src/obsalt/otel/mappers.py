"""Priority-ordered mapper registry. ``obsalt.*`` wins."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from obsalt.domain.events import NormalizedEvent
from obsalt.plugin.host import PluginHost


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
