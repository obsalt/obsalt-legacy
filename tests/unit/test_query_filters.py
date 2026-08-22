"""Small tests: call-list filters. Pending candidates are not fleet failures."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.domain.enums import AnalysisState, Speaker
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision, Turn
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


def test_pending_hallucination_candidates_are_not_a_flag_filter() -> None:
    pending = _row(
        "hallucination",
        state=AnalysisState.PENDING,
        payload={
            "candidates": [{"kind": "price_claim", "needs_llm": True}],
            "selection": "pending",
        },
    )
    assert matches_call_filters(_call(), flag="price_claim", analysis=[pending]) is False


def test_confirmed_hallucination_matches_flag_filter() -> None:
    confirmed = _row(
        "hallucination",
        payload={"claims": [{"kind": "price_claim", "verdict": "contradicted"}]},
    )
    assert matches_call_filters(_call(), flag="price_claim", analysis=[confirmed]) is True


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
