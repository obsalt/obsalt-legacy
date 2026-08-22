"""Identity-preserving OTLP forwarder policy. Strips marked PII by default."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from obsalt.otel.conventions import PII_PREFIX, is_pii_attr


def filter_attributes(
    attrs: Mapping[str, Any],
    *,
    emit_pii: bool = False,
    extra_deny: frozenset[str] | None = None,
) -> dict[str, Any]:
    deny = extra_deny or frozenset()
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in deny:
            continue
        if not emit_pii and (key.startswith(PII_PREFIX) or is_pii_attr(key)):
            continue
        out[key] = value
    return out
