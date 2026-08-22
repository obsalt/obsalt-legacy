"""Calibration harness: labelled calls vs a rubric before fleet rollout (§9.6)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

from obsalt.analysis.evals import rubric_to_request
from obsalt.analysis.judge import HeuristicJudge
from obsalt.domain.models import CallRevision, Rubric


class CalibrationReport(TypedDict):
    rubric_id: str
    rubric_version: int
    n: int
    agree: int
    agreement: float | None
    items: list[dict[str, Any]]
    note: str


async def calibrate_rubric(
    rubric: Rubric,
    labelled: Sequence[tuple[CallRevision, bool]],
    *,
    judge: Any | None = None,
) -> CalibrationReport:
    """Run a rubric against a labelled set and report agreement."""

    judge_impl = judge or HeuristicJudge()
    rows: list[dict[str, Any]] = []
    agree = 0
    for call, expected_pass in labelled:
        judged = await judge_impl.judge(rubric_to_request(call, rubric))
        match = judged.passed is expected_pass
        if match:
            agree += 1
        rows.append(
            {
                "call_id": call.call_id,
                "revision": call.revision,
                "expected_pass": expected_pass,
                "actual_pass": judged.passed,
                "score": judged.score,
                "agree": match,
                "rationale": judged.rationale,
            }
        )
    total = len(labelled)
    return {
        "rubric_id": rubric.id,
        "rubric_version": rubric.version,
        "n": total,
        "agree": agree,
        "agreement": (agree / total) if total else None,
        "items": rows,
        "note": "calibration set only; not a fleet statistic",
    }
