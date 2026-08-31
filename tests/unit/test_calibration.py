"""Uncalibrated rubric evals never count as fleet passes."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.calibration import (
    DEFAULT_AGREEMENT_THRESHOLD,
    calibration_key,
    is_calibrated,
)
from obsalt.analysis.rollups import build_quality_rollup
from obsalt.domain.enums import AnalysisState, Speaker
from obsalt.domain.models import (
    AnalysisExecution,
    AnalysisResult,
    CallRevision,
    Turn,
)


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        turns=[],
        tools=[],
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_uncalibrated_rubric_eval_is_not_a_fleet_pass() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="Hello")])
    shadow = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r1",
            analyzer_id="tier2",
            analyzer_version="1",
            state=AnalysisState.COMPLETED,
        ),
        payload={"passed": True, "shadow": True, "calibrated": False, "selection": "manual"},
    )
    roll = build_quality_rollup([call], [shadow], "g1")
    assert roll["evals"]["completed"] == 0
    assert roll["evals"]["passed"] == 0


def test_calibration_key_threshold() -> None:
    store = {calibration_key("acme", "r1", 1): 0.95}
    assert is_calibrated(store, "acme", "r1", 1) is True
    assert is_calibrated(store, "acme", "r1", 2) is False
    store[calibration_key("acme", "r1", 2)] = DEFAULT_AGREEMENT_THRESHOLD - 0.1
    assert is_calibrated(store, "acme", "r1", 2) is False
