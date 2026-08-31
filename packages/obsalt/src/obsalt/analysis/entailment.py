"""Hallucination entailment (§9.4). Tier 1 finds candidates; this is the LLM verdict."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from obsalt.analysis.hallucination import claim_is_settled, detect_claims, grounding_corpus
from obsalt.domain.enums import ClaimVerdict
from obsalt.domain.models import CallRevision
from obsalt.plugin.types import JudgeRequest, JudgeResult

_KNOWN_VERDICTS = {item.value for item in ClaimVerdict}


async def entail_claims(
    call: CallRevision,
    *,
    judge: Any | None = None,
    candidates: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Verdict per claim: grounded / contradicted / unsupported, with a quoted span."""

    claims = [
        dict(item) for item in (candidates if candidates is not None else detect_claims(call))
    ]
    grounding = grounding_corpus(call)
    out: list[dict[str, Any]] = []
    for claim in claims:
        if claim_is_settled(claim):
            out.append({**claim, "grounding_considered": grounding[:8]})
            continue
        if judge is None:
            out.append({**claim, "needs_llm": True, "grounding_considered": grounding[:8]})
            continue
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
        judged: JudgeResult = await judge.judge(request)
        rationale = str(judged.rationale or "")
        if rationale in _KNOWN_VERDICTS:
            verdict = rationale
        else:
            verdict = (
                ClaimVerdict.GROUNDED.value
                if judged.passed
                else (
                    ClaimVerdict.CONTRADICTED.value
                    if judged.score < 0.3
                    else ClaimVerdict.UNSUPPORTED.value
                )
            )
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
