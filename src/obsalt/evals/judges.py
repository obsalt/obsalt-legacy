from __future__ import annotations

from obsalt.domain.models import CanonicalCall, EvalResult, Rubric
from obsalt.util import new_id, utcnow


class Judge:
    def evaluate(self, call: CanonicalCall, rubric: Rubric) -> EvalResult:
        raise NotImplementedError


class HeuristicJudge(Judge):
    """Deterministic stand-in for Claude/GPT judges.

    Interprets the rubric's plain-English description. Production installs swap
    this for LlmJudge; tests stay offline and reproducible.
    """

    def evaluate(self, call: CanonicalCall, rubric: Rubric) -> EvalResult:
        text = rubric.description.lower()
        quotes: list[str] = []
        deductions: list[str] = []
        score = 1.0

        if any(word in text for word in ("hallucin", "invent", "fabricat", "should not know", "ground")):
            if call.hallucinations:
                score -= min(0.85, 0.4 + 0.15 * (len(call.hallucinations) - 1))
                deductions.append(f"{len(call.hallucinations)} hallucination flag(s)")
                quotes.extend(h.span_text for h in call.hallucinations[:3])

        if any(word in text for word in ("latency", "fast", "slow", "responsive", "delay")):
            e2e = [s.duration_ms for s in call.latency_samples if s.component.value in {"e2e", "ttfa"}]
            if e2e:
                worst = max(e2e)
                quotes.append(f"worst time-to-first-audio {worst:.0f}ms")
                if worst > 1500:
                    score -= 0.4
                    deductions.append("time-to-first-audio over 1500ms")
                elif worst > 800:
                    score -= 0.15
                    deductions.append("time-to-first-audio over 800ms")

        if any(word in text for word in ("tool", "function", "successfully")):
            failed = [t for t in call.tools if t.status.value in {"error", "timeout"}]
            if failed:
                score -= 0.4
                deductions.append("tool failure")
                quotes.append(failed[0].name)

        if any(word in text for word in ("hangup", "customer", "frustrat", "polite", "empath")):
            if call.hangup and call.hangup.loss_score >= 0.5:
                score -= 0.35
                deductions.append(f"customer-loss score {call.hangup.loss_score}")
                if call.hangup.last_user_text:
                    quotes.append(call.hangup.last_user_text)

        if "interrupt" in text:
            interrupts = sum(1 for t in call.turns if t.interrupted)
            if interrupts:
                score -= min(0.3, 0.1 * interrupts)
                deductions.append(f"{interrupts} interruption(s)")

        score = max(0.0, min(1.0, round(score, 3)))
        passed = score >= rubric.threshold
        if deductions:
            rationale = "Failed checks: " + "; ".join(deductions) if not passed else "Deductions: " + "; ".join(deductions)
        else:
            rationale = "No rubric violations detected."
        return EvalResult(
            rubric_id=rubric.id,
            rubric_name=rubric.name,
            score=score,
            passed=passed,
            rationale=rationale,
            quotes=quotes,
        )


class LlmJudge(Judge):
    """OpenAI-compatible chat judge. Optional; HeuristicJudge is the default."""

    def __init__(self, complete) -> None:
        self.complete = complete

    def evaluate(self, call: CanonicalCall, rubric: Rubric) -> EvalResult:
        transcript = call.transcript_text or "\n".join(f"{t.speaker.value}: {t.text}" for t in call.turns)
        prompt = (
            "You are evaluating a voice agent call against a quality rubric.\n"
            f"Rubric: {rubric.name}\n{rubric.description}\n"
            f"Pass threshold: {rubric.threshold}\n\n"
            f"Transcript:\n{transcript[:8000]}\n\n"
            "Reply as JSON with keys score (0-1), passed (bool), rationale, quotes (array of strings)."
        )
        raw = self.complete(prompt)
        score = float(raw.get("score", 0))
        return EvalResult(
            rubric_id=rubric.id,
            rubric_name=rubric.name,
            score=score,
            passed=bool(raw.get("passed", score >= rubric.threshold)),
            rationale=str(raw.get("rationale", "")),
            quotes=list(raw.get("quotes") or []),
        )


def default_rubrics(org_id: str) -> list[Rubric]:
    now = utcnow()
    specs = [
        (
            "grounded-claims",
            "Grounded claims",
            "The agent never invents order numbers, prices, or policies. Flag hallucinations.",
        ),
        (
            "latency-budget",
            "Latency budget",
            "The agent should stay responsive. Time-to-first-audio over 800ms is a concern; over 1500ms fails.",
        ),
        (
            "customer-kept",
            "Customer kept",
            "The call should not lose the customer. Frustrated hangups fail this rubric.",
        ),
        (
            "tools-succeed",
            "Tools succeed",
            "Every function/tool invocation should complete successfully.",
        ),
    ]
    return [
        Rubric(
            id=f"{org_id}:{slug}",
            org_id=org_id,
            name=name,
            description=description,
            threshold=0.7,
            created_at=now,
        )
        for slug, name, description in specs
    ]


def new_rubric(org_id: str, name: str, description: str, threshold: float = 0.7) -> Rubric:
    return Rubric(
        id=new_id(),
        org_id=org_id,
        name=name,
        description=description,
        threshold=threshold,
    )
