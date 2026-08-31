"""Tier 1 — every call, deterministic, cheap."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from obsalt.analysis.behavior import behavior_flags
from obsalt.analysis.compliance import compliance_flags
from obsalt.analysis.hangup import hangup_bucket, hangup_provider_code
from obsalt.analysis.tool_integrity import tool_integrity_flags
from obsalt.domain.enums import AnalysisState, HangupReason, ToolStatus
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.util import sha256_text

ANALYZER_VERSION = "2"
DEAD_AIR_SECONDS = 8.0


def analyze_tier1(call: CallRevision) -> list[AnalysisResult]:
    """Cheap deterministic analysis. Never mutates the immutable call revision."""
    results: list[AnalysisResult] = []
    if call.hangup is not None:
        results.append(
            _result(
                call,
                "hangup",
                {
                    "reason": hangup_bucket(call),
                    "provider_code": hangup_provider_code(call),
                },
            )
        )

    flags: list[dict[str, object]] = []
    if call.hangup and call.hangup.reason in {
        HangupReason.SILENCE_TIMEOUT,
        HangupReason.INACTIVITY,
    }:
        flags.append({"kind": "silence", "reason": call.hangup.reason.value})
    failed = [t for t in call.tools if t.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}]
    if failed:
        flags.append({"kind": "tool_failure", "tools": [t.name for t in failed]})
    low_conf = [t for t in call.turns if t.confidence is not None and t.confidence < 0.6]
    if low_conf:
        flags.append({"kind": "low_stt_confidence", "turns": [t.index for t in low_conf]})
    dead = _dead_air(call)
    if dead:
        flags.append({"kind": "dead_air", "gap_seconds": dead})
    if _truncated_llm(call):
        flags.append({"kind": "truncated_llm", "reason": "empty_or_length"})
    flags.extend(tool_integrity_flags(call))
    flags.extend(behavior_flags(call))
    flags.extend(compliance_flags(call))
    results.append(_result(call, "flags", {"flags": flags}))

    tool_roll = {
        "count": len(call.tools),
        "failures": len(failed),
        "names": sorted({t.name for t in call.tools}),
    }
    results.append(_result(call, "tools", tool_roll))
    results.append(
        _result(
            call,
            "coverage",
            {
                "timeline_fidelity": call.timeline_fidelity.value,
                "signals": [
                    {"signal": c.signal.value, "status": c.status.value, "reason": c.reason}
                    for c in call.coverage
                ],
            },
        )
    )
    return results


def _dead_air(call: CallRevision) -> float | None:
    timed = [turn for turn in call.turns if turn.started_at or turn.ended_at]
    epoch = datetime.min.replace(tzinfo=UTC)
    timed.sort(key=lambda turn: turn.started_at or turn.ended_at or epoch)
    widest = 0.0
    for prev, nxt in zip(timed, timed[1:], strict=False):
        prev_end = prev.ended_at or prev.started_at
        nxt_start = nxt.started_at or nxt.ended_at
        if prev_end is None or nxt_start is None:
            continue
        gap = (nxt_start - prev_end).total_seconds()
        if gap > widest:
            widest = gap
    if widest > DEAD_AIR_SECONDS:
        return widest
    return None


def _truncated_llm(call: CallRevision) -> bool:
    attrs = getattr(call, "unmapped_attributes", None) or {}
    finish = str(
        attrs.get("finish_reason") or attrs.get("gen_ai.response.finish_reason") or ""
    ).lower()
    if finish == "length":
        return True
    agents = call.agent_turns()
    if not agents:
        return False
    text = (agents[-1].text or "").rstrip()
    return (not text) or text.endswith("...") or text.endswith("…")


def _result(call: CallRevision, analyzer_id: str, payload: dict[str, Any]) -> AnalysisResult:
    execution = AnalysisExecution(
        call_id=call.call_id,
        revision=call.revision,
        analyzer_id=analyzer_id,
        analyzer_version=ANALYZER_VERSION,
        state=AnalysisState.COMPLETED,
        content_hash=sha256_text(call.revision + analyzer_id),
    )
    return AnalysisResult(execution=execution, payload=payload)
