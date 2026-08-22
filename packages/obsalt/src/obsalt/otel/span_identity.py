"""OTLP delivery identity is (org_id, trace_id, span_id, content_fingerprint) (§6.2)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from obsalt.plugin.types import ReadableSpan
from obsalt.util import canonical_json, sha256_bytes, sha256_text


def span_content_fingerprint(span: ReadableSpan) -> str:
    payload = {
        "name": span.name,
        "parent": span.parent_span_id,
        "start": span.start_unix_nano,
        "end": span.end_unix_nano,
        "attributes": span.attributes,
        "status": span.status_code,
    }
    return sha256_text(canonical_json(payload))


def span_identity(org_id: str, span: ReadableSpan) -> tuple[str, str, str, str]:
    return (org_id, span.trace_id, span.span_id, span_content_fingerprint(span))


def otlp_delivery_key(org_id: str, raw: bytes, spans: Sequence[ReadableSpan] | None = None) -> str:
    """Identical exporter retries of the same spans share this key.

    Empty or unparsed batches fall back to the raw-body digest so receive
    still dedupes exact retries before decode.
    """

    if not spans:
        return f"otlp:{org_id}:{sha256_bytes(raw)}"
    parts = sorted(
        f"{span.trace_id}:{span.span_id}:{span_content_fingerprint(span)}" for span in spans
    )
    return f"otlp:{org_id}:{sha256_text('|'.join(parts))}"


class SpanIdentityIndex:
    """Detect same-identity different-content conflicts. A hash is not a revision."""

    def __init__(self) -> None:
        self._fingerprints: dict[tuple[str, str, str], str] = {}
        self.conflicts: list[tuple[str, str, str]] = []

    def observe(self, org_id: str, span: ReadableSpan) -> str:
        key = (org_id, span.trace_id, span.span_id)
        fingerprint = span_content_fingerprint(span)
        existing = self._fingerprints.get(key)
        if existing is None:
            self._fingerprints[key] = fingerprint
            return "accepted"
        if existing == fingerprint:
            return "duplicate"
        self.conflicts.append(key)
        return "conflict"

    def snapshot(self) -> dict[str, Any]:
        return {"count": len(self._fingerprints), "conflicts": list(self.conflicts)}


class PostgresSpanIdentityIndex(SpanIdentityIndex):
    """Durable (org, trace, span) fingerprints. A hash is not a revision."""

    def __init__(self, conn: Any) -> None:
        super().__init__()
        self._conn = conn

    def observe(self, org_id: str, span: ReadableSpan) -> str:
        fingerprint = span_content_fingerprint(span)
        cached = super().observe(org_id, span)
        if cached != "accepted":
            return cached
        try:
            row = self._conn.execute(
                """
                SELECT content_fingerprint FROM otlp_span_identities
                WHERE org_id = %s AND trace_id = %s AND span_id = %s
                """,
                (org_id, span.trace_id, span.span_id),
            ).fetchone()
        except Exception:
            return cached
        existing = None if row is None else (row["content_fingerprint"] if isinstance(row, dict) else row[0])
        if existing is None:
            try:
                with self._conn.transaction():
                    self._conn.execute(
                        """
                        INSERT INTO otlp_span_identities (org_id, trace_id, span_id, content_fingerprint)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (org_id, trace_id, span_id) DO NOTHING
                        """,
                        (org_id, span.trace_id, span.span_id, fingerprint),
                    )
            except Exception:
                return cached
            return "accepted"
        if existing == fingerprint:
            return "duplicate"
        self.conflicts.append((org_id, span.trace_id, span.span_id))
        return "conflict"
