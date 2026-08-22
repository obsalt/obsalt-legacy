"""Default judge: deterministic heuristic. Free, no API key.

An org can replace this with any Judge plugin (Claude, OpenAI-compatible,
or a local model). The heuristic exists so evaluate-on-click works without
a bill, and so calibration has a baseline.
"""

from __future__ import annotations

from obsalt.plugin.protocol import JudgeRequest, JudgeResult

JUDGE_VERSION = "heuristic/1"


class HeuristicJudge:
    name = "heuristic"
    version = JUDGE_VERSION

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        corpus = "\n".join(request.grounding).lower()
        transcript = request.transcript.lower()
        rubric = request.rubric.lower()
        contradicted = ("error" in corpus or "not_found" in corpus) and (
            "refund" in transcript or "processed" in transcript
        )
        unsupported = any(token in transcript for token in ("ord-", "invoice", "$")) and not corpus
        passed = not contradicted and not unsupported
        if "fail" in rubric and contradicted:
            passed = False
        score = 0.2 if contradicted else 0.45 if unsupported else 0.85
        rationale = (
            "heuristic: tool/knowledge corpus contradicts the agent claim"
            if contradicted
            else "heuristic: checkable claim with empty grounding"
            if unsupported
            else "heuristic: no cheap contradiction found"
        )
        quotes = [request.transcript[:240]] if request.transcript else []
        return JudgeResult(
            score=score,
            passed=passed,
            rationale=rationale,
            quotes=quotes,
            model=JUDGE_VERSION,
            prompt_version="heuristic/1",
        )
