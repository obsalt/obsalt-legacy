"""Serving queries over the active-revision pointer. Never ask storage for 'latest'."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from obsalt.analysis.cluster import cluster_hangups
from obsalt.analysis.rollups import build_latency_rollup, build_quality_rollup, build_tools_rollup
from obsalt.domain.models import AnalysisResult, CallRevision
from obsalt.search.index import MemorySearchIndex


def active_calls(state: Any, org_id: str) -> list[CallRevision]:
    """Hydrate the Postgres/memory active-call pointer with an exact revision get."""
    items: list[CallRevision] = []
    lister = getattr(state.pointers, "list_org", None)
    if callable(lister):
        for call_id, revision in lister(org_id):
            rev = state.sink.get(org_id, call_id, revision)
            if rev is None or rev.org_id != org_id:
                continue
            items.append(rev)
    else:
        revisions = getattr(state.sink, "revisions", {})
        for rev in revisions.values():
            if rev.org_id != org_id:
                continue
            if state.pointers.get(rev.org_id, rev.call_id) != rev.revision:
                continue
            items.append(rev)
    items.sort(key=lambda r: (r.started_at or r.created_at, r.call_id), reverse=True)
    return items


def in_range(call: CallRevision, start: datetime, end: datetime) -> bool:
    ts = call.started_at or call.created_at
    return start <= ts <= end


def call_list_item(rev: CallRevision) -> dict[str, Any]:
    return {
        "id": rev.call_id,
        "revision": rev.revision,
        "source": rev.source,
        "agent_id": rev.agent_id,
        "status": rev.status.value,
        "started_at": rev.started_at.isoformat() if rev.started_at else None,
        "timeline_fidelity": rev.timeline_fidelity.value,
        "pipeline_architecture": rev.pipeline_architecture.value,
        "hangup": rev.hangup.reason.value if rev.hangup else None,
        "decoder_version": rev.decoder_version,
    }


def paginate_calls(
    items: list[dict[str, Any]], *, cursor: str | None, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Cursor is `{call_id}:{revision}` so a page never mixes serving identities."""
    start = 0
    if cursor:
        for index, item in enumerate(items):
            if f"{item['id']}:{item['revision']}" == cursor:
                start = index + 1
                break
    page = items[start : start + limit]
    next_cursor = None
    if start + limit < len(items) and page:
        last = page[-1]
        next_cursor = f"{last['id']}:{last['revision']}"
    return page, next_cursor


def sample_percentile(values: list[float], pct: float) -> float | None:
    """Sample percentile. Provider AggregateMeasurements must never enter this list."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    n = max(1, int(round((len(values) - 1) * pct / 100.0)))
    ordered = sorted(values)
    return ordered[min(n, len(ordered) - 1)]


def latency_rollup(calls: list[CallRevision], *, as_of_generation: str) -> dict[str, Any]:
    data = build_latency_rollup(calls, as_of_generation)
    items = []
    for stage, stats in (data.get("sample_percentiles") or {}).items():
        items.append(
            {
                "stage": stage,
                "metric": stats.get("metric"),
                "count": stats.get("n"),
                "p50": stats.get("p50"),
                "p95": stats.get("p95"),
            }
        )
    data["items"] = items
    data["aggregates_excluded"] = len(data.get("provider_aggregates") or [])
    return data


def tools_rollup(calls: list[CallRevision], *, as_of_generation: str) -> dict[str, Any]:
    data = build_tools_rollup(calls, as_of_generation)
    by_tool = data.get("by_tool") or {}
    data["items"] = [{"name": name, **stats} for name, stats in by_tool.items()]
    return data


def hangup_rollup(calls: list[CallRevision], *, as_of_generation: str) -> dict[str, Any]:
    data = cluster_hangups(calls, as_of_generation)
    data["items"] = [
        {"reason": row["reason"], "count": row["size"], "call_ids": row["call_ids"]}
        for row in data.get("clusters") or []
    ]
    return data


def quality_rollup(calls: list[CallRevision], analysis: object, *, as_of_generation: str) -> dict[str, Any]:
    rows: list[AnalysisResult] = []
    if isinstance(analysis, dict):
        for value in analysis.values():
            if isinstance(value, list):
                rows.extend(value)
            elif value is not None:
                rows.append(value)
    elif isinstance(analysis, list):
        rows = analysis
    data = build_quality_rollup(calls, rows, as_of_generation)
    data["flag_count"] = data.get("hallucinations", {}).get("count", 0)
    return data


def search_calls(
    calls: list[CallRevision],
    query: str,
    *,
    index: Any | None = None,
    org_id: str | None = None,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if index is not None and hasattr(index, "query") and not isinstance(index, MemorySearchIndex):
        result = index.query(org_id or "", query, filters=filters)
        return list(result.get("items") or [])
    search_index = MemorySearchIndex()
    for call in calls:
        search_index.index(call)
    result = search_index.query(query, filters=filters)
    return list(result.get("items") or [])
