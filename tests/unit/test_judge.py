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


def test_entailment_and_calibration_disagree_on_ungrounded_price() -> None:
    from obsalt.analysis.calibration import calibrate_rubric
    from obsalt.analysis.entailment import entail_claims
    from obsalt.domain.events import CallObserved, TurnObserved
    from obsalt.domain.models import Rubric
    from obsalt.worker.process import process_normalized_events
    from tests.helpers import example_state, fidelity_declaration

    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    claims = asyncio.run(entail_claims(rev, judge=HeuristicJudge()))
    assert claims
    assert any(item["verdict"] != "grounded" for item in claims)
    rubric = Rubric(id="r1", org_id="acme", name="hallucination", description="Flag invented facts")
    result = asyncio.run(calibrate_rubric(rubric, [(rev, False)], judge=HeuristicJudge()))
    assert result["n"] == 1
    assert "agreement" in result


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
