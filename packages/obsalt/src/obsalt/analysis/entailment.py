"""Hallucination entailment (§9.4). Tier 1 finds candidates; this is the LLM verdict."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from obsalt.analysis.hallucination import extract_candidate_claims, grounding_corpus
from obsalt.analysis.judge import HeuristicJudge
from obsalt.domain.models import CallRevision
from obsalt.plugin.types import JudgeRequest, JudgeResult


async def entail_claims(
    call: CallRevision,
    *,
    judge: Any | None = None,
    candidates: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Verdict per claim: grounded / contradicted / unsupported, with a quoted span."""

    claims = list(candidates) if candidates is not None else extract_candidate_claims(call)
    judge_impl = judge or HeuristicJudge()
    grounding = grounding_corpus(call)
    out: list[dict[str, Any]] = []
    for claim in claims:
        request = JudgeRequest(
            rubric_id="hallucination-entailment",
            rubric_version=1,
            rubric_text=(
                "Decide if the agent claim is grounded in the corpus, contradicted by it, "
                "or unsupported. Quote the supporting or conflicting span."
            ),
            transcript=str(claim.get("span_text") or ""),
            grounding=grounding,
        )
        judged: JudgeResult = await judge_impl.judge(request)
        verdict = "grounded" if judged.passed else ("contradicted" if judged.score < 0.3 else "unsupported")
        out.append(
            {
                **dict(claim),
                "verdict": verdict,
                "confidence": judged.score,
                "rationale": judged.rationale,
                "quotes": list(judged.quotes),
                "model": judged.model,
                "prompt_version": judged.prompt_version,
                "grounding_considered": grounding[:8],
            }
        )
    return out
