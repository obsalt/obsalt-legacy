"""Quality Card is flags, not a dimension scorecard."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.hallucination import detect_claims, detector_payload
from obsalt.analysis.quality_card import (
    ANALYZER_VERSION,
    SCHEMA,
    card_dimension_status,
    compose_quality_card,
    quality_card_result,
)
from obsalt.analysis.rollups import build_quality_rollup
from obsalt.domain.enums import AnalysisState, HangupReason, Speaker, ToolStatus
from obsalt.domain.models import (
    AnalysisExecution,
    AnalysisResult,
    CallRevision,
    Hangup,
    ToolInvocation,
    Turn,
)
from obsalt.ui.present import present_quality, present_quality_card


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[],
        tools=[],
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def _hallo(call: CallRevision) -> AnalysisResult:
    return AnalysisResult(
        execution=AnalysisExecution(
            call_id=call.call_id,
            revision=call.revision,
            analyzer_id="hallucination",
            analyzer_version="2",
            state=AnalysisState.COMPLETED,
        ),
        payload=detector_payload(detect_claims(call)),
    )


def test_empty_grounding_card_is_evidence_missing_not_critical() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    card = compose_quality_card(call)
    assert "dimensions" not in card
    assert card["schema"] == SCHEMA
    assert card["counts"]["evidence_missing"] >= 1
    assert card["counts"]["confirmed"] == 0
    assert card["critical_failure"] is False
    assert "passed" not in card
    assert "claims" not in card


def test_honest_tool_failure_has_no_confirmed_flags() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I could not find that order.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            )
        ],
    )
    card = compose_quality_card(call)
    assert card["flag_kinds"] == []
    assert card["critical_failure"] is False
    assert present_quality_card(card) == []


def test_bound_phantom_is_critical() -> None:
    call = _call(
        turns=[
            Turn(
                index=0,
                speaker=Speaker.AGENT,
                text="I've processed a refund for order ORD-99999 totaling $48.50.",
            )
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                result='{"error": "not_found"}',
            )
        ],
    )
    card = compose_quality_card(call)
    assert "phantom_tool_success" in card["flag_kinds"]
    assert "price_claim" in card["flag_kinds"]
    assert card["critical_failure"] is True
    result = quality_card_result(call)
    assert result.execution.analyzer_id == "quality_card"
    assert result.execution.analyzer_version == ANALYZER_VERSION
    assert result.execution.state is AnalysisState.COMPLETED


def test_vacuous_pass_reason_is_not_judged_for_counts() -> None:
    assert (
        card_dimension_status({"status": "pass", "reason": "no tools on this call"}) == "not_judged"
    )
    assert (
        card_dimension_status({"status": "fail", "reason": "detector contradicted a claim"})
        == "fail"
    )


def test_quality_rollup_ignores_historical_dimensions() -> None:
    call = _call()
    result = quality_card_result(call)
    result.payload["dimensions"] = {
        "faithfulness": {"status": "pass", "reason": "no factual claims to check"},
        "tool_integrity": {"status": "pass", "reason": "no tools on this call"},
    }
    roll = build_quality_rollup([call], [result], "g1")
    assert roll["quality_card"]["by_dimension"] == {}
    view = present_quality(roll, 0.0, 0.0)
    assert view["accuracy_dims"] == []


def test_completed_hangup_without_tools_does_not_invent_task_pass() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="Goodbye.")])
    card = compose_quality_card(call)
    assert "dimensions" not in card
    assert "task_completion" not in card
    assert "abandonment" not in card
    assert card["flag_kinds"] == []


def test_quality_rollup_counts_confirmed_flags_not_dimensions() -> None:
    call = _call(
        turns=[
            Turn(
                index=0,
                speaker=Speaker.AGENT,
                text="I've processed a refund for order ORD-99999 totaling $48.50.",
            )
        ],
        tools=[
            ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR, error="not_found")
        ],
    )
    hallo = _hallo(call)
    result = quality_card_result(call)
    roll = build_quality_rollup([call], [hallo, result], "g1")
    assert roll["quality_card"]["calls"] == 1
    assert roll["quality_card"]["critical"] == 1
    assert roll["quality_card"]["by_dimension"] == {}
    assert roll["hallucinations"]["count"] == 1
    view = present_quality(roll, 0.0, 0.0)
    assert view["accuracy_dims"] == []
    assert view["hallucination_count"] == 1
    assert present_quality_card(result.payload) == []


def test_empty_grounding_price_is_evidence_missing_not_candidate() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    roll = build_quality_rollup([call], [_hallo(call)], "g1")
    assert roll["hallucinations"]["count"] == 0
    assert roll["hallucinations"]["candidate_count"] == 0
    assert roll["hallucinations"]["evidence_missing"] == 1
