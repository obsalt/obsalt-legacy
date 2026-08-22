"""Health signals from day one (§12.2)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

late_spans_after_finalize_total = Counter(
    "obsalt_late_spans_after_finalize_total",
    "Spans arrived after a trace was finalized; they build a newer revision.",
)
decode_failures_total = Counter(
    "obsalt_decode_failures_total",
    "Decode failures by plugin.",
    ["plugin"],
)
promotion_failures_total = Counter(
    "obsalt_active_revision_promotion_failures_total",
    "CAS or conflict blocked promotion.",
)
dlq_inserts_total = Counter(
    "obsalt_dlq_inserts_total",
    "Dead-letter inserts. Alert on this, not just a log line.",
)
unmapped_provider_codes_total = Counter(
    "obsalt_unmapped_provider_codes_total",
    "Hangup codes that classified as unknown.",
    ["provider"],
)
inbox_age_seconds = Gauge(
    "obsalt_inbox_age_seconds",
    "Age of the oldest unassembled envelope.",
)
tier2_spend_usd = Gauge(
    "obsalt_tier2_spend_usd",
    "Per-org LLM spend burn-down.",
    ["org_id"],
)
