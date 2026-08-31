"""Small tests: compliance flags and the opening-disclosure predicate."""

from __future__ import annotations

from obsalt.analysis.compliance import compliance_flags, luhn_valid
from obsalt.analysis.predicates import evaluate_spec
from obsalt.domain.enums import Speaker
from obsalt.domain.models import CallRevision, Turn

# Classic Luhn-valid test PAN.
VALID_PAN = "4111111111111111"
LUHN_INVALID = "4111111111111112"


def _turn(index: int, speaker: Speaker, text: str) -> Turn:
    return Turn(index=index, speaker=speaker, text=text)


def _call(turns: list[Turn]) -> CallRevision:
    return CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        turns=turns,
        tools=[],
        grounding=[],
    )


def _kinds(call: CallRevision) -> set[str]:
    return {str(flag["kind"]) for flag in compliance_flags(call)}


def test_luhn_accepts_and_rejects() -> None:
    assert luhn_valid(VALID_PAN)
    assert not luhn_valid(LUHN_INVALID)
    assert not luhn_valid("123")
    assert not luhn_valid("411111111111111abc")


def test_agent_reading_a_card_number_is_flagged_and_masked() -> None:
    call = _call(
        [
            _turn(
                0,
                Speaker.AGENT,
                f"I see your card ends in 1234. Reading it back: {VALID_PAN}. Is that right?",
            )
        ]
    )
    assert "pan_spoken" in _kinds(call)
    flag = next(f for f in compliance_flags(call) if f["kind"] == "pan_spoken")
    assert flag["span_text"] == "**** **** **** 1111"
    assert VALID_PAN not in str(flag)


def test_spaced_card_number_is_caught() -> None:
    spaced = "4111 1111 1111 1111"
    call = _call([_turn(0, Speaker.AGENT, f"Your card is {spaced}, correct?")])
    flag = next(f for f in compliance_flags(call) if f["kind"] == "pan_spoken")
    assert flag["span_text"] == "**** **** **** 1111"


def test_luhn_invalid_numbers_are_not_flagged() -> None:
    call = _call(
        [
            _turn(0, Speaker.AGENT, f"Your reference is {LUHN_INVALID} on file."),
            _turn(1, Speaker.AGENT, "Call me back at 555 201 8890 please."),
        ]
    )
    assert "pan_spoken" not in _kinds(call)


def test_caller_card_number_is_not_an_agent_violation() -> None:
    call = _call([_turn(0, Speaker.USER, f"my card is {VALID_PAN}")])
    assert "pan_spoken" not in _kinds(call)


def test_ssn_spoken_by_agent_is_flagged_and_masked() -> None:
    call = _call([_turn(0, Speaker.AGENT, "I have your SSN as 123-45-6789, right?")])
    flag = next(f for f in compliance_flags(call) if f["kind"] == "ssn_spoken")
    assert flag["span_text"] == "***-**-6789"
    assert "123-45-6789" not in str(flag)


def test_ssn_like_id_without_dashes_is_not_flagged() -> None:
    call = _call([_turn(0, Speaker.AGENT, "Your case id 123456789 is open.")])
    assert "ssn_spoken" not in _kinds(call)


def test_verbal_secret_request_is_flagged() -> None:
    for text in (
        "What is your password?",
        "Can you confirm your PIN for me?",
        "Please share your one-time code.",
        "Could you provide your social security number?",
    ):
        call = _call([_turn(0, Speaker.AGENT, text)])
        assert "verbal_secret_request" in _kinds(call), text


def test_keypad_pin_instruction_is_not_flagged() -> None:
    call = _call([_turn(0, Speaker.AGENT, "Please enter your PIN on the keypad now.")])
    assert "verbal_secret_request" not in _kinds(call)


def test_no_compliance_flags_on_clean_call() -> None:
    call = _call(
        [
            _turn(0, Speaker.USER, "I need help with my order"),
            _turn(1, Speaker.AGENT, "Happy to help. What is your order number?"),
        ]
    )
    assert compliance_flags(call) == []


def test_opening_disclosure_predicate_passes_on_first_agent_turn() -> None:
    call = _call(
        [
            _turn(0, Speaker.AGENT, "Thanks for calling Acme. This call may be recorded."),
            _turn(1, Speaker.USER, "I need help"),
        ]
    )
    judged = evaluate_spec(call, {"all": [{"phrase_in_opening": "call may be recorded"}]})
    assert judged["verdict"] == "pass"


def test_opening_disclosure_late_in_call_fails() -> None:
    call = _call(
        [
            _turn(0, Speaker.AGENT, "Thanks for calling Acme."),
            _turn(1, Speaker.USER, "hello"),
            _turn(2, Speaker.AGENT, "Just so you know, this call may be recorded."),
        ]
    )
    judged = evaluate_spec(call, {"all": [{"phrase_in_opening": "call may be recorded"}]})
    assert judged["verdict"] == "fail"


def test_opening_disclosure_without_agent_speech_is_evidence_missing() -> None:
    call = _call([_turn(0, Speaker.USER, "hello")])
    judged = evaluate_spec(call, {"all": [{"phrase_in_opening": "call may be recorded"}]})
    assert judged["verdict"] == "evidence_missing"
