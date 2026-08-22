"""Export redaction for derived spans and foreign OTLP JSON batches (§8.3)."""

from __future__ import annotations

import json
from typing import Any

from obsalt.otel.conventions import PII_FORBIDDEN_ATTR_KEYS, PII_PREFIX


def strip_attribute_map(attrs: dict[str, Any], *, emit_pii: bool) -> dict[str, Any]:
    if emit_pii:
        return dict(attrs)
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        name = str(key)
        if name.startswith(PII_PREFIX) or name in PII_FORBIDDEN_ATTR_KEYS:
            continue
        out[key] = value
    return out


def redact_otlp_json(raw: bytes, *, emit_pii: bool) -> bytes:
    """Strip PII attributes from a proto3-JSON OTLP batch. Identity fields stay."""

    if emit_pii:
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        resource = resource_span.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("attributes"), list):
            resource["attributes"] = _strip_otlp_attr_list(resource["attributes"])
        for scope in resource_span.get("scopeSpans") or []:
            if not isinstance(scope, dict):
                continue
            for span in scope.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                if isinstance(span.get("attributes"), list):
                    span["attributes"] = _strip_otlp_attr_list(span["attributes"])
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _strip_otlp_attr_list(attrs: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for item in attrs:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        key = str(item.get("key") or "")
        if key.startswith(PII_PREFIX) or key in PII_FORBIDDEN_ATTR_KEYS:
            continue
        kept.append(item)
    return kept


class StripPiiSpanProcessor:
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
