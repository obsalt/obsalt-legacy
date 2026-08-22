"""Small tests: deterministic claim pre-filter. Independent of the judge."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.hallucination import extract_candidate_claims, grounding_corpus
from obsalt.domain.enums import GroundingKind, HangupReason, Provenance, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, GroundingRef, Hangup, ToolInvocation, Turn


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


def test_ungrounded_price_and_id_are_candidates() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50 for order ORD-99999.")]
    )
    kinds = {item["kind"] for item in extract_candidate_claims(call)}
    assert "price_claim" in kinds
    assert "fabricated_id" in kinds


def test_grounded_price_and_id_are_not_flagged() -> None:
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
    assert extract_candidate_claims(call) == []


def test_commitment_without_a_successful_tool_is_flagged() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")])
    claims = extract_candidate_claims(call)
    assert any(item["kind"] == "commitment" for item in claims)


def test_failed_tool_plus_success_claim_is_phantom_tool_success() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")],
        tools=[ToolInvocation(id="t1", name="book_appointment", status=ToolStatus.ERROR)],
    )
    kinds = {item["kind"] for item in extract_candidate_claims(call)}
    assert "phantom_tool_success" in kinds
    assert "commitment" not in kinds


def test_successful_related_tool_is_not_a_commitment() -> None:
    call = _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I've booked that for Tuesday.")],
        tools=[ToolInvocation(id="t1", name="book_appointment", status=ToolStatus.SUCCESS)],
    )
    kinds = {item["kind"] for item in extract_candidate_claims(call)}
    assert "commitment" not in kinds
    assert "phantom_tool_success" not in kinds


def test_grounding_corpus_includes_tool_errors() -> None:
    call = _call(
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
