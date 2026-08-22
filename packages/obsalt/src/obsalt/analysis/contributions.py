"""Immutable contribution facts for fleet rollups (§9.2).

Candidate facts never feed fleet rollups. After a successful first promotion,
record sample measurements for the active revision. A later revision for the
same call replaces that call's contributions and publishes a new serving
generation. Provider AggregateMeasurements are stored separately and never
enter sample percentiles.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from obsalt.analysis.rollups import _percentile_stats, build_latency_rollup
from obsalt.domain.models import CallRevision
from obsalt.util import new_id


class MemoryRollupStore:
    def __init__(self) -> None:
        self.samples: list[dict[str, Any]] = []
        self.aggregates: list[dict[str, Any]] = []
        self.generation: str = "gen-0"

    def contribute(self, revision: CallRevision) -> str:
        self.samples = [row for row in self.samples if row["call_id"] != revision.call_id]
        self.aggregates = [row for row in self.aggregates if row["call_id"] != revision.call_id]
        for stage in revision.stage_measurements:
            self.samples.append(
                {
                    "org_id": revision.org_id,
                    "call_id": revision.call_id,
                    "revision": revision.revision,
                    "agent_id": revision.agent_id,
                    "stage": stage.stage.value,
                    "metric": stage.metric.value,
                    "value_ms": stage.value_ms,
                }
            )
        for aggregate in revision.aggregate_measurements:
            self.aggregates.append(
                {
                    "org_id": revision.org_id,
                    "call_id": revision.call_id,
                    "revision": revision.revision,
                    "agent_id": revision.agent_id,
                    "stage": aggregate.stage.value,
                    "metric": aggregate.metric.value,
                    "statistic": aggregate.statistic.value,
                    "value_ms": aggregate.value_ms,
                    "label": "provider_published",
                }
            )
        self.generation = new_id()
        return self.generation

    def delete_call(self, org_id: str, call_id: str) -> str:
        self.samples = [row for row in self.samples if not (row["org_id"] == org_id and row["call_id"] == call_id)]
        self.aggregates = [row for row in self.aggregates if not (row["org_id"] == org_id and row["call_id"] == call_id)]
        self.generation = new_id()
        return self.generation

    def rebuild(self, revisions: list[CallRevision]) -> str:
        self.samples = []
        self.aggregates = []
        generation = "gen-0"
        for revision in revisions:
            generation = self.contribute(revision)
        self.generation = generation
        return generation

    def latency(self, org_id: str, as_of_generation: str) -> dict[str, Any]:
        samples: dict[tuple[str, str], list[float]] = defaultdict(list)
        for row in self.samples:
            if row["org_id"] != org_id:
                continue
            samples[(row["stage"], row["metric"])].append(float(row["value_ms"]))
        items = []
        sample_percentiles: dict[str, Any] = {}
        for (stage, metric), values in sorted(samples.items()):
            stats = _percentile_stats(values)
            stats["metric"] = metric
            sample_percentiles[stage] = stats
            items.append(
                {
                    "stage": stage,
                    "metric": metric,
                    "count": stats["n"],
                    "p50": stats["p50"],
                    "p95": stats["p95"],
                }
            )
        provider = [row for row in self.aggregates if row["org_id"] == org_id]
        return {
            "as_of_generation": as_of_generation or self.generation,
            "sample_percentiles": sample_percentiles,
            "provider_aggregates": provider,
            "aggregates_excluded": len(provider),
            "items": items,
            "note": "sample percentiles exclude AggregateMeasurement provider statistics; one serving generation only",
        }


def latency_from_store_or_calls(
    calls: list[CallRevision],
    *,
    as_of_generation: str,
    store: MemoryRollupStore | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if store is not None and store.samples:
        return store.latency(org_id or (calls[0].org_id if calls else ""), as_of_generation)
    return build_latency_rollup(calls, as_of_generation)
