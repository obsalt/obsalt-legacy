from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from obsalt.domain.enums import ToolStatus
from obsalt.domain.models import CanonicalCall, ToolInvocation
from obsalt.domain.redact import payload_shape, preview_text, redact_value
from obsalt.latency.stats import percentile
from obsalt.util import canonical_json, sha256_text


def parse_arguments(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_unparsed": text[:200]}
    return {"_value": raw}


def enrich_tool(tool: ToolInvocation, arguments: Any | None = None, result: Any | None = None) -> ToolInvocation:
    parsed = parse_arguments(arguments) if arguments is not None else parse_arguments(tool.metadata.get("arguments"))
    shape = payload_shape(parsed)
    tool.payload_shape = shape
    redacted = redact_value(None, parsed)
    tool.argument_hash = sha256_text(canonical_json({"shape": shape, "args": redacted}))
    if result is not None and tool.result_preview is None:
        tool.result_preview = preview_text(result)
    if tool.status == ToolStatus.PENDING and result is not None:
        tool.status = ToolStatus.SUCCESS
    return tool


def retry_counts(tools: list[ToolInvocation]) -> None:
    """Consecutive invocations of the same tool name in a call are retries after a failure."""
    last_failed: dict[str, int] = {}
    for tool in tools:
        prior = last_failed.get(tool.name)
        if prior is not None:
            tool.retry_count = prior + 1
        if tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
            last_failed[tool.name] = tool.retry_count
        else:
            last_failed.pop(tool.name, None)


def attach_time_to_tool(call: CanonicalCall) -> None:
    user_starts = [
        (t.seconds_from_start, t.started_at)
        for t in call.turns
        if t.speaker.value == "user" and (t.seconds_from_start is not None or t.started_at is not None)
    ]
    for tool in call.tools:
        if tool.time_to_tool_ms is not None:
            continue
        if tool.started_at is None and not user_starts:
            continue
        if tool.started_at is not None:
            prior = None
            for _sec, started in user_starts:
                if started and started <= tool.started_at:
                    prior = started
            if prior is not None:
                tool.time_to_tool_ms = max(0.0, (tool.started_at - prior).total_seconds() * 1000.0)


def enrich_call_tools(call: CanonicalCall) -> CanonicalCall:
    for tool in call.tools:
        enrich_tool(tool)
    retry_counts(call.tools)
    attach_time_to_tool(call)
    for tool in call.tools:
        if tool.duration_ms is not None:
            continue
        if tool.started_at and tool.ended_at:
            tool.duration_ms = max(0.0, (tool.ended_at - tool.started_at).total_seconds() * 1000.0)
    return call


@dataclass
class ToolRollup:
    name: str
    invocations: int
    success_rate: float
    retry_rate: float
    p50_ms: float | None
    p95_ms: float | None
    payload_shapes: list[dict[str, Any]] = field(default_factory=list)


def rollup_tools(calls: list[CanonicalCall]) -> list[ToolRollup]:
    by_name: dict[str, list[ToolInvocation]] = defaultdict(list)
    for call in calls:
        for tool in call.tools:
            by_name[tool.name].append(tool)
    rollups: list[ToolRollup] = []
    for name, items in sorted(by_name.items()):
        durations = sorted(t.duration_ms for t in items if t.duration_ms is not None)
        successes = sum(1 for t in items if t.status == ToolStatus.SUCCESS)
        retries = sum(1 for t in items if t.retry_count > 0)
        shapes = Counter(canonical_json(t.payload_shape) for t in items)
        rollups.append(
            ToolRollup(
                name=name,
                invocations=len(items),
                success_rate=round(successes / len(items), 4) if items else 0.0,
                retry_rate=round(retries / len(items), 4) if items else 0.0,
                p50_ms=round(percentile(durations, 50) or 0.0, 3) if durations else None,
                p95_ms=round(percentile(durations, 95) or 0.0, 3) if durations else None,
                payload_shapes=[
                    {"shape": json.loads(shape) if shape != "null" else None, "count": count}
                    for shape, count in shapes.most_common(8)
                ],
            )
        )
    return rollups
