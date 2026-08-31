"""LiveKit pack: detector prechecks only; no English default-pass or fail."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from obsalt.analysis.pack import DEFAULT_PACK, analyzer_id, normalize_pack, run_pack
from obsalt.domain.enums import (
    GroundingKind,
    HangupReason,
    JudgeVerdict,
    Provenance,
    Speaker,
    ToolStatus,
)
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
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Hello.")],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_default_pack_names() -> None:
    assert normalize_pack([]) == DEFAULT_PACK
    assert "handoff" not in normalize_pack([])


def test_handoff_without_transfer_is_not_applicable() -> None:
    results = asyncio.run(run_pack(_call(), names=["handoff"], judge=None))
    row = results[0]
    assert row.execution.analyzer_id == analyzer_id("handoff")
    assert row.payload["verdict"] == JudgeVerdict.NOT_APPLICABLE.value
    assert row.payload["passed"] is not True
    assert row.payload["shadow"] is False


def test_task_completion_without_prompt_is_evidence_missing() -> None:
    results = asyncio.run(run_pack(_call(), names=["task_completion"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.EVIDENCE_MISSING.value


def test_task_completion_with_prompt_is_not_judged_without_runner() -> None:
    call = _call(
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Book appointments honestly.",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ]
    )
    results = asyncio.run(run_pack(call, names=["task_completion"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value
    assert results[0].payload["passed"] is not False
    assert results[0].payload["shadow"] is True


def test_accuracy_without_grounding_is_evidence_missing() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12.")])
    results = asyncio.run(run_pack(call, names=["accuracy"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.EVIDENCE_MISSING.value


def test_accuracy_without_runner_is_not_judged() -> None:
    call = _call(
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Be accurate.",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I can help with that.")],
    )
    results = asyncio.run(run_pack(call, names=["accuracy"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value
    assert results[0].payload["passed"] is not False


def test_tool_use_without_tools_is_not_judged() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.USER, text="Hello")])
    results = asyncio.run(run_pack(call, names=["tool_use"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value


def test_tool_use_does_not_fail_on_effective_tool_failure_alone() -> None:
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
    results = asyncio.run(run_pack(call, names=["tool_use"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value
    assert results[0].payload["passed"] is not False


def test_tool_use_fails_on_bound_phantom() -> None:
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
    results = asyncio.run(run_pack(call, names=["tool_use"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["shadow"] is False
    quotes = results[0].payload.get("quotes") or []
    assert quotes


def test_conciseness_does_not_fail_on_long_turn() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.AGENT, text="x" * 900)])
    results = asyncio.run(run_pack(call, names=["conciseness"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value
    assert results[0].payload["passed"] is not False


def test_maybe_cascades_to_expensive_judge() -> None:
    from obsalt.plugin.types import JudgeResult

    class Once:
        def __init__(self, verdict: JudgeVerdict, name: str) -> None:
            self.name = name
            self.version = name
            self._verdict = verdict

        async def judge(self, request: object) -> JudgeResult:
            passed = True if self._verdict is JudgeVerdict.PASS else False
            if self._verdict not in {JudgeVerdict.PASS, JudgeVerdict.FAIL}:
                passed = None
            return JudgeResult(
                score=0.4,
                passed=passed,
                verdict=self._verdict,
                rationale=self.name,
                model=self.name,
            )

    call = _call(
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Stay accurate.",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="Your appointment is Tuesday.")],
    )
    cheap = Once(JudgeVerdict.MAYBE, "cheap/1")
    expensive = Once(JudgeVerdict.FAIL, "expensive/1")
    results = asyncio.run(
        run_pack(call, names=["accuracy"], judge=cheap, expensive_judge=expensive)
    )
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["cascade"] == "expensive"
    assert results[0].payload["cheap_verdict"] == JudgeVerdict.MAYBE.value
    assert results[0].payload["model"] == "expensive/1"


def _phantom_failure_call() -> Any:
    return _call(
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I can't find your order.")],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                turn_index=0,
                status=ToolStatus.SUCCESS,
                result='{"order_id": "ORD-1234", "status": "shipped"}',
            )
        ],
    )


def _args_mismatch_call() -> Any:
    return _call(
        turns=[
            Turn(index=0, speaker=Speaker.USER, text="my order is ORD-1234"),
            Turn(index=1, speaker=Speaker.AGENT, text="Let me look that up."),
        ],
        tools=[
            ToolInvocation(
                id="t1",
                name="lookup_order",
                turn_index=1,
                status=ToolStatus.SUCCESS,
                args={"order_id": "unknown"},
                result="{}",
            )
        ],
    )


def test_tool_use_fails_on_phantom_failure_without_runner() -> None:
    results = asyncio.run(run_pack(_phantom_failure_call(), names=["tool_use"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["shadow"] is False
    assert results[0].payload["model"].startswith("detector")


def test_tool_use_fails_on_phantom_failure_with_runner() -> None:
    results = asyncio.run(run_pack(_phantom_failure_call(), names=["tool_use"], judge=_PassJudge()))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value
    assert results[0].payload["shadow"] is False
    assert results[0].payload["model"].startswith("detector")


def test_tool_use_fails_on_args_mismatch_in_both_scenarios() -> None:
    for judge in (None, _PassJudge()):
        results = asyncio.run(run_pack(_args_mismatch_call(), names=["tool_use"], judge=judge))
        assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value, judge
        assert results[0].payload["passed"] is False


def test_accuracy_fails_on_detector_contradictions_with_runner() -> None:
    results = asyncio.run(run_pack(_phantom_failure_call(), names=["accuracy"], judge=_PassJudge()))
    assert results[0].payload["verdict"] == JudgeVerdict.FAIL.value


def test_unsettled_candidates_do_not_fail_accuracy_without_runner() -> None:
    call = _call(
        grounding=[
            GroundingRef(
                kind=GroundingKind.SYSTEM_PROMPT,
                content="Be accurate.",
                content_ref="p1",
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I see jane.smith@other.org on file.")],
    )
    results = asyncio.run(run_pack(call, names=["accuracy"], judge=None))
    assert results[0].payload["verdict"] == JudgeVerdict.NOT_JUDGED.value
    assert results[0].payload["passed"] is not False


class _PassJudge:
    name = "stub-llm"

    def __init__(self) -> None:
        from obsalt.plugin.types import JudgeResult

        self._result = JudgeResult(
            score=1.0,
            passed=True,
            verdict=JudgeVerdict.PASS.value,
            rationale="stub pass",
            model="stub-llm",
        )

    async def judge(self, request: object) -> Any:
        return self._result
