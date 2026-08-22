"""Correction-aware fleet rollups over a single serving generation.

Sample percentiles are computed only from ``StageMeasurement`` rows. Provider
``AggregateMeasurement`` statistics are returned separately and never enter
sample p50/p95 (T1 / §9.2). Every response is labelled with one
``as_of_generation``; callers must not mix generations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from obsalt.domain.enums import AnalysisState, Metric, ToolStatus
from obsalt.domain.models import (
    AggregateMeasurement,
    AnalysisResult,
    CallRevision,
    StageMeasurement,
    ToolInvocation,
)
from obsalt.util import canonical_json, duration_ms


def build_latency_rollup(calls: Sequence[CallRevision], as_of_generation: str) -> dict[str, Any]:
    """P50/P95 per stage from sample measurements only, plus labelled provider aggregates."""
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    samples_by_agent: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for call in calls:
        for measurement in call.stage_measurements:
            _accumulate_sample(samples, samples_by_agent, call.agent_id, measurement)

    sample_percentiles: dict[str, Any] = {}
    for (stage, metric), values in sorted(samples.items()):
        stats = _percentile_stats(values)
        stats["metric"] = metric
        agent_rows: dict[str, Any] = {}
        for (agent_id, agent_stage, agent_metric), agent_values in sorted(samples_by_agent.items()):
            if agent_stage == stage and agent_metric == metric:
                agent_rows[agent_id] = _percentile_stats(agent_values)
        stats["by_agent"] = agent_rows
        stage_entry = sample_percentiles.setdefault(stage, {})
        stage_entry[metric] = stats
        if metric == Metric.DURATION.value or "p50" not in stage_entry:
            stage_entry["p50"] = stats["p50"]
            stage_entry["p95"] = stats["p95"]
            stage_entry["n"] = stats["n"]
            stage_entry["metric"] = metric

    by_agent: dict[str, Any] = {}
    for (agent_id, stage, metric), values in sorted(samples_by_agent.items()):
        agent_entry = by_agent.setdefault(agent_id, {})
        stats = _percentile_stats(values)
        stats["metric"] = metric
        stage_entry = agent_entry.setdefault(stage, {})
        stage_entry[metric] = stats
        if metric == Metric.DURATION.value or "p50" not in stage_entry:
            stage_entry["p50"] = stats["p50"]
            stage_entry["p95"] = stats["p95"]
            stage_entry["n"] = stats["n"]
            stage_entry["metric"] = metric

    provider_aggregates = [
        _aggregate_row(call, item) for call in calls for item in call.aggregate_measurements
    ]
    items = [
        {
            "stage": stage,
            "metric": stats.get("metric"),
            "count": stats.get("n"),
            "n": stats.get("n"),
            "p50": stats.get("p50"),
            "p95": stats.get("p95"),
        }
        for stage, stats in sample_percentiles.items()
    ]
    return {
        "as_of_generation": as_of_generation,
        "sample_percentiles": sample_percentiles,
        "by_agent": by_agent,
        "provider_aggregates": provider_aggregates,
        "aggregates_excluded": len(provider_aggregates),
        "items": items,
        "call_count": len(calls),
        "note": (
            "sample percentiles exclude AggregateMeasurement provider statistics; "
            "one serving generation only"
        ),
    }


def build_tools_rollup(calls: Sequence[CallRevision], as_of_generation: str) -> dict[str, Any]:
    """Success/retry/duration/shape telemetry per tool. Missing duration stays missing."""
    grouped: dict[str, list[tuple[CallRevision, ToolInvocation]]] = defaultdict(list)
    grouped_agent: dict[tuple[str, str], list[tuple[CallRevision, ToolInvocation]]] = defaultdict(
        list
    )
    for call in calls:
        for tool in call.tools:
            grouped[tool.name].append((call, tool))
            grouped_agent[(call.agent_id, tool.name)].append((call, tool))

    by_tool = {name: _tool_stats(rows) for name, rows in sorted(grouped.items())}
    by_agent: dict[str, dict[str, Any]] = {}
    for (agent_id, name), rows in sorted(grouped_agent.items()):
        by_agent.setdefault(agent_id, {})[name] = _tool_stats(rows)

    return {
        "as_of_generation": as_of_generation,
        "by_tool": by_tool,
        "by_agent": by_agent,
        "invocation_count": sum(len(rows) for rows in grouped.values()),
        "note": "duration percentiles use only tools that reported duration_ms; never invented",
    }


def build_quality_rollup(
    calls: Sequence[CallRevision],
    analysis_results: Sequence[AnalysisResult],
    as_of_generation: str,
) -> dict[str, Any]:
    """Eval + hallucination fleet view. Missing output is never a pass."""
    state_counts = {state.value: 0 for state in AnalysisState}
    evals_completed = 0
    evals_passed = 0
    evals_failed = 0
    baseline_completed = 0
    baseline_passed = 0
    baseline_failed = 0
    hallucinations_by_kind: dict[str, int] = defaultdict(int)
    hallucination_count = 0
    review_queue: list[dict[str, Any]] = []
    by_rubric: dict[str, dict[str, Any]] = {}

    active = {(call.call_id, call.revision) for call in calls}
    eligible = len(calls)

    for result in analysis_results:
        key = (result.execution.call_id, result.execution.revision)
        if active and key not in active:
            continue
        state_counts[result.execution.state.value] = (
            state_counts.get(result.execution.state.value, 0) + 1
        )
        payload = result.payload
        selection = str(payload.get("selection") or payload.get("trigger") or "")
        rubric_key = result.execution.rubric_version or result.execution.analyzer_id
        rubric_row = by_rubric.setdefault(
            rubric_key,
            {"completed": 0, "passed": 0, "failed": 0, "eligible": 0},
        )
        rubric_row["eligible"] += 1

        if _is_eval(result):
            if result.execution.state is AnalysisState.COMPLETED:
                evals_completed += 1
                rubric_row["completed"] += 1
                passed = payload.get("passed") is True
                if passed:
                    evals_passed += 1
                    rubric_row["passed"] += 1
                else:
                    evals_failed += 1
                    rubric_row["failed"] += 1
                    review_queue.append(_queue_item(result, "eval_fail"))
                if selection == "baseline_sample":
                    baseline_completed += 1
                    if passed:
                        baseline_passed += 1
                    else:
                        baseline_failed += 1
            elif result.execution.state is not AnalysisState.SAMPLED_OUT:
                review_queue.append(_queue_item(result, "eval_incomplete"))

        kinds = _hallucination_kinds(result)
        if kinds:
            hallucination_count += len(kinds)
            for kind in kinds:
                hallucinations_by_kind[kind] += 1
            review_queue.append(_queue_item(result, "hallucination"))

    review_queue.sort(key=lambda item: (item.get("call_id") or "", item.get("kind") or ""))
    return {
        "as_of_generation": as_of_generation,
        "eligible": eligible,
        "evals": {
            "eligible": eligible,
            "completed": evals_completed,
            "passed": evals_passed,
            "failed": evals_failed,
            "by_rubric": by_rubric,
        },
        "executions": state_counts,
        "baseline": {
            "completed": baseline_completed,
            "passed": baseline_passed,
            "failed": baseline_failed,
            "note": "unbiased baseline-sample statistics only",
        },
        "hallucinations": {
            "count": hallucination_count,
            "by_kind": dict(sorted(hallucinations_by_kind.items())),
        },
        "review_queue": review_queue,
        "note": "missing output is never interpreted as a passing call; one serving generation only",
    }


def _accumulate_sample(
    samples: dict[tuple[str, str], list[float]],
    samples_by_agent: dict[tuple[str, str, str], list[float]],
    agent_id: str,
    measurement: StageMeasurement,
) -> None:
    key = (measurement.stage.value, measurement.metric.value)
    samples[key].append(measurement.value_ms)
    samples_by_agent[(agent_id, measurement.stage.value, measurement.metric.value)].append(
        measurement.value_ms
    )


def _percentile_stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "p50": _percentile(values, 50.0),
        "p95": _percentile(values, 95.0),
        "n": len(values),
        "source": "samples",
    }


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    h = (p / 100.0) * (n - 1)
    lo = int(h)
    hi = min(lo + 1, n - 1)
    frac = h - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _aggregate_row(call: CallRevision, item: AggregateMeasurement) -> dict[str, Any]:
    return {
        "call_id": call.call_id,
        "agent_id": call.agent_id,
        "stage": item.stage.value,
        "metric": item.metric.value,
        "statistic": item.statistic.value,
        "value_ms": item.value_ms,
        "population": item.population,
        "window": item.window,
        "provenance": item.provenance.value,
        "source_path": item.source_path,
        "label": "provider_published",
        "source": "aggregate_measurement",
    }


def _tool_stats(rows: Sequence[tuple[CallRevision, ToolInvocation]]) -> dict[str, Any]:
    count = len(rows)
    successes = sum(1 for _call, tool in rows if tool.status is ToolStatus.SUCCESS)
    failures = sum(
        1 for _call, tool in rows if tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}
    )
    retries = sum(tool.retry_count for _call, tool in rows)
    durations = [tool.duration_ms for _call, tool in rows if tool.duration_ms is not None]
    ttt: list[float] = []
    shapes: dict[str, int] = defaultdict(int)
    for call, tool in rows:
        shapes[_shape_key(tool.payload_shape)] += 1
        if call.started_at is not None and tool.started_at is not None:
            gap = duration_ms(call.started_at, tool.started_at)
            if gap is not None:
                ttt.append(gap)
    return {
        "count": count,
        "successes": successes,
        "failures": failures,
        "success_rate": (successes / count) if count else None,
        "retries": retries,
        "retry_rate": (retries / count) if count else None,
        "duration_p50_ms": _percentile(durations, 50.0),
        "duration_p95_ms": _percentile(durations, 95.0),
        "duration_n": len(durations),
        "time_to_tool_p50_ms": _percentile(ttt, 50.0),
        "payload_shapes": dict(sorted(shapes.items())),
    }


def _shape_key(shape: Any) -> str:
    if shape is None:
        return ""
    if isinstance(shape, str):
        return shape
    return canonical_json(shape)


_NON_EVAL_ANALYZERS = frozenset({"hallucination", "flags", "hangup", "tools", "coverage", "tier1"})


def _is_eval(result: AnalysisResult) -> bool:
    analyzer = result.execution.analyzer_id
    if analyzer in _NON_EVAL_ANALYZERS:
        return False
    if analyzer in {"eval", "tier2", "tier2_eval", "rubric"}:
        return True
    return "passed" in result.payload


def _hallucination_kinds(result: AnalysisResult) -> list[str]:
    """Confirmed hallucination flags only. Pending Tier-1 candidates are not fleet failures."""
    if result.execution.state is not AnalysisState.COMPLETED:
        return []
    payload = result.payload
    kinds: list[str] = []
    for claim in payload.get("claims") or []:
        if (
            isinstance(claim, dict)
            and claim.get("kind")
            and claim.get("verdict") in {"contradicted", "unsupported"}
        ):
            kinds.append(str(claim["kind"]))
    return kinds


def _queue_item(result: AnalysisResult, kind: str) -> dict[str, Any]:
    return {
        "call_id": result.execution.call_id,
        "revision": result.execution.revision,
        "kind": kind,
        "state": result.execution.state.value,
        "analyzer_id": result.execution.analyzer_id,
        "passed": result.payload.get("passed"),
        "selection": result.payload.get("selection") or result.payload.get("trigger"),
    }
