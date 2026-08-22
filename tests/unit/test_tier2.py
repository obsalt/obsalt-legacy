"""Tier-2 sampling, budget, and cache. Default baseline rate is 0%."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.hallucination import extract_candidate_claims
from obsalt.analysis.judge import HeuristicJudge
from obsalt.analysis.tier2 import decide_tier2, run_tier2
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.domain.enums import (
    AnalysisState,
    GroundingKind,
    HangupReason,
    Provenance,
    Speaker,
    ToolStatus,
)
from obsalt.domain.models import CallRevision, GroundingRef, Hangup, ToolInvocation, Turn
from obsalt.runtime import AppState, MemoryOrgSpend, add_org_spend, org_spend_usd
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="vapi",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Hello")],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_zero_baseline_samples_out_completed_calls() -> None:
    execution = decide_tier2(_call(), baseline_sample_rate=0.0)
    assert execution.state is AnalysisState.SAMPLED_OUT


def test_hard_budget_blocks_before_judge() -> None:
    execution = decide_tier2(_call(), manual=True, budget_usd=1.0, spend_usd=1.0)
    assert execution.state is AnalysisState.BUDGET_BLOCKED


def test_manual_trigger_runs_and_cache_is_free() -> None:
    call = _call()
    cache: dict = {}
    first = asyncio.run(run_tier2(call, manual=True, cache=cache, judge=HeuristicJudge()))
    assert first.execution.state is AnalysisState.COMPLETED
    second = asyncio.run(
        run_tier2(call, manual=True, cache=cache, judge=HeuristicJudge(), cost_usd=0.5)
    )
    assert second is first
    assert "cost_usd" not in (second.payload or {})


def test_ungrounded_price_claim_is_not_a_pass() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")],
        grounding=[],
        tools=[
            ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR, error="not_found")
        ],
    )
    claims = extract_candidate_claims(call)
    assert claims
    assert {item["kind"] for item in claims} >= {"price_claim", "fabricated_id"}
    entailed = asyncio.run(entail_claims(call, judge=HeuristicJudge(), candidates=claims))
    assert entailed
    assert all(item["verdict"] != "grounded" for item in entailed)
    assert any(item["verdict"] == "contradicted" for item in entailed)
    assert any(item.get("quotes") for item in entailed)
    assert any(
        "lookup_order" in " ".join(item.get("grounding_considered") or []) for item in entailed
    )


def test_grounded_identifier_passes() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Order ORD-100 is $12.00")],
        grounding=[
            GroundingRef(
                kind=GroundingKind.TOOL_RESULT,
                content="ORD-100 $12.00",
                content_ref="g1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    claims = extract_candidate_claims(call)
    assert claims == [], "grounded price and order id must not be flagged by the tier-1 pre-filter"
    entailed = asyncio.run(
        entail_claims(
            call,
            judge=HeuristicJudge(),
            candidates=[
                {
                    "kind": "price_claim",
                    "span_text": "Order ORD-100 is $12.00",
                    "turn_index": 0,
                    "evidence": ["$12.00"],
                    "needs_llm": True,
                }
            ],
        )
    )
    assert entailed
    assert all(item["verdict"] == "grounded" for item in entailed)


def test_tier2_triggers_tool_failure_and_watched_hangup() -> None:
    failed = _call(tools=[ToolInvocation(id="t1", name="lookup", status=ToolStatus.ERROR)])
    assert decide_tier2(failed).state is AnalysisState.PENDING
    hung = _call(hangup=Hangup(reason=HangupReason.USER_HANGUP))
    assert decide_tier2(hung).state is AnalysisState.PENDING


def test_org_spend_is_isolated() -> None:
    state = AppState(
        settings=Settings(environment="test"),
        plugins=[],
        resolver=MemoryResolver(),
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        spend_store=MemoryOrgSpend(),
        org_spend={},
    )
    add_org_spend(state, "acme", 1.25)
    add_org_spend(state, "other", 9.0)
    assert org_spend_usd(state, "acme") == 1.25
    assert org_spend_usd(state, "other") == 9.0
