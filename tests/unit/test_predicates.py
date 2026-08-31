"""Predicate DSL: closed vocabulary, fail-closed, no eval()."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from obsalt.analysis.predicates import evaluate_spec, parse_spec, run_predicate, spec_from_form
from obsalt.domain.enums import HangupReason, JudgeVerdict, RubricKind, Speaker, ToolStatus
from obsalt.domain.models import CallRevision, Hangup, Rubric, ToolInvocation, Turn


def _call(**kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.COMPLETED),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="This call may be recorded.")],
    )
    base.update(kwargs)
    return CallRevision(**base)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        parse_spec({"all": [{"__import__": "os"}]})


def test_disclosure_phrase_passes() -> None:
    judged = evaluate_spec(_call(), {"all": [{"phrase_in_agent": "call may be recorded"}]})
    assert judged["verdict"] == JudgeVerdict.PASS.value


def test_missing_phrase_is_evidence_missing() -> None:
    call = _call(turns=[Turn(index=0, speaker=Speaker.USER, text="hello")])
    judged = evaluate_spec(call, {"all": [{"phrase_in_agent": "recorded"}]})
    assert judged["verdict"] == JudgeVerdict.EVIDENCE_MISSING.value
    assert judged["passed"] is not True


def test_none_success_claim_after_tool_error() -> None:
    spec = {"none": [{"tool_status": "error", "agent_mentions": ["refunded"]}]}
    clean = _call(tools=[], turns=[Turn(index=0, speaker=Speaker.AGENT, text="Hello")])
    assert evaluate_spec(clean, spec)["verdict"] == JudgeVerdict.PASS.value
    bad = _call(
        tools=[ToolInvocation(id="t1", name="refund", status=ToolStatus.ERROR, error="failed")],
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded your order.")],
    )
    assert evaluate_spec(bad, spec)["verdict"] == JudgeVerdict.FAIL.value


def test_hangup_missing_is_evidence_missing() -> None:
    call = _call(hangup=None)
    judged = evaluate_spec(call, {"all": [{"hangup_in": ["completed"]}]})
    assert judged["verdict"] == JudgeVerdict.EVIDENCE_MISSING.value


def test_form_builds_closed_spec() -> None:
    spec = spec_from_form(
        {
            "combinator": "all",
            "phrase_in_agent": "call may be recorded",
            "tool_status": "any",
            "hangup_in": "any",
        }
    )
    assert spec == {"all": [{"phrase_in_agent": "call may be recorded"}]}


def test_run_predicate_is_not_shadow() -> None:
    rubric = Rubric(
        id="p1",
        org_id="acme",
        name="disclosure",
        description="recording",
        kind=RubricKind.PREDICATE,
        spec={"all": [{"phrase_in_agent": "recorded"}]},
    )
    result = run_predicate(_call(), rubric)
    assert result.payload["shadow"] is False
    assert result.payload["passed"] is True
    assert result.execution.analyzer_id == "rubric:p1"
