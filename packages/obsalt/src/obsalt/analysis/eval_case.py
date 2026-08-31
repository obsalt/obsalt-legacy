"""Turn a confirmed high/critical failure into a redacted regression fixture."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from obsalt.analysis.hallucination import (
    PAGEABLE_SEVERITIES,
    detect_claims,
    grounding_corpus,
    tool_effectively_failed,
    tool_effectively_succeeded,
)
from obsalt.analysis.rollups import _is_confirmed_claim
from obsalt.domain.models import CallRevision

SCHEMA = "obsalt.eval_case/1"
_EXCERPT = 240


def build_eval_case(
    call: CallRevision,
    analysis: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """JSON case a human can commit next to record-golden. Not a simulation runner."""
    payload, claims = _claims_from_analysis(analysis)
    if not claims:
        claims = detect_claims(call)
        payload = {}
    pageable = [
        item
        for item in claims
        if _is_confirmed_claim(item, payload)
        and str(item.get("severity") or "") in PAGEABLE_SEVERITIES
    ]
    confirmed = pageable or [item for item in claims if _is_confirmed_claim(item, payload)]
    ranks = {"critical": 0, "high": 1, "medium": 2}
    severity = "high"
    if confirmed:
        severity = min(
            (str(item.get("severity") or "high") for item in confirmed),
            key=lambda value: ranks.get(value, 9),
        )
    user_goal = next((turn.text for turn in reversed(call.user_turns()) if turn.text), "")
    return {
        "schema": SCHEMA,
        "source_failure": "production",
        "severity": severity,
        "call": {
            "call_id": call.call_id,
            "revision": call.revision,
            "source": call.source,
            "agent_id": call.agent_id if call.agent_id != "unknown" else "",
            "hangup": call.hangup.reason.value if call.hangup else "",
        },
        "caller_goal": user_goal[:_EXCERPT],
        "allowed_source": [_excerpt(part) for part in grounding_corpus(call)[:8]],
        "forbidden_claims": [
            str(item.get("span_text") or "") for item in confirmed if item.get("span_text")
        ],
        "claims": [_claim_row(item) for item in confirmed],
        "tools": [
            {
                "name": tool.name,
                "status": (
                    "failed"
                    if tool_effectively_failed(tool)
                    else "success"
                    if tool_effectively_succeeded(tool)
                    else tool.status.value
                ),
                "result": _excerpt(str(tool.error or tool.result or tool.result_ref or "")),
            }
            for tool in call.tools
        ],
        "expected_agent_behavior": [
            "do not repeat the forbidden claims",
            "stay inside allowed_source",
        ],
        "note": "Redacted shape only. Commit beside plugin goldens. Not a caller simulator.",
    }


def _claims_from_analysis(
    analysis: Sequence[Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not analysis:
        return {}, []
    for row in analysis:
        payload = row.payload if hasattr(row, "payload") else row
        if not isinstance(payload, Mapping):
            continue
        analyzer = ""
        execution = getattr(row, "execution", None)
        if execution is not None:
            analyzer = getattr(execution, "analyzer_id", "")
        if analyzer not in {"", "hallucination"}:
            continue
        items = payload.get("claims") or payload.get("candidates") or []
        if items:
            as_dict = dict(payload)
            return as_dict, [dict(item) for item in items if isinstance(item, dict)]
    return {}, []


def _claim_row(item: Mapping[str, Any]) -> dict[str, Any]:
    binding = item.get("binding") if isinstance(item.get("binding"), Mapping) else {}
    tool_id = ""
    if isinstance(binding, Mapping):
        tool_id = str(binding.get("tool_id") or "")
    if not tool_id:
        for span in item.get("evidence_spans") or []:
            if isinstance(span, Mapping) and span.get("tool_id"):
                tool_id = str(span.get("tool_id") or "")
                break
    return {
        "kind": str(item.get("kind") or ""),
        "span_text": str(item.get("span_text") or ""),
        "agent_span": str(item.get("agent_span") or ""),
        "verdict": str(item.get("verdict") or ""),
        "severity": str(item.get("severity") or ""),
        "evidence": list(item.get("evidence") or []),
        "tool_id": tool_id,
    }


def _excerpt(value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= _EXCERPT:
        return cleaned
    return cleaned[: _EXCERPT - 1].rstrip() + "…"
