from __future__ import annotations

from obsalt.domain.models import CallRevision, Rubric
from obsalt.plugin.types import JudgeRequest, JudgeResult


class HeuristicJudge:
    """Offline default. Production orgs plug in an LLM judge capability."""

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        text = request.rubric_text.lower()
        score = 1.0
        quotes: list[str] = []
        if "hallucin" in text or "invent" in text:
            if any(token in request.transcript.lower() for token in ("ord-", "$")):
                if not any(token in "\n".join(request.grounding).lower() for token in ("ord-", "$")):
                    score -= 0.5
                    quotes.append("ungrounded identifier or price")
        passed = score >= 0.7
        return JudgeResult(score=score, passed=passed, rationale="heuristic", quotes=quotes, prompt_version="heuristic/1")


def rubric_to_request(call: CallRevision, rubric: Rubric) -> JudgeRequest:
    return JudgeRequest(
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        rubric_text=rubric.description,
        transcript="\n".join(f"{t.speaker.value}: {t.text}" for t in call.turns),
        grounding=[item.content or item.content_ref for item in call.grounding if item.content or item.content_ref],
    )
