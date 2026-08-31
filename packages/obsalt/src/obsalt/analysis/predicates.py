"""Closed-vocabulary predicates. No eval(), no tenant Python."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from obsalt.domain.enums import AnalysisState, JudgeVerdict, RubricKind, ToolStatus
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision, Rubric
from obsalt.util import sha256_text

ANALYZER_VERSION = "1"
PROMPT_VERSION = "predicate/1"
COMBINATORS = ("all", "any", "none")
CLAUSE_KEYS = frozenset(
    {
        "phrase_in_agent",
        "phrase_in_opening",
        "phrase_in_user",
        "agent_mentions",
        "tool_called",
        "tool_status",
        "hangup_in",
    }
)
_SUCCESS_STATUS = frozenset({ToolStatus.SUCCESS})
_ERROR_STATUS = frozenset({ToolStatus.ERROR, ToolStatus.TIMEOUT})


def parse_spec(raw: Mapping[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    spec = dict(raw or {})
    found = [key for key in COMBINATORS if key in spec]
    if len(found) != 1:
        raise ValueError("predicate spec needs exactly one of all, any, none")
    combinator = found[0]
    clauses_raw = spec[combinator]
    if not isinstance(clauses_raw, list) or not clauses_raw:
        raise ValueError("predicate clauses must be a non-empty list")
    clauses: list[dict[str, Any]] = []
    for item in clauses_raw:
        if not isinstance(item, dict) or not item:
            raise ValueError("each clause must be an object")
        unknown = set(item) - CLAUSE_KEYS
        if unknown:
            raise ValueError(f"unknown predicate keys: {', '.join(sorted(unknown))}")
        clauses.append(dict(item))
    return combinator, clauses


def spec_from_form(form: Mapping[str, Any]) -> dict[str, Any]:
    combinator = str(form.get("combinator") or "all")
    if combinator not in COMBINATORS:
        raise ValueError("combinator must be all, any, or none")
    clause: dict[str, Any] = {}
    phrase = str(form.get("phrase_in_agent") or "").strip()
    if phrase:
        clause["phrase_in_agent"] = phrase
    opening = str(form.get("phrase_in_opening") or "").strip()
    if opening:
        clause["phrase_in_opening"] = opening
    mentions = str(form.get("agent_mentions") or "").strip()
    if mentions:
        clause["agent_mentions"] = [part.strip() for part in mentions.split(",") if part.strip()]
    tool = str(form.get("tool_called") or "").strip()
    if tool:
        clause["tool_called"] = tool
    status = str(form.get("tool_status") or "").strip().lower()
    if status and status != "any":
        clause["tool_status"] = status
    hangup = str(form.get("hangup_in") or "").strip()
    if hangup and hangup != "any":
        clause["hangup_in"] = [part.strip() for part in hangup.split(",") if part.strip()]
    if not clause:
        raise ValueError("fill at least one predicate field")
    return {combinator: [clause]}


def evaluate_spec(call: CallRevision, spec: Mapping[str, Any]) -> dict[str, Any]:
    combinator, clauses = parse_spec(spec)
    results = [_clause(call, clause) for clause in clauses]
    quotes = [item["quote"] for item in results if item.get("quote")]
    truths = [item["value"] for item in results]
    if any(value is None for value in truths):
        return _out(JudgeVerdict.EVIDENCE_MISSING, "predicate evidence missing", quotes)
    if combinator == "all":
        ok = all(truths)
    elif combinator == "any":
        ok = any(truths)
    else:
        ok = not any(truths)
    verdict = JudgeVerdict.PASS if ok else JudgeVerdict.FAIL
    return _out(verdict, f"predicate {combinator} {'passed' if ok else 'failed'}", quotes)


def run_predicate(
    call: CallRevision, rubric: Rubric, *, selection: str = "always"
) -> AnalysisResult:
    judged = evaluate_spec(call, rubric.spec)
    payload = {
        "judge_id": rubric.id,
        "verdict": judged["verdict"],
        "passed": judged["passed"],
        "score": judged["score"],
        "rationale": judged["rationale"],
        "quotes": judged["quotes"],
        "model": "predicate/1",
        "prompt_version": PROMPT_VERSION,
        "selection": selection,
        "trigger": selection,
        "shadow": False,
        "calibrated": True,
        "kind": RubricKind.PREDICATE.value,
    }
    return AnalysisResult(
        execution=AnalysisExecution(
            call_id=call.call_id,
            revision=call.revision,
            analyzer_id=f"rubric:{rubric.id}",
            analyzer_version=ANALYZER_VERSION,
            rubric_version=str(rubric.version),
            prompt_version=PROMPT_VERSION,
            judge_version="predicate/1",
            state=AnalysisState.COMPLETED,
            content_hash=sha256_text(call.revision + rubric.id + str(rubric.version)),
        ),
        payload=payload,
    )


def _clause(call: CallRevision, clause: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[bool | None] = []
    quote = ""
    if "phrase_in_agent" in clause:
        text = _agent_text(call)
        if not text.strip():
            checks.append(None)
        else:
            needle = str(clause["phrase_in_agent"]).lower()
            hit = needle in text
            checks.append(hit)
            if hit:
                quote = str(clause["phrase_in_agent"])
    if "phrase_in_opening" in clause:
        # Required-disclosure position: the opening agent turn only.
        opening = _opening_text(call)
        if not opening.strip():
            checks.append(None)
        else:
            needle = str(clause["phrase_in_opening"]).lower()
            hit = needle in opening
            checks.append(hit)
            if hit:
                quote = str(clause["phrase_in_opening"])
    if "phrase_in_user" in clause:
        text = _user_text(call)
        if not text.strip():
            checks.append(None)
        else:
            needle = str(clause["phrase_in_user"]).lower()
            checks.append(needle in text)
    if "agent_mentions" in clause:
        text = _agent_text(call)
        tokens = _tokens(clause["agent_mentions"])
        if not text.strip():
            checks.append(None)
        else:
            checks.append(all(token in text for token in tokens))
            if checks[-1]:
                quote = ", ".join(tokens)
    if "tool_called" in clause:
        name = str(clause["tool_called"]).lower()
        names = [tool.name.lower() for tool in call.tools]
        checks.append(any(name in item for item in names))
        if checks[-1]:
            quote = str(clause["tool_called"])
    if "tool_status" in clause:
        wanted = str(clause["tool_status"]).lower()
        if wanted in {"error", "timeout"}:
            checks.append(any(tool.status in _ERROR_STATUS for tool in call.tools))
        elif wanted in {"ok", "success"}:
            checks.append(any(tool.status in _SUCCESS_STATUS for tool in call.tools))
        else:
            raise ValueError("tool_status must be error, timeout, or ok")
    if "hangup_in" in clause:
        if call.hangup is None:
            checks.append(None)
        else:
            allowed = {item.lower() for item in _tokens(clause["hangup_in"])}
            checks.append(call.hangup.reason.value.lower() in allowed)
    if not checks:
        raise ValueError("empty predicate clause")
    if any(item is None for item in checks):
        return {"value": None, "quote": quote}
    return {"value": all(bool(item) for item in checks), "quote": quote}


def _tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip().lower() for part in value.split(",") if part.strip()]
    if isinstance(value, Sequence):
        return [str(part).strip().lower() for part in value if str(part).strip()]
    return [str(value).strip().lower()]


def _agent_text(call: CallRevision) -> str:
    return " ".join(turn.text.lower() for turn in call.agent_turns() if turn.text)


def _opening_text(call: CallRevision) -> str:
    for turn in call.agent_turns():
        if turn.text:
            return turn.text.lower()
    return ""


def _user_text(call: CallRevision) -> str:
    return " ".join(turn.text.lower() for turn in call.user_turns() if turn.text)


def _out(verdict: JudgeVerdict, rationale: str, quotes: list[str]) -> dict[str, Any]:
    passed = (
        True if verdict is JudgeVerdict.PASS else False if verdict is JudgeVerdict.FAIL else None
    )
    return {
        "verdict": verdict.value,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "rationale": rationale,
        "quotes": quotes[:5],
    }
