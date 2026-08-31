"""Small tests: deterministic tool-call integrity flags and the pack precheck."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from obsalt.analysis.pack import run_pack
from obsalt.analysis.tier1 import analyze_tier1
from obsalt.analysis.tool_integrity import (
    broken_transfer_promise,
    double_invocations,
    retry_storms,
    unfulfilled_promises,
)
from obsalt.domain.enums import HangupParty, HangupReason, JudgeVerdict, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, Hangup, ToolInvocation, Turn


def _turn(index: int, speaker: Speaker, text: str) -> Turn:
    return Turn(index=index, speaker=speaker, text=text)


def _call(turns: list[Turn], tools: list[ToolInvocation], **kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        turns=turns,
        tools=tools,
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def _tool(**kwargs) -> ToolInvocation:
    base = dict(id="t1", name="refund_order", status=ToolStatus.SUCCESS)
    base.update(kwargs)
    return ToolInvocation(**base)


def test_double_invocation_same_arguments_is_flagged() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "Refunding now.")],
        tools=[
            _tool(id="a", argument_hash="h1"),
            _tool(id="b", argument_hash="h1"),
        ],
    )
    dups = double_invocations(call)
    assert dups == [{"tool": "refund_order", "invocations": 2}]
    assert "double_invocation" in _flag_kinds(call)


def test_double_invocation_requires_mutation_and_success() -> None:
    different_args = _call(
        [], tools=[_tool(id="a", argument_hash="h1"), _tool(id="b", argument_hash="h2")]
    )
    read_only = _call(
        [],
        tools=[
            _tool(id="a", name="lookup_order", argument_hash="h1"),
            _tool(id="b", name="lookup_order", argument_hash="h1"),
        ],
    )
    one_failed = _call(
        [],
        tools=[
            _tool(id="a", argument_hash="h1"),
            _tool(id="b", argument_hash="h1", status=ToolStatus.ERROR, error="boom"),
        ],
    )
    for call in (different_args, read_only, one_failed):
        assert double_invocations(call) == []


def test_retry_storm_same_arguments() -> None:
    call = _call(
        [],
        tools=[
            _tool(id="a", argument_hash="h1", status=ToolStatus.ERROR, error="timeout"),
            _tool(id="b", argument_hash="h1", status=ToolStatus.TIMEOUT),
            _tool(id="c", argument_hash="h1", status=ToolStatus.ERROR, error="timeout"),
        ],
    )
    assert retry_storms(call) == [("refund_order", 3, 3)]
    assert "retry_storm" in _flag_kinds(call)


def test_two_attempts_are_not_a_storm() -> None:
    call = _call(
        [],
        tools=[
            _tool(id="a", argument_hash="h1", status=ToolStatus.ERROR, error="timeout"),
            _tool(id="b", argument_hash="h1", status=ToolStatus.TIMEOUT),
        ],
    )
    assert retry_storms(call) == []


def test_unfulfilled_promise_without_matching_tool() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "I'll send you a confirmation email right away.")],
        tools=[_tool(id="a", name="lookup_order", turn_index=1)],
    )
    promises = unfulfilled_promises(call)
    assert promises and promises[0]["verb"] == "send"


def test_matching_tool_after_promise_fulfills_it() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "I'll send you a confirmation email right away.")],
        tools=[_tool(id="a", name="send_email", turn_index=1)],
    )
    assert unfulfilled_promises(call) == []


def test_no_tools_means_no_promise_flag() -> None:
    call = _call([_turn(0, Speaker.AGENT, "I'll send you a confirmation email.")], tools=[])
    assert unfulfilled_promises(call) == []


def test_broken_transfer_promise() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "Let me transfer you to our billing team.")],
        tools=[],
        hangup=Hangup(reason=HangupReason.MAX_DURATION, party=HangupParty.SYSTEM),
    )
    assert broken_transfer_promise(call)
    transferred = _call(
        [_turn(0, Speaker.AGENT, "Let me transfer you to our billing team.")],
        tools=[],
        hangup=Hangup(reason=HangupReason.TRANSFER, party=HangupParty.SYSTEM),
    )
    assert not broken_transfer_promise(transferred)


def test_tool_use_fails_on_double_invocation_without_runner() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "Refunding now.")],
        tools=[
            _tool(id="a", argument_hash="h1"),
            _tool(id="b", argument_hash="h1"),
        ],
    )
    results = asyncio.run(run_pack(call, names=["tool_use"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["shadow"] is False
    assert "duplicate mutating tool invocation" in str(results[0].payload["rationale"])


def test_tool_use_fails_on_double_invocation_with_runner() -> None:
    from obsalt.plugin.types import JudgeResult

    class Stub:
        name = "stub-llm"

        async def judge(self, request: object) -> JudgeResult:
            return JudgeResult(
                score=1.0,
                passed=True,
                verdict=JudgeVerdict.PASS.value,
                rationale="stub pass",
                model="stub-llm",
            )

    call = _call(
        [_turn(0, Speaker.AGENT, "Refunding now.")],
        tools=[
            _tool(id="a", argument_hash="h1"),
            _tool(id="b", argument_hash="h1"),
        ],
    )
    results = asyncio.run(run_pack(call, names=["tool_use"], judge=Stub()))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["model"].startswith("detector")


def test_accuracy_is_not_failed_by_double_invocation_alone() -> None:
    call = _call(
        [_turn(0, Speaker.AGENT, "Your refund is on the way.")],
        tools=[
            _tool(id="a", argument_hash="h1"),
            _tool(id="b", argument_hash="h1"),
        ],
    )
    results = asyncio.run(run_pack(call, names=["accuracy"], judge=None))
    assert results[0].payload["verdict"] != JudgeVerdict.FAIL.value


def _flag_kinds(call: CallRevision) -> set[str]:
    results = analyze_tier1(call)
    row = next(item for item in results if item.execution.analyzer_id == "flags")
    return {flag["kind"] for flag in row.payload["flags"]}
