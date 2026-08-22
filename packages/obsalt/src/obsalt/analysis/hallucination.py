"""Hallucination detection: claim extraction + grounding + entailment.

Tier 1 finds candidate claims cheaply. Only candidates reach a judge.
Grounding must be populated by the decoder — empty grounding is a coverage fact,
not a silent pass.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from obsalt.domain.enums import AnalysisExecutionState, Signal, SignalCoverageStatus
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.plugin.protocol import Judge, JudgeRequest

ANALYZER_VERSION = "hallucination/1"

_CANDIDATE = re.compile(
    r"(\$[0-9]+(?:\.[0-9]+)?|\b[A-Z]{2,}-?\d{3,}\b|\border\b|\binvoice\b|\brefund\b|\bguarantee\b)",
    re.I,
)


def candidate_claims(turns: Sequence[object]) -> list[str]:
    claims: list[str] = []
    for turn in turns:
        if getattr(turn, "speaker", None) is None:
            continue
        if getattr(turn.speaker, "value", "") != "agent":
            continue
        text = getattr(turn, "text", "") or ""
        if _CANDIDATE.search(text):
            claims.append(text)
    return claims


def grounding_ready(revision: CallRevision) -> bool:
    needed = {
        Signal.GROUNDING_SYSTEM_PROMPT,
        Signal.GROUNDING_TOOL_RESULTS,
        Signal.GROUNDING_USER_TEXT,
    }
    present = {
        row.signal
        for row in revision.coverage
        if row.status is SignalCoverageStatus.PRESENT and row.signal in needed
    }
    return bool(present)


async def detect(
    revision: CallRevision,
    *,
    judge: Judge | None,
    transcript: str,
    grounding: Sequence[str],
) -> tuple[AnalysisExecution, list[AnalysisResult]]:
    claims = candidate_claims(revision.turns)
    if not claims:
        execution = AnalysisExecution(
            org_id=revision.org_id,
            call_id=revision.call_id,
            revision=revision.revision,
            analyzer_id="hallucination",
            analyzer_version=ANALYZER_VERSION,
            content_hash=revision.processing_run_id,
            state=AnalysisExecutionState.COMPLETED,
        )
        return execution, []
    if not grounding_ready(revision) or judge is None:
        execution = AnalysisExecution(
            org_id=revision.org_id,
            call_id=revision.call_id,
            revision=revision.revision,
            analyzer_id="hallucination",
            analyzer_version=ANALYZER_VERSION,
            content_hash=revision.processing_run_id,
            state=AnalysisExecutionState.FAILED,
            error="grounding missing or no judge configured; refusing to flag without evidence",
        )
        return execution, []
    request = JudgeRequest(
        rubric=(
            "For each agent claim, verdict grounded / contradicted / unsupported. "
            "Quote the supporting span. Claims:\n" + "\n".join(claims)
        ),
        transcript=transcript,
        grounding=list(grounding),
    )
    judged = await judge.judge(request)
    execution = AnalysisExecution(
        org_id=revision.org_id,
        call_id=revision.call_id,
        revision=revision.revision,
        analyzer_id="hallucination",
        analyzer_version=ANALYZER_VERSION,
        judge_version=judged.model,
        prompt_version=judged.prompt_version,
        content_hash=revision.processing_run_id,
        state=AnalysisExecutionState.COMPLETED,
    )
    result = AnalysisResult(
        org_id=revision.org_id,
        call_id=revision.call_id,
        revision=revision.revision,
        analyzer_id="hallucination",
        analyzer_version=ANALYZER_VERSION,
        kind="hallucination",
        passed=judged.passed,
        score=judged.score,
        rationale=judged.rationale,
        evidence_quotes=list(judged.quotes),
        payload={"claims": claims, "model": judged.model},
    )
    return execution, [result]
