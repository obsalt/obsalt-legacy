"""Correction-aware fleet rollups over a single serving generation.

Sample percentiles are computed only from ``StageMeasurement`` rows. Provider
``AggregateMeasurement`` statistics are returned separately and never enter
sample p50/p95 (T1 / §9.2). Every response is labelled with one
``as_of_generation``; callers must not mix generations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from obsalt.analysis.hallucination import tool_effectively_failed, tool_effectively_succeeded
from obsalt.domain.enums import AnalysisState, JudgeVerdict, Metric
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


_STATE_RANK = {
    AnalysisState.SAMPLED_OUT: 1,
    AnalysisState.PENDING: 2,
    AnalysisState.BUDGET_BLOCKED: 3,
    AnalysisState.RUNNING: 4,
    AnalysisState.FAILED: 5,
    AnalysisState.COMPLETED: 6,
}


def latest_analysis_results(rows: Sequence[AnalysisResult]) -> list[AnalysisResult]:
    """One serving row per (call, revision, analyzer, rubric version).

    State first (COMPLETED beats PENDING), then integer analyzer_version, then list index.
    """
    chosen: dict[tuple[str, str, str, str], tuple[int, int, int, AnalysisResult]] = {}
    for index, result in enumerate(rows):
        execution = result.execution
        key = (
            execution.call_id,
            execution.revision,
            execution.analyzer_id,
            execution.rubric_version or "",
        )
        rank = _STATE_RANK.get(execution.state, 0)
        version = _version_int(execution.analyzer_version)
        previous = chosen.get(key)
        if previous is None or (rank, version, index) >= previous[:3]:
            chosen[key] = (rank, version, index, result)
    return [item[3] for item in sorted(chosen.values(), key=lambda row: row[2])]


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
    confirmed_calls: set[str] = set()
    confirmed_by_kind: dict[str, set[str]] = defaultdict(set)
    confirmed_claims = 0
    candidate_calls: set[str] = set()
    candidate_by_kind: dict[str, set[str]] = defaultdict(set)
    candidate_claims = 0
    review_queue: list[dict[str, Any]] = []
    by_rubric: dict[str, dict[str, Any]] = {}
    card_calls = 0
    critical_calls: set[str] = set()
    evidence_missing_calls: set[str] = set()
    groundedness_calls: set[str] = set()
    groundedness_spans = 0
    groundedness_not_judged = 0
    behavior_calls: set[str] = set()
    behavior_by_kind: dict[str, set[str]] = defaultdict(set)

    active = {(call.call_id, call.revision) for call in calls}
    eligible = len(calls)
    scanned_ids = {call.call_id for call in calls if any(turn.text for turn in call.agent_turns())}
    scanned = len(scanned_ids)
    unscannable = eligible - scanned
    checkable = sum(
        1
        for call in calls
        if call.tools or call.grounding or any(turn.text for turn in call.user_turns())
    )

    for result in latest_analysis_results(analysis_results):
        key = (result.execution.call_id, result.execution.revision)
        if active and key not in active:
            continue
        state_counts[result.execution.state.value] = (
            state_counts.get(result.execution.state.value, 0) + 1
        )
        payload = result.payload
        selection = str(payload.get("selection") or payload.get("trigger") or "")
        rubric_key = result.execution.rubric_version or result.execution.analyzer_id
        rubric_row = None
        if _is_eval(result):
            rubric_row = by_rubric.setdefault(
                rubric_key,
                {"completed": 0, "passed": 0, "failed": 0, "eligible": 0},
            )
            rubric_row["eligible"] += 1

        if _is_eval(result) and rubric_row is not None:
            if result.execution.state is AnalysisState.COMPLETED:
                if _binary_eval_verdict(payload):
                    evals_completed += 1
                    rubric_row["completed"] += 1
                    passed = payload.get("passed") is True
                    if passed:
                        evals_passed += 1
                        rubric_row["passed"] += 1
                    else:
                        evals_failed += 1
                        rubric_row["failed"] += 1
                        if not payload.get("shadow"):
                            review_queue.append(_queue_item(result, "eval_fail", severity="high"))
                    if selection == "baseline_sample":
                        baseline_completed += 1
                        if passed:
                            baseline_passed += 1
                        else:
                            baseline_failed += 1
            elif result.execution.state is not AnalysisState.SAMPLED_OUT:
                review_queue.append(_queue_item(result, "eval_incomplete", severity="medium"))

        call_id = result.execution.call_id
        kinds = _hallucination_kinds(result)
        if kinds and result.execution.analyzer_id == "hallucination":
            confirmed_calls.add(call_id)
            confirmed_claims += len(kinds)
            for kind in set(kinds):
                confirmed_by_kind[kind].add(call_id)
            if _confirmed_is_pageable(result):
                critical_calls.add(call_id)
            review_queue.append(
                _queue_item(
                    result, "hallucination", severity=_claim_severity(result, confirmed=True)
                )
            )
        if result.execution.analyzer_id == "hallucination" and _has_evidence_missing(result):
            evidence_missing_calls.add(call_id)
        pending_kinds = _candidate_kinds(result)
        if pending_kinds:
            candidate_calls.add(call_id)
            candidate_claims += len(pending_kinds)
            for kind in set(pending_kinds):
                candidate_by_kind[kind].add(call_id)

        if result.execution.analyzer_id == "quality_card":
            card_calls += 1
            if payload.get("critical_failure"):
                review_queue.append(_queue_item(result, "hallucination", severity="critical"))
        if result.execution.analyzer_id == "groundedness":
            groundedness_calls.add(call_id)
            spans = payload.get("spans") or []
            if isinstance(spans, list):
                groundedness_spans += len(spans)
            if str(payload.get("status") or "") == "not_judged":
                groundedness_not_judged += 1
        if result.execution.analyzer_id == "flags":
            kinds = [
                str(item.get("kind"))
                for item in payload.get("flags") or []
                if isinstance(item, dict) and item.get("kind")
            ]
            if kinds:
                behavior_calls.add(call_id)
                for kind in kinds:
                    behavior_by_kind[kind].add(call_id)

    review_queue = _dedupe_queue(review_queue)
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
            "count": len(confirmed_calls),
            "claim_count": confirmed_claims,
            "by_kind": {kind: len(ids) for kind, ids in sorted(confirmed_by_kind.items())},
            "candidate_count": len(candidate_calls),
            "candidate_claim_count": candidate_claims,
            "candidates_by_kind": {
                kind: len(ids) for kind, ids in sorted(candidate_by_kind.items())
            },
            "scanned": scanned,
            "unscannable": unscannable,
            "checkable": checkable,
            "evidence_missing": len(evidence_missing_calls),
            "not_settled": len(
                scanned_ids - confirmed_calls - evidence_missing_calls - candidate_calls
            ),
        },
        "quality_card": {
            "calls": card_calls,
            "critical": len(critical_calls),
            "evidence_missing": len(evidence_missing_calls),
            "by_dimension": {},
        },
        "groundedness": {
            "calls": len(groundedness_calls),
            "span_count": groundedness_spans,
            "not_judged": groundedness_not_judged,
        },
        "behavior_flags": {
            "calls": len(behavior_calls),
            "by_kind": {kind: len(ids) for kind, ids in sorted(behavior_by_kind.items())},
        },
        "review_queue": review_queue,
        "note": (
            "missing output is never interpreted as a passing call; one serving generation only; "
            "heuristic stand-in is not a confirmed hallucination"
        ),
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
    successes = sum(1 for _call, tool in rows if tool_effectively_succeeded(tool))
    failures = sum(1 for _call, tool in rows if tool_effectively_failed(tool))
    retries = sum(tool.retry_count for _call, tool in rows)
    durations = [
        tool.duration_ms
        for _call, tool in rows
        if tool.duration_ms is not None and tool.duration_ms > 0
    ]
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


_NON_EVAL_ANALYZERS = frozenset(
    {
        "hallucination",
        "flags",
        "hangup",
        "tools",
        "coverage",
        "tier1",
        "quality_card",
        "groundedness",
        "judge_faithfulness",
        "judge_task_outcome",
        "judge_conversation_flow",
        "judge_escalation",
    }
)


_OPEN_VERDICTS = frozenset(
    {
        JudgeVerdict.MAYBE.value,
        JudgeVerdict.NOT_APPLICABLE.value,
        JudgeVerdict.NOT_JUDGED.value,
        JudgeVerdict.EVIDENCE_MISSING.value,
    }
)


def _binary_eval_verdict(payload: Mapping[str, Any]) -> bool:
    """Pass/fail only. maybe / N/A / not_judged / evidence_missing are not fleet evals."""
    verdict = str(payload.get("verdict") or "")
    if verdict in _OPEN_VERDICTS:
        return False
    if payload.get("passed") is None and verdict not in {
        JudgeVerdict.PASS.value,
        JudgeVerdict.FAIL.value,
    }:
        return False
    return True


def _is_eval(result: AnalysisResult) -> bool:
    analyzer = result.execution.analyzer_id
    if analyzer in _NON_EVAL_ANALYZERS:
        return False
    if result.payload.get("shadow"):
        return False
    if analyzer in {"eval", "tier2", "tier2_eval", "rubric"} or analyzer.startswith(
        ("rubric:", "pack:")
    ):
        return True
    return "passed" in result.payload


def _candidate_kinds(result: AnalysisResult) -> list[str]:
    """Pending, heuristic, or evidence-missing claims. Not confirmed fleet failures."""
    from obsalt.analysis.hallucination import hallucination_claim_list

    if result.execution.analyzer_id != "hallucination":
        return []
    payload = result.payload
    source = hallucination_claim_list(payload)
    if result.execution.state is not AnalysisState.COMPLETED:
        source = list(payload.get("candidates") or source)
    kinds: list[str] = []
    for claim in source:
        if not isinstance(claim, dict) or not claim.get("kind"):
            continue
        if _is_confirmed_claim(claim, payload):
            continue
        verdict = str(claim.get("verdict") or "")
        model = str(claim.get("model") or payload.get("model") or "")
        if verdict == "evidence_missing" and model.startswith("detector"):
            continue
        if verdict == "grounded":
            continue
        kinds.append(str(claim["kind"]))
    return kinds


def _hallucination_kinds(result: AnalysisResult) -> list[str]:
    """Confirmed flags: detector or calibrated judge. Heuristic is never confirmed."""
    if result.execution.state is not AnalysisState.COMPLETED:
        return []
    payload = result.payload
    kinds: list[str] = []
    for claim in payload.get("claims") or []:
        if _is_confirmed_claim(claim, payload):
            kinds.append(str(claim["kind"]))
    return kinds


def _version_int(value: str | None) -> int:
    try:
        return int(value or "0")
    except ValueError:
        return 0


def _has_evidence_missing(result: AnalysisResult) -> bool:
    from obsalt.analysis.hallucination import hallucination_claim_list

    payload = result.payload
    for claim in hallucination_claim_list(payload):
        if not isinstance(claim, dict):
            continue
        if str(claim.get("verdict") or "") != "evidence_missing":
            continue
        model = str(claim.get("model") or payload.get("model") or "")
        if model.startswith("detector"):
            return True
    return False


def _confirmed_is_pageable(result: AnalysisResult) -> bool:
    payload = result.payload
    for claim in payload.get("claims") or []:
        if not _is_confirmed_claim(claim, payload):
            continue
        if str(claim.get("severity") or "medium") in {"critical", "high"}:
            return True
    return False


def _is_confirmed_claim(claim: Any, payload: dict[str, Any]) -> bool:
    if not isinstance(claim, dict) or not claim.get("kind"):
        return False
    if claim.get("verdict") != "contradicted":
        return False
    model = str(claim.get("model") or payload.get("model") or "")
    return model.startswith("detector")


def _queue_item(result: AnalysisResult, kind: str, *, severity: str = "medium") -> dict[str, Any]:
    return {
        "call_id": result.execution.call_id,
        "revision": result.execution.revision,
        "kind": kind,
        "state": result.execution.state.value,
        "analyzer_id": result.execution.analyzer_id,
        "passed": result.payload.get("passed"),
        "selection": result.payload.get("selection") or result.payload.get("trigger"),
        "severity": severity,
    }


_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "needs_review": 3, "low": 4}


def _claim_severity(result: AnalysisResult, *, confirmed: bool) -> str:
    ranks: list[int] = []
    from obsalt.analysis.hallucination import hallucination_claim_list

    for claim in hallucination_claim_list(result.payload):
        if not isinstance(claim, dict):
            continue
        if confirmed and not _is_confirmed_claim(claim, result.payload):
            continue
        sev = str(claim.get("severity") or "medium")
        ranks.append(_SEV_RANK.get(sev, 2))
    if not ranks:
        return "high" if confirmed else "needs_review"
    best = min(ranks)
    for name, rank in _SEV_RANK.items():
        if rank == best:
            return name
    return "medium"


def _dedupe_queue(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per call. Critical before high. Low never pages the queue."""
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        if str(item.get("severity") or "") == "low":
            continue
        call_id = str(item.get("call_id") or "")
        previous = best.get(call_id)
        rank = _SEV_RANK.get(str(item.get("severity") or "medium"), 9)
        if previous is None or rank < _SEV_RANK.get(str(previous.get("severity") or "medium"), 9):
            best[call_id] = item
    return sorted(
        best.values(),
        key=lambda item: (
            _SEV_RANK.get(str(item.get("severity") or "medium"), 9),
            item.get("call_id") or "",
        ),
    )
