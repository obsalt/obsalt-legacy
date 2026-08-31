"""Regression fixtures from confirmed failures. Not a simulator."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.eval_case import SCHEMA, build_eval_case
from obsalt.domain.enums import HangupReason, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, Hangup, ToolInvocation, Turn
from obsalt.ui.present import present_flags, present_quality


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c-eval-1",
        revision="r1",
        source="vapi",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="I want a refund for my order."),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I've processed a refund for order ORD-99999 totaling $48.50.",
            ),
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                status=ToolStatus.ERROR,
                error="not_found",
            )
        ],
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_eval_case_lists_forbidden_spans_and_tool_result() -> None:
    case = build_eval_case(_call())
    assert case["schema"] == SCHEMA
    assert case["severity"] == "critical"
    assert any("ORD-99999" in span or "$48.50" in span for span in case["forbidden_claims"])
    assert case["tools"][0]["name"] == "lookup_order"
    assert "not_found" in case["tools"][0]["result"]
    assert case["caller_goal"] == "I want a refund for my order."
    rows = case["claims"]
    assert rows
    assert any(
        item.get("agent_span") in {"$48.50", "ORD-99999"} or item.get("agent_span") for item in rows
    )
    assert any(item.get("tool_id") == "t1" for item in rows)


def test_eval_case_exports_effective_tool_failure() -> None:
    case = build_eval_case(
        _call(
            tools=[
                ToolInvocation(
                    id="t1",
                    name="lookup_order",
                    status=ToolStatus.SUCCESS,
                    result='{"error": "not_found"}',
                )
            ]
        )
    )
    assert case["tools"][0]["status"] != "success"
    assert case["tools"][0]["status"] == "failed"


def test_detector_flag_label_names_the_tool_contradiction() -> None:
    items = present_flags(
        [
            {
                "kind": "price_claim",
                "verdict": "contradicted",
                "model": "detector/1",
                "span_text": "$48.50",
            }
        ]
    )
    assert items[0]["tone"] == "loss"
    assert "contradicted by tool result" in items[0]["label"]


def test_quality_hero_exposes_coverage_not_a_fake_pass_rate() -> None:
    view = present_quality(
        {
            "eligible": 10,
            "evals": {"eligible": 10, "completed": 0, "passed": 0, "failed": 0},
            "hallucinations": {
                "count": 1,
                "evidence_missing": 3,
                "not_settled": 4,
                "checkable": 7,
                "scanned": 8,
                "unscannable": 2,
            },
            "quality_card": {"calls": 4, "critical": 1, "evidence_missing": 3, "by_dimension": {}},
        },
        0.0,
        0.0,
    )
    assert view["eligible"] == 10
    assert view["hallucination_count"] == 1
    assert view["not_settled_calls"] == 4
    assert view["evidence_missing_calls"] == 3
    assert view["checkable"] == 7
    assert view["accuracy_dims"] == []
