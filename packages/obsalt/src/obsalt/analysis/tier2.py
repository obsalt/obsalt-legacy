"""Tier 2 — sampled or triggered, expensive analysis.

Default baseline sample rate is 0. Hard budget: spend_usd >= budget_usd =>
budget_blocked. Triggers: manual POST analyze, or hallucination candidates
with needs_llm. Cache by content hash. HeuristicJudge is the default.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.evals import HeuristicJudge, rubric_to_request
from obsalt.analysis.hallucination import extract_candidate_claims
from obsalt.domain.enums import AnalysisState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision, Rubric
from obsalt.util import canonical_json, sha256_text

DEFAULT_BASELINE_SAMPLE_RATE = 0.0
ANALYZER_ID = "tier2"
ANALYZER_VERSION = "1"
JUDGE_VERSION = "heuristic/1"
PROMPT_VERSION = "heuristic/1"
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

    trigger = _trigger(call, manual=manual, hallucination_candidates=hallucination_candidates)
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
) -> AnalysisResult:
    """Run HeuristicJudge when eligible. Cache hits do not consume budget."""
    candidates = _candidates(call, hallucination_candidates)
    resolved_analyzer = analyzer_id or (HALLUCINATION_ANALYZER_ID if rubric is None and _needs_llm(candidates) else ANALYZER_ID)
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
    trigger = _trigger(call, manual=manual, hallucination_candidates=candidates)
    selection = trigger or "baseline_sample"
    judge_impl = judge or HeuristicJudge()
    try:
        payload = await _judge_payload(
            call,
            rubric=rubric,
            candidates=candidates,
            judge=judge_impl,
            selection=selection,
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
) -> str | None:
    if manual:
        return "manual"
    if _needs_llm(_candidates(call, hallucination_candidates)):
        return "hallucination_candidate"
    return None


def _candidates(
    call: CallRevision,
    hallucination_candidates: Sequence[Mapping[str, Any]] | None,
) -> Sequence[Mapping[str, Any]]:
    if hallucination_candidates is not None:
        return hallucination_candidates
    return extract_candidate_claims(call)


def _needs_llm(candidates: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(item.get("needs_llm")) for item in candidates)


async def _judge_payload(
    call: CallRevision,
    *,
    rubric: Rubric | None,
    candidates: Sequence[Mapping[str, Any]],
    judge: Any,
    selection: str,
) -> dict[str, Any]:
    if rubric is None:
        entailed = await entail_claims(call, judge=judge, candidates=list(candidates))
        unsupported = [item for item in entailed if item.get("verdict") != "grounded"]
        return {
            "score": 0.0 if unsupported else 1.0,
            "passed": not unsupported,
            "rationale": "entailment",
            "quotes": [str(item.get("span_text") or "") for item in unsupported],
            "prompt_version": "entailment/1",
            "model": getattr(judge, "version", "heuristic/1"),
            "selection": selection,
            "trigger": selection,
            "candidates": [dict(item) for item in entailed],
            "claims": [dict(item) for item in entailed],
        }
    judged = await judge.judge(rubric_to_request(call, rubric))
    payload: dict[str, Any] = {
        "score": judged.score,
        "passed": judged.passed,
        "rationale": judged.rationale,
        "quotes": list(judged.quotes),
        "prompt_version": judged.prompt_version,
        "model": judged.model,
        "selection": selection,
        "trigger": selection,
    }
    if candidates:
        payload["candidates"] = [dict(item) for item in candidates]
        payload["claims"] = payload["candidates"]
    return payload
