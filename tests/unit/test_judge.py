"""No heuristic judge. Historical heuristic/1 rows stay unconfirmed."""

from __future__ import annotations

import asyncio

from obsalt.analysis.hallucination import extract_candidate_claims, grounding_corpus
from obsalt.analysis.judge import is_heuristic_result, judge_from_settings
from obsalt.domain.enums import (
    AnalysisState,
    GroundingKind,
    HangupReason,
    JudgeVerdict,
    Provenance,
    Speaker,
    ToolStatus,
)
from obsalt.domain.models import CallRevision, GroundingRef, Hangup, Rubric, ToolInvocation, Turn


def test_heuristic_result_detects_stored_model_and_claim() -> None:
    assert is_heuristic_result({"model": "heuristic/1"}) is True
    assert is_heuristic_result({"claims": [{"model": "heuristic/1"}]}) is True
    assert (
        is_heuristic_result({"model": "gpt-4.1-mini", "claims": [{"verdict": "contradicted"}]})
        is False
    )


def test_judge_from_settings_without_runner_is_none() -> None:
    assert judge_from_settings(None) is None


def test_english_rubric_without_runner_is_not_judged() -> None:
    from obsalt.analysis.tier2 import run_tier2

    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Thanks, I refunded $12.")],
    )
    rubric = Rubric(id="polite", org_id="acme", name="polite", description="Was the agent polite?")
    result = asyncio.run(run_tier2(call, rubric=rubric, manual=True, judge=None))
    assert result.execution.state is AnalysisState.COMPLETED
    assert result.payload.get("verdict") == JudgeVerdict.NOT_JUDGED.value
    assert result.payload.get("passed") is not True
    assert result.payload.get("shadow") is True


def test_grounding_corpus_includes_tool_errors() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="vapi",
        source_call_id="s1",
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[Turn(index=0, speaker=Speaker.USER, text="refund please")],
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Be honest",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        tools=[
            ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR, error="not_found")
        ],
    )
    corpus = grounding_corpus(call)
    assert "refund please" in corpus
    assert "Be honest" in corpus
    assert any("not_found" in item and "lookup_order" in item for item in corpus)
    assert extract_candidate_claims(call) == []


def test_usage_cost_uses_reported_dollars_only() -> None:
    from obsalt.analysis.judge import _usage_cost_usd

    assert _usage_cost_usd({"usage": {"total_cost": "0.02"}}, {}) == 0.02
    assert _usage_cost_usd({"usage": {"prompt_tokens": 10}}, {}) is None
