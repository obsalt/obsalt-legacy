"""Tier 2 — sampled or triggered, expensive analysis.

Default baseline sample rate is 0. Hard budget: spend_usd >= budget_usd =>
budget_blocked. Triggers: manual POST analyze, or hallucination candidates
with needs_llm. Cache by content hash. Without an LLM runner, rows stay
not_judged.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.evals import rubric_to_request
from obsalt.analysis.hallucination import detect_claims, tool_effectively_failed
from obsalt.domain.enums import AnalysisState, HangupReason
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision, Rubric
from obsalt.util import canonical_json, sha256_text

DEFAULT_BASELINE_SAMPLE_RATE = 0.0
DEFAULT_LATENCY_TRIGGER_MS = 2000.0


def is_paid_judge(judge: Any | None) -> bool:
    """An LLM runner can bill. ``None`` (no runner) cannot."""
    if judge is None:
        return False
    name = str(getattr(judge, "name", "") or "")
    return name not in {"", "heuristic"}


def budget_for_judge(budget_usd: float, judge: Any | None) -> float:
    """$0 blocks paid judges. No runner does not invent spend."""
    if is_paid_judge(judge):
        return max(0.0, float(budget_usd))
    return float("inf")


WATCHED_HANGUPS = frozenset(
    {
        HangupReason.USER_HANGUP,
        HangupReason.ERROR_STT,
        HangupReason.ERROR_LLM,
        HangupReason.ERROR_TTS,
        HangupReason.ERROR_TOOL,
        HangupReason.INACTIVITY,
        HangupReason.SILENCE_TIMEOUT,
    }
)
ANALYZER_ID = "tier2"
ANALYZER_VERSION = "1"
JUDGE_VERSION = "none"
PROMPT_VERSION = "none"
HALLUCINATION_ANALYZER_ID = "hallucination"


def call_content_hash(call: CallRevision) -> str:
    payload = {
        "turns": [(turn.index, turn.speaker.value, turn.text) for turn in call.turns],
        "tools": [(tool.name, tool.status.value, tool.argument_hash) for tool in call.tools],
        "grounding": [(item.kind.value, item.content_ref, item.content) for item in call.grounding],
    }
    return sha256_text(canonical_json(payload))


def cache_key(
    call: CallRevision,
    *,
    rubric_version: str | int | None,
    judge_version: str,
    prompt_version: str,
    analyzer_id: str,
) -> str:
    return sha256_text(
        "|".join(
            [
                call.revision,
                call_content_hash(call),
                str(rubric_version or ""),
                judge_version,
                prompt_version,
                analyzer_id,
            ]
        )
    )


def in_baseline_sample(call: CallRevision, rate: float) -> bool:
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = int(sha256_text(f"{call.call_id}:{call.revision}")[:8], 16)
    return (digest / 0xFFFFFFFF) < rate


def decide_tier2(
    call: CallRevision,
    *,
    rubric: Rubric | None = None,
    manual: bool = False,
    baseline_sample_rate: float = DEFAULT_BASELINE_SAMPLE_RATE,
    budget_usd: float = float("inf"),
    spend_usd: float = 0.0,
    cache: Mapping[str, AnalysisResult] | None = None,
    hallucination_candidates: Sequence[Mapping[str, Any]] | None = None,
    judge_version: str = JUDGE_VERSION,
    prompt_version: str = PROMPT_VERSION,
    analyzer_id: str = ANALYZER_ID,
) -> AnalysisExecution:
    """Record the next AnalysisExecution state without running the judge."""
    content = call_content_hash(call)
    key = cache_key(
        call,
        rubric_version=None if rubric is None else rubric.version,
        judge_version=judge_version,
        prompt_version=prompt_version,
        analyzer_id=analyzer_id,
    )
    execution = AnalysisExecution(
        call_id=call.call_id,
        revision=call.revision,
        analyzer_id=analyzer_id,
        analyzer_version=ANALYZER_VERSION,
        rubric_version=None if rubric is None else str(rubric.version),
        prompt_version=prompt_version,
        judge_version=judge_version,
        state=AnalysisState.PENDING,
        content_hash=content,
    )
    if cache is not None and key in cache:
        cached = cache[key].execution.model_copy()
        cached.state = AnalysisState.COMPLETED
        cached.content_hash = content
        return cached

    if spend_usd >= budget_usd:
        execution.state = AnalysisState.BUDGET_BLOCKED
        return execution

    trigger = _trigger(
        call,
        manual=manual,
        hallucination_candidates=hallucination_candidates,
        latency_threshold_ms=DEFAULT_LATENCY_TRIGGER_MS,
    )
    sampled = in_baseline_sample(call, baseline_sample_rate)
    if trigger is None and not sampled:
        execution.state = AnalysisState.SAMPLED_OUT
        return execution
    execution.state = AnalysisState.PENDING
    return execution


async def run_tier2(
    call: CallRevision,
    *,
    rubric: Rubric | None = None,
    manual: bool = False,
    baseline_sample_rate: float = DEFAULT_BASELINE_SAMPLE_RATE,
    budget_usd: float = float("inf"),
    spend_usd: float = 0.0,
    cache: MutableMapping[str, AnalysisResult] | None = None,
    hallucination_candidates: Sequence[Mapping[str, Any]] | None = None,
    judge: Any | None = None,
    judge_version: str = JUDGE_VERSION,
    prompt_version: str = PROMPT_VERSION,
    analyzer_id: str | None = None,
    cost_usd: float = 0.0,
    calibrated: bool = False,
) -> AnalysisResult:
    """Run an LLM judge when eligible. Cache hits do not consume budget."""
    candidates = _candidates(call, hallucination_candidates)
    resolved_analyzer = analyzer_id or (
        HALLUCINATION_ANALYZER_ID if rubric is None and _needs_llm(candidates) else ANALYZER_ID
    )
    store: MutableMapping[str, AnalysisResult] = cache if cache is not None else {}
    key = cache_key(
        call,
        rubric_version=None if rubric is None else rubric.version,
        judge_version=judge_version,
        prompt_version=prompt_version,
        analyzer_id=resolved_analyzer,
    )
    if key in store:
        return store[key]

    execution = decide_tier2(
        call,
        rubric=rubric,
        manual=manual,
        baseline_sample_rate=baseline_sample_rate,
        budget_usd=budget_usd,
        spend_usd=spend_usd,
        cache=store,
        hallucination_candidates=candidates,
        judge_version=judge_version,
        prompt_version=prompt_version,
        analyzer_id=resolved_analyzer,
    )
    if execution.state is not AnalysisState.PENDING:
        return AnalysisResult(execution=execution, payload={"selection": execution.state.value})

    execution.state = AnalysisState.RUNNING
    trigger = _trigger(
        call,
        manual=manual,
        hallucination_candidates=candidates,
        latency_threshold_ms=DEFAULT_LATENCY_TRIGGER_MS,
    )
    selection = trigger or "baseline_sample"
    try:
        payload = await _judge_payload(
            call,
            rubric=rubric,
            candidates=candidates,
            judge=judge,
            selection=selection,
            calibrated=calibrated,
        )
        execution.state = AnalysisState.COMPLETED
        result = AnalysisResult(execution=execution, payload=payload)
        store[key] = result
        if cost_usd:
            # Caller owns spend_usd; returning the incurred cost on the payload.
            result.payload["cost_usd"] = cost_usd
        return result
    except Exception as exc:  # noqa: BLE001 — execution state must record failure
        execution.state = AnalysisState.FAILED
        execution.error = str(exc)
        return AnalysisResult(execution=execution, payload={"selection": selection})


def _trigger(
    call: CallRevision,
    *,
    manual: bool,
    hallucination_candidates: Sequence[Mapping[str, Any]] | None,
    latency_threshold_ms: float = DEFAULT_LATENCY_TRIGGER_MS,
) -> str | None:
    # Precedence from §9.1: manual, then tier-1 signals, then baseline sample.
    if manual:
        return "manual"
    if _needs_llm(_candidates(call, hallucination_candidates)):
        return "hallucination_candidate"
    if any(tool_effectively_failed(tool) for tool in call.tools):
        return "tool_failure"
    if call.hangup is not None and call.hangup.reason in WATCHED_HANGUPS:
        return "watched_hangup"
    values = [item.value_ms for item in call.stage_measurements]
    if values and max(values) > latency_threshold_ms:
        return "latency_threshold"
    return None


def _candidates(
    call: CallRevision,
    hallucination_candidates: Sequence[Mapping[str, Any]] | None,
) -> Sequence[Mapping[str, Any]]:
    if hallucination_candidates is not None:
        return hallucination_candidates
    return detect_claims(call)


def _needs_llm(candidates: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(item.get("needs_llm")) for item in candidates)


async def _judge_payload(
    call: CallRevision,
    *,
    rubric: Rubric | None,
    candidates: Sequence[Mapping[str, Any]],
    judge: Any | None,
    selection: str,
    calibrated: bool = False,
) -> dict[str, Any]:
    from obsalt.analysis.judge import is_heuristic_result
    from obsalt.domain.enums import JudgeVerdict

    if rubric is None:
        entailed = await entail_claims(
            call, judge=judge, candidates=[dict(item) for item in candidates]
        )
        fails = [
            item
            for item in entailed
            if item.get("verdict") in {"contradicted", "unsupported"}
            and not str(item.get("model") or "").startswith("heuristic")
        ]
        missing = [item for item in entailed if item.get("verdict") == "evidence_missing"]
        pending = [item for item in entailed if item.get("needs_llm") and not item.get("verdict")]
        if fails:
            passed: bool | None = False
            rationale = "entailment"
        elif missing and not fails:
            passed = None
            rationale = "evidence_missing"
        elif pending:
            passed = None
            rationale = "not_judged"
        else:
            passed = True
            rationale = "entailment"
        payload = {
            "score": None if passed is None else (0.0 if fails or missing else 1.0),
            "passed": passed,
            "rationale": rationale,
            "quotes": [str(item.get("span_text") or "") for item in fails or missing],
            "prompt_version": "entailment/1",
            "model": getattr(judge, "version", None) or "",
            "selection": selection,
            "trigger": selection,
            "candidates": [dict(item) for item in entailed],
            "claims": [dict(item) for item in entailed],
            "judge_id": "faithfulness",
        }
    elif judge is None:
        payload = {
            "score": None,
            "passed": None,
            "verdict": JudgeVerdict.NOT_JUDGED.value,
            "rationale": "not_judged",
            "quotes": [],
            "prompt_version": PROMPT_VERSION,
            "model": "",
            "selection": selection,
            "trigger": selection,
            "judge_id": rubric.id,
        }
    else:
        judged = await judge.judge(rubric_to_request(call, rubric))
        verdict = str(getattr(judged, "verdict", None) or ("pass" if judged.passed else "fail"))
        payload = {
            "score": judged.score,
            "passed": judged.passed,
            "verdict": verdict,
            "rationale": judged.rationale,
            "quotes": list(judged.quotes),
            "prompt_version": judged.prompt_version,
            "model": judged.model,
            "selection": selection,
            "trigger": selection,
            "judge_id": rubric.id,
        }
        cost_usd = getattr(judged, "cost_usd", None)
        if cost_usd:
            payload["cost_usd"] = cost_usd
        if candidates:
            payload["candidates"] = [dict(item) for item in candidates]
            payload["claims"] = payload["candidates"]
    heuristic = is_heuristic_result(payload)
    payload["shadow"] = heuristic or not calibrated
    if not payload.get("model"):
        payload["shadow"] = True
    payload["calibrated"] = calibrated and not heuristic and not payload["shadow"]
    return payload
