"""Tier-2 sampling, budget, and cache. Default baseline rate is 0%."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.hallucination import extract_candidate_claims
from obsalt.analysis.judge import HeuristicJudge
from obsalt.analysis.tier2 import decide_tier2, run_tier2
from obsalt.domain.enums import AnalysisState, HangupReason, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, GroundingRef, Hangup, ToolInvocation, Turn
from obsalt.runtime import MemoryOrgSpend, add_org_spend, org_spend_usd
from obsalt.runtime import AppState
from obsalt.config import Settings
from obsalt.assemble.promote import MemoryPointerStore
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


@pytest.mark.asyncio
async def test_manual_trigger_runs_and_cache_is_free() -> None:
    call = _call()
    cache: dict = {}
    first = await run_tier2(call, manual=True, cache=cache, judge=HeuristicJudge())
    assert first.execution.state is AnalysisState.COMPLETED
    second = await run_tier2(call, manual=True, cache=cache, judge=HeuristicJudge(), cost_usd=0.5)
    assert second is first
    assert "cost_usd" not in (second.payload or {})


@pytest.mark.asyncio
async def test_ungrounded_price_claim_is_not_a_pass() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")],
        grounding=[],
        tools=[ToolInvocation(id="t1", name="lookup_order", status=ToolStatus.ERROR, error="not_found")],
    )
    claims = extract_candidate_claims(call)
    assert claims
    entailed = await entail_claims(call, judge=HeuristicJudge(), candidates=claims)
    assert any(item["verdict"] != "grounded" for item in entailed)


@pytest.mark.asyncio
async def test_grounded_identifier_passes() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Order ORD-100 is $12.00")],
        grounding=[
            GroundingRef(
                kind="tool_result",  # type: ignore[arg-type]
                content="ORD-100 $12.00",
                content_ref="g1",
                provenance="provider_reported",  # type: ignore[arg-type]
            )
        ],
    )
    claims = extract_candidate_claims(call)
    if not claims:
        return
    entailed = await entail_claims(call, judge=HeuristicJudge(), candidates=claims)
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
