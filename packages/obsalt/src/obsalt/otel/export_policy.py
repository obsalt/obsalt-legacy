"""Export redaction for derived spans (§8.3)."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace import SpanProcessor

from obsalt.otel.conventions import PII_FORBIDDEN_ATTR_KEYS, PII_PREFIX


class StripPiiSpanProcessor(SpanProcessor):
    """SDK span processor. Mutates obsalt.pii.* off the span before export."""

    def __init__(self, *, emit_pii: bool = False) -> None:
        self.emit_pii = emit_pii

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        return None

    def on_end(self, span: Any) -> None:
        if self.emit_pii:
            return
        attrs = getattr(span, "_attributes", None)
        if not isinstance(attrs, dict):
            return
        for key in list(attrs):
            name = str(key)
            if name.startswith(PII_PREFIX) or name in PII_FORBIDDEN_ATTR_KEYS:
                attrs.pop(key, None)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 0) -> bool:
        return True
