"""Attribute promotion: unmapped keys land in a catch-all map (§10.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from obsalt.metrics import unmapped_attributes_total
from obsalt.plugin.types import ReadableSpan

KNOWN_PREFIXES = (
    "gen_ai.",
    "obsalt.",
    "turn.",
    "lk.",
    "metrics.",
    "openinference.",
    "llm.",
    "traceloop.",
    "elevenlabs.",
    "stt.",
    "tts.",
    "tool.",
    "call.",
    "agent.",
    "error.",
    "service.",
)


def leftover_attributes(spans: Iterable[ReadableSpan], *, budget: int = 64) -> dict[str, str]:
    """Typed columns are created by migration; this map is the honest catch-all."""

    out: dict[str, str] = {}
    for span in spans:
        attrs: Mapping[str, object] = getattr(span, "attributes", None) or {}
        for key, value in attrs.items():
            name = str(key)
            if any(name.startswith(prefix) for prefix in KNOWN_PREFIXES):
                continue
            if name in out:
                continue
            out[name] = str(value)[:512]
            unmapped_attributes_total.inc()
            if len(out) >= budget:
                return out
    return out
