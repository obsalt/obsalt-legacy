"""Small tests: deterministic behavioral flags from clocks and lexicons."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obsalt.analysis.behavior import behavior_flags
from obsalt.analysis.tier1 import analyze_tier1
from obsalt.domain.enums import HangupParty, HangupReason, Speaker
from obsalt.domain.models import CallRevision, Hangup, Turn


def _turn(index: int, speaker: Speaker, text: str, **kwargs) -> Turn:
    base = dict(index=index, speaker=speaker, text=text)
    base.update(kwargs)
    return Turn(**base)


def _call(turns: list[Turn], **kwargs) -> CallRevision:
    base = dict(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        turns=turns,
        tools=[],
        grounding=[],
    )
    base.update(kwargs)
    return CallRevision(**base)


def _kinds(call: CallRevision) -> set[str]:
    return {str(flag["kind"]) for flag in behavior_flags(call)}


def test_same_agent_utterance_three_times_is_a_loop() -> None:
    text = "I'm sorry, I didn't catch that."
    call = _call(
        [
            _turn(0, Speaker.USER, "what are your hours"),
            _turn(1, Speaker.AGENT, text),
            _turn(2, Speaker.USER, "what are your hours"),
            _turn(3, Speaker.AGENT, text),
            _turn(4, Speaker.USER, "what are your hours"),
            _turn(5, Speaker.AGENT, text),
        ]
    )
    assert "loop_detected" in _kinds(call)


def test_two_repeats_is_not_a_loop() -> None:
    text = "I'm sorry, I didn't catch that."
    call = _call(
        [
            _turn(0, Speaker.USER, "what are your hours"),
            _turn(1, Speaker.AGENT, text),
            _turn(2, Speaker.USER, "what are your hours"),
            _turn(3, Speaker.AGENT, text),
        ]
    )
    assert "loop_detected" not in _kinds(call)


def test_user_repeat_after_agent_reply_is_flagged() -> None:
    text = "I want a refund for my order please"
    call = _call(
        [
            _turn(0, Speaker.USER, text),
            _turn(1, Speaker.AGENT, "Of course, which order?"),
            _turn(2, Speaker.USER, text),
        ]
    )
    assert "user_repeated" in _kinds(call)


def test_back_to_back_user_repeat_is_not_flagged() -> None:
    text = "I want a refund for my order please"
    call = _call(
        [
            _turn(0, Speaker.USER, text),
            _turn(1, Speaker.USER, text),
        ]
    )
    assert "user_repeated" not in _kinds(call)


def test_farewell_missed_when_agent_keeps_talking() -> None:
    call = _call(
        [
            _turn(0, Speaker.USER, "thanks, that's all goodbye"),
            _turn(1, Speaker.AGENT, "Before you go, our spring sale is on."),
            _turn(2, Speaker.AGENT, "You can save 20 percent with code SPRING."),
        ]
    )
    assert "farewell_missed" in _kinds(call)


def test_single_agent_turn_after_farewell_is_not_flagged() -> None:
    call = _call(
        [
            _turn(0, Speaker.USER, "thanks, that's all goodbye"),
            _turn(1, Speaker.AGENT, "Have a great day!"),
        ]
    )
    assert "farewell_missed" not in _kinds(call)


def test_escalation_unmet_without_transfer() -> None:
    call = _call(
        [
            _turn(0, Speaker.USER, "let me speak to a human"),
            _turn(1, Speaker.AGENT, "I can help with that."),
        ],
        hangup=Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER),
    )
    assert "escalation_unmet" in _kinds(call)


def test_transfer_hangup_satisfies_escalation() -> None:
    call = _call(
        [_turn(0, Speaker.USER, "let me speak to a human")],
        hangup=Hangup(reason=HangupReason.TRANSFER, party=HangupParty.SYSTEM),
    )
    assert "escalation_unmet" not in _kinds(call)


def test_monologue_flags_single_long_agent_turn() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    call = _call(
        [
            _turn(0, Speaker.USER, "hello"),
            _turn(
                1,
                Speaker.AGENT,
                "Let me tell you about our plans.",
                started_at=start,
                ended_at=start + timedelta(seconds=75),
            ),
        ]
    )
    assert "monologue" in _kinds(call)


def test_balanced_talk_time_is_not_a_monologue() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    call = _call(
        [
            _turn(
                0,
                Speaker.AGENT,
                "Hi, how can I help?",
                started_at=start,
                ended_at=start + timedelta(seconds=10),
            ),
            _turn(
                1,
                Speaker.USER,
                "I need help with my order",
                started_at=start + timedelta(seconds=12),
                ended_at=start + timedelta(seconds=20),
            ),
        ]
    )
    assert "monologue" not in _kinds(call)


def test_barge_in_not_recovered() -> None:
    call = _call(
        [
            _turn(0, Speaker.AGENT, "Your order total is", interrupted=True),
            _turn(1, Speaker.AGENT, "Your order total is forty dollars."),
        ]
    )
    assert "barge_in_no_recovery" in _kinds(call)


def test_dead_air_before_user_hangup() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    call = _call(
        [
            _turn(
                0,
                Speaker.AGENT,
                "One moment please.",
                started_at=start,
                ended_at=start + timedelta(seconds=2),
            )
        ],
        ended_at=start + timedelta(seconds=15),
        hangup=Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER),
    )
    assert "dead_air_hangup" in _kinds(call)


def test_agent_side_dead_air_is_not_flagged() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    call = _call(
        [
            _turn(
                0,
                Speaker.AGENT,
                "One moment please.",
                started_at=start,
                ended_at=start + timedelta(seconds=2),
            )
        ],
        ended_at=start + timedelta(seconds=15),
        hangup=Hangup(reason=HangupReason.AGENT_HANGUP, party=HangupParty.AGENT),
    )
    assert "dead_air_hangup" not in _kinds(call)


def test_tier1_flags_payload_includes_behavior_flags() -> None:
    text = "I'm sorry, I didn't catch that."
    call = _call(
        [
            _turn(0, Speaker.USER, "what are your hours"),
            _turn(1, Speaker.AGENT, text),
            _turn(2, Speaker.USER, "what are your hours"),
            _turn(3, Speaker.AGENT, text),
            _turn(4, Speaker.USER, "what are your hours"),
            _turn(5, Speaker.AGENT, text),
        ]
    )
    results = analyze_tier1(call)
    flags_row = next(row for row in results if row.execution.analyzer_id == "flags")
    kinds = {flag["kind"] for flag in flags_row.payload["flags"]}
    assert "loop_detected" in kinds


def test_tier1_flags_do_not_include_tool_integrity_without_tools() -> None:
    call = _call([_turn(0, Speaker.USER, "hello")])
    results = analyze_tier1(call)
    flags_row = next(row for row in results if row.execution.analyzer_id == "flags")
    kinds = {flag["kind"] for flag in flags_row.payload["flags"]}
    assert "double_invocation" not in kinds
    assert "retry_storm" not in kinds
