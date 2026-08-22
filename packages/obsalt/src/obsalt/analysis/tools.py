from __future__ import annotations

from collections import defaultdict
from typing import Any

from obsalt.domain.models import CallRevision


def tool_telemetry(revision: CallRevision) -> dict[str, Any]:
    by_name: dict[str, list[Any]] = defaultdict(list)
    for tool in revision.tools:
        by_name[tool.name].append(tool)
    rows = []
    for name, items in sorted(by_name.items()):
        successes = sum(1 for t in items if t.status.value == "success")
        errors = sum(1 for t in items if t.status.value in {"error", "timeout"})
        durations = [t.duration_ms for t in items if t.duration_ms is not None]
        retries = 0
        prev = None
        for tool in items:
            if (
                prev is not None
                and prev.status.value in {"error", "timeout"}
                and tool.name == prev.name
                and tool.argument_hash
                and tool.argument_hash == prev.argument_hash
            ):
                retries += 1
            prev = tool
        rows.append(
            {
                "name": name,
                "count": len(items),
                "success_rate": successes / len(items) if items else None,
                "error_count": errors,
                "retry_count": retries,
                "duration_reported": len(durations) > 0,
                "durations_ms": durations,
                "shapes": [t.payload_shape for t in items],
            }
        )
    return {"tools": rows}
