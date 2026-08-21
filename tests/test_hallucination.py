from __future__ import annotations

from obsalt.domain.enums import HallucinationKind, Provider, Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, GroundingContext, ToolInvocation, Turn
from obsalt.hallucination.detector import detect_hallucinations
from obsalt.util import call_id_for


def _call(**kwargs) -> CanonicalCall:
    call = CanonicalCall(
        id=call_id_for("org", "vapi", "c1"),
        org_id="org",
        provider=Provider.VAPI,
        provider_call_id="c1",
        agent_id="a",
        grounding=GroundingContext(system_prompt="You are support. Never invent ids."),
    )
    for key, value in kwargs.items():
        setattr(call, key, value)
    return call


def test_flags_ungrounded_price_and_order_id() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="I need a refund."),
            Turn(index=1, speaker=Speaker.AGENT, text="I've processed a refund for order ORD-99999 totaling $48.50."),
        ]
    )
    flags = detect_hallucinations(call)
    kinds = {f.kind for f in flags}
    assert HallucinationKind.PRICE_CLAIM in kinds
    assert HallucinationKind.FABRICATED_ID in kinds
    assert HallucinationKind.PHANTOM_TOOL_SUCCESS in kinds


def test_grounded_when_tool_returned_the_fact() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="Look up INV-1001"),
            Turn(index=1, speaker=Speaker.AGENT, text="I found invoice INV-1001 for $20.00."),
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_invoice",
                status=ToolStatus.SUCCESS,
                result_preview='{"invoice_id":"INV-1001","amount":"$20.00"}',
            )
        ],
        grounding=GroundingContext(tool_results=['{"invoice_id":"INV-1001","amount":"$20.00"}']),
    )
    flags = detect_hallucinations(call)
    assert flags == []


def test_phantom_tool_success_after_failure() -> None:
    call = _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="Refund me."),
            Turn(index=1, speaker=Speaker.AGENT, text="I've refunded your purchase."),
        ],
        tools=[ToolInvocation(id="t1", name="refund_order", status=ToolStatus.ERROR, error="timeout")],
    )
    flags = detect_hallucinations(call)
    assert any(f.kind == HallucinationKind.PHANTOM_TOOL_SUCCESS for f in flags)
