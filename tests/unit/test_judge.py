"""Heuristic entailment must fail closed on ungrounded claims (§9.4)."""

from __future__ import annotations

import asyncio

from obsalt.analysis.hallucination import extract_candidate_claims, grounding_corpus
from obsalt.analysis.judge import HeuristicJudge
from obsalt.domain.enums import GroundingKind, HangupReason, Provenance, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, GroundingRef, Hangup, ToolInvocation, Turn
from obsalt.plugin.types import JudgeRequest


def _request(**kwargs) -> JudgeRequest:
    base = dict(
        rubric_id="hallucination-entailment",
        rubric_version=1,
        rubric_text="Decide if the agent claim is grounded, contradicted, or unsupported.",
        transcript="",
        grounding=[],
    )
    base.update(kwargs)
    return JudgeRequest(**base)


def test_ungrounded_price_is_unsupported() -> None:
    judged = asyncio.run(
        HeuristicJudge().judge(_request(transcript="The ticket is $48.50.", grounding=[]))
    )
    assert judged.passed is False
    assert judged.score < 0.7
    assert judged.rationale == "unsupported"


def test_failed_tool_contradicts_refund_claim() -> None:
    judged = asyncio.run(
        HeuristicJudge().judge(
            _request(
                transcript="I refunded $48.50 for order ORD-99999.",
                grounding=["lookup_order error: not_found"],
            )
        )
    )
    assert judged.passed is False
    assert judged.rationale == "contradicted"
    assert judged.quotes


def test_matching_tool_result_is_grounded() -> None:
    judged = asyncio.run(
        HeuristicJudge().judge(
            _request(
                transcript="Order ORD-100 is $12.00",
                grounding=["ORD-100 $12.00"],
            )
        )
    )
    assert judged.passed is True
    assert judged.rationale == "grounded"


def test_politeness_rubric_does_not_fail_on_price() -> None:
    judged = asyncio.run(
        HeuristicJudge().judge(
            JudgeRequest(
                rubric_id="polite",
                rubric_version=1,
                rubric_text="Was the agent polite?",
                transcript="Thanks, I refunded $12.",
                grounding=[],
            )
        )
    )
    assert judged.passed is True


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
