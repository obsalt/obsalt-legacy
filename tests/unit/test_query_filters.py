"""Small tests: call-list filters. Pending candidates are not fleet failures."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.hangup import ENDING_NOT_REPORTED
from obsalt.api import _ui_flags_and_evals
from obsalt.domain.enums import AnalysisState, HangupParty, HangupReason, Speaker
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision, Hangup, Turn
from obsalt.query import matches_call_filters


def _call() -> CallRevision:
    return CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r-active",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="refund")],
    )


def _row(
    analyzer: str,
    *,
    state: AnalysisState = AnalysisState.COMPLETED,
    payload: dict,
    revision: str = "r-active",
) -> AnalysisResult:
    return AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision=revision,
            analyzer_id=analyzer,
            analyzer_version="1",
            state=state,
        ),
        payload=payload,
    )


def test_not_reported_hangup_filter_matches_missing_outcome() -> None:
    assert matches_call_filters(_call(), outcome=ENDING_NOT_REPORTED) is True
    hung = _call().model_copy(
        update={"hangup": Hangup(reason=HangupReason.USER_HANGUP, party=HangupParty.USER)}
    )
    assert matches_call_filters(hung, outcome=ENDING_NOT_REPORTED) is False
    assert matches_call_filters(hung, outcome="user_hangup") is True


def test_pending_hallucination_candidates_are_a_flag_filter() -> None:
    pending = _row(
        "hallucination",
        state=AnalysisState.PENDING,
        payload={
            "candidates": [{"kind": "price_claim", "needs_llm": True}],
            "selection": "pending",
        },
    )
    assert matches_call_filters(_call(), flag="price_claim", analysis=[pending]) is True


def test_confirmed_hallucination_matches_flag_filter() -> None:
    confirmed = _row(
        "hallucination",
        payload={
            "model": "detector/1",
            "claims": [{"kind": "price_claim", "verdict": "contradicted", "model": "detector/1"}],
        },
    )
    assert matches_call_filters(_call(), flag="price_claim", analysis=[confirmed]) is True
    assert matches_call_filters(_call(), flag="hallucination", analysis=[confirmed]) is True


def test_heuristic_hallucination_is_a_candidate_filter() -> None:
    heuristic = _row(
        "hallucination",
        payload={
            "model": "heuristic/1",
            "claims": [{"kind": "price_claim", "verdict": "contradicted", "model": "heuristic/1"}],
        },
    )
    assert matches_call_filters(_call(), flag="price_claim", analysis=[heuristic]) is True
    assert (
        matches_call_filters(_call(), flag="hallucination_candidate", analysis=[heuristic]) is True
    )
    assert matches_call_filters(_call(), flag="hallucination", analysis=[heuristic]) is False


def test_eval_result_ignores_hallucination_passed() -> None:
    hallo = _row("hallucination", payload={"passed": True, "claims": []})
    assert matches_call_filters(_call(), eval_result="pass", analysis=[hallo]) is False
    eval_row = _row("tier2", payload={"passed": True, "selection": "manual"})
    assert matches_call_filters(_call(), eval_result="pass", analysis=[eval_row]) is True


def test_eval_result_fail_when_passed_is_missing() -> None:
    incomplete = _row("tier2", payload={"selection": "manual"})
    assert matches_call_filters(_call(), eval_result="fail", analysis=[incomplete]) is False
    failed = _row("tier2", payload={"passed": False, "selection": "manual"})
    assert matches_call_filters(_call(), eval_result="fail", analysis=[failed]) is True


def test_grounded_hallucination_is_not_a_pending_flag() -> None:
    flags, _evals, _card = _ui_flags_and_evals(
        [
            _row(
                "hallucination",
                payload={
                    "model": "detector/1",
                    "claims": [
                        {
                            "kind": "price_claim",
                            "verdict": "contradicted",
                            "span_text": "$1",
                            "model": "detector/1",
                        },
                        {
                            "kind": "commitment",
                            "verdict": "grounded",
                            "span_text": "ok",
                            "model": "detector/1",
                        },
                    ],
                },
            )
        ]
    )
    assert [item.get("kind") for item in flags] == ["price_claim"]
    assert flags[0].get("pending") is False


def test_eval_result_open_verdict_is_not_fail() -> None:
    open_row = _row(
        "pack:accuracy",
        payload={"passed": None, "verdict": "not_judged", "selection": "manual"},
    )
    missing = _row(
        "pack:accuracy",
        payload={"passed": None, "verdict": "evidence_missing", "selection": "manual"},
    )
    shadow = _row(
        "pack:accuracy",
        payload={"passed": False, "verdict": "fail", "shadow": True, "selection": "manual"},
    )
    assert matches_call_filters(_call(), eval_result="fail", analysis=[open_row]) is False
    assert matches_call_filters(_call(), eval_result="pass", analysis=[open_row]) is False
    assert matches_call_filters(_call(), eval_result="fail", analysis=[missing]) is False
    assert matches_call_filters(_call(), eval_result="fail", analysis=[shadow]) is False
