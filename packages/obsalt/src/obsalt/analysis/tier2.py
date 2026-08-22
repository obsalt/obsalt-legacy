"""Sampled LLM analysis. Default baseline sample rate is 0% (open question 6)."""

from __future__ import annotations

from collections.abc import Sequence

from obsalt.domain.enums import AnalysisExecutionState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.plugin.protocol import Judge, JudgeRequest

DEFAULT_BASELINE_RATE = 0.0


def should_run_tier2(
    *,
    manual: bool = False,
    triggered: bool = False,
    user_filter: bool = False,
    baseline_rate: float = DEFAULT_BASELINE_RATE,
    sampled: bool = False,
    budget_remaining: bool = True,
) -> AnalysisExecutionState:
    if not budget_remaining:
        return AnalysisExecutionState.BUDGET_BLOCKED
    if manual or triggered or user_filter:
        return AnalysisExecutionState.PENDING
    if sampled and baseline_rate > 0:
        return AnalysisExecutionState.PENDING
    return AnalysisExecutionState.SAMPLED_OUT


async def judge_rubric(
    revision: CallRevision,
    *,
    rubric_id: str,
    rubric_version: str,
    rubric_body: str,
    judge: Judge,
    judge_version: str,
    prompt_version: str,
    transcript: str,
    grounding: Sequence[str],
) -> tuple[AnalysisExecution, AnalysisResult]:
    result = await judge.judge(
        JudgeRequest(rubric=rubric_body, transcript=transcript, grounding=list(grounding))
    )
    execution = AnalysisExecution(
        org_id=revision.org_id,
        call_id=revision.call_id,
        revision=revision.revision,
        analyzer_id=f"eval.{rubric_id}",
        analyzer_version=rubric_version,
        rubric_version=rubric_version,
        prompt_version=prompt_version,
        judge_version=judge_version,
        content_hash=revision.processing_run_id,
        state=AnalysisExecutionState.COMPLETED,
    )
    analysis = AnalysisResult(
        org_id=revision.org_id,
        call_id=revision.call_id,
        revision=revision.revision,
        analyzer_id=f"eval.{rubric_id}",
        analyzer_version=rubric_version,
        kind="eval",
        passed=result.passed,
        score=result.score,
        rationale=result.rationale,
        evidence_quotes=list(result.quotes),
        payload={"model": result.model, "prompt_version": prompt_version},
    )
    return execution, analysis
