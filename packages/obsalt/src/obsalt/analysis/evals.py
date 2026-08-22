from __future__ import annotations

from obsalt.analysis.hallucination import grounding_corpus
from obsalt.analysis.judge import HeuristicJudge, OpenAICompatibleJudge, judge_from_settings
from obsalt.domain.models import CallRevision, Rubric
from obsalt.plugin.types import JudgeRequest

__all__ = ["HeuristicJudge", "OpenAICompatibleJudge", "judge_from_settings", "rubric_to_request"]


def rubric_to_request(call: CallRevision, rubric: Rubric) -> JudgeRequest:
    return JudgeRequest(
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        rubric_text=rubric.description,
        transcript="\n".join(f"{t.speaker.value}: {t.text}" for t in call.turns),
        grounding=grounding_corpus(call),
    )
