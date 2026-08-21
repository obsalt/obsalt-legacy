from __future__ import annotations

from obsalt.domain.enums import LatencyComponent
from obsalt.domain.models import LatencyPercentiles


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile on a pre-sorted list. pct in 0–100."""
    if not sorted_values:
        return None
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    low = int(k)
    high = min(low + 1, len(sorted_values) - 1)
    weight = k - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def summarize(component: LatencyComponent, values: list[float]) -> LatencyPercentiles:
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return LatencyPercentiles(component=component, count=0)
    return LatencyPercentiles(
        component=component,
        count=len(ordered),
        p50_ms=round(percentile(ordered, 50) or 0.0, 3),
        p95_ms=round(percentile(ordered, 95) or 0.0, 3),
        p99_ms=round(percentile(ordered, 99) or 0.0, 3),
        avg_ms=round(sum(ordered) / len(ordered), 3),
        min_ms=round(ordered[0], 3),
        max_ms=round(ordered[-1], 3),
    )
