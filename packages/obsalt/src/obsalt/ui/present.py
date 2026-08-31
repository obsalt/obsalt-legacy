"""Human-readable console view-models. No new facts — labels and ranking only.

The join view is still the product. These helpers exist so the seven screens
answer the six capability questions instead of dumping decoder internals.
Waterfall geometry is computed only from INTERVAL clocks already on the call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from obsalt.analysis.hallucination import grounding_corpus
from obsalt.analysis.hangup import ENDING_NOT_REPORTED, ENDING_UNROOTED, hangup_bucket
from obsalt.analysis.quality_card import card_dimension_status
from obsalt.domain.enums import Capability, EvidenceKind, GroundingKind, HangupReason, Role, Speaker
from obsalt.domain.models import CallRevision, StageMeasurement, Turn
from obsalt.security.authz import can_map

HANGUP_LABELS: dict[str, str] = {
    HangupReason.USER_HANGUP.value: "Caller hung up",
    HangupReason.AGENT_HANGUP.value: "Agent ended the call",
    HangupReason.TRANSFER.value: "Transferred",
    HangupReason.VOICEMAIL.value: "Voicemail",
    HangupReason.INACTIVITY.value: "Inactivity",
    HangupReason.SILENCE_TIMEOUT.value: "Silence timeout",
    HangupReason.MAX_DURATION.value: "Hit max duration",
    HangupReason.BUSY.value: "Busy",
    HangupReason.NO_ANSWER.value: "No answer",
    HangupReason.DIAL_FAILED.value: "Dial failed",
    HangupReason.ERROR_STT.value: "Speech-to-text error",
    HangupReason.ERROR_LLM.value: "Model error",
    HangupReason.ERROR_TTS.value: "Voice error",
    HangupReason.ERROR_TOOL.value: "Tool error",
    HangupReason.ERROR_TELEPHONY.value: "Telephony error",
    HangupReason.ERROR_UNKNOWN.value: "Unclassified platform error",
    HangupReason.SPAM.value: "Marked as spam",
    HangupReason.CONCURRENCY.value: "Concurrency limit",
    HangupReason.CANCELLED.value: "Cancelled",
    HangupReason.COMPLETED.value: "Completed",
    HangupReason.UNKNOWN.value: "Unmapped ending",
    ENDING_NOT_REPORTED: "Ending not reported",
    ENDING_UNROOTED: "Still assembling",
}


STAGE_LABELS: dict[str, str] = {
    "vad": "Voice activity",
    "stt": "Speech-to-text",
    "llm": "Model",
    "tts": "Text-to-speech",
    "playout": "Playout",
    "tool": "Tool",
    "e2e": "End-to-end turn",
    "ttfa": "Time to first audio",
    "transport": "Transport",
    "endpointing": "Endpointing",
    "user_input": "User input",
    "generation": "Generation",
}

METRIC_LABELS: dict[str, str] = {
    "duration": "duration",
    "ttft": "time to first token",
    "ttfb": "time to first byte",
    "first_audio": "first audio",
}

FIDELITY_LABELS: dict[str, str] = {
    "stage_level": "Stage waterfall from real start and end clocks.",
    "turn_level": "Turn timing. Stage durations are not placed on a waterfall.",
    "message_level": "Message anchors — often whole seconds.",
    "call_level": "Call-level aggregates only.",
    "none": "Almost no clocks on this call.",
}

FLAG_LABELS: dict[str, str] = {
    "silence": "Silence / inactivity",
    "tool_failure": "Tool failed",
    "low_stt_confidence": "Low transcript confidence",
    "dead_air": "Dead air",
    "truncated_llm": "Truncated model reply",
    "loop_detected": "Agent loop",
    "user_repeated": "Caller repeated themselves",
    "farewell_missed": "Farewell not honored",
    "escalation_unmet": "Escalation request unmet",
    "monologue": "Agent monologue",
    "barge_in_no_recovery": "Barge-in not recovered",
    "dead_air_hangup": "Dead air before hangup",
    "double_invocation": "Duplicate tool invocation",
    "retry_storm": "Tool retry storm",
    "unfulfilled_promise": "Unfulfilled promise",
    "broken_transfer_promise": "Broken transfer promise",
    "pan_spoken": "Card number spoken aloud",
    "ssn_spoken": "SSN spoken aloud",
    "verbal_secret_request": "Verbal secret request",
    "price_claim": "Ungrounded price",
    "fabricated_id": "Fabricated id",
    "phantom_tool_success": "Claimed a tool that failed",
    "phantom_tool_failure": "Claimed a tool failed that succeeded",
    "date_time_claim": "Ungrounded date or time",
    "count_claim": "Ungrounded count",
    "args_mismatch": "Wrong value passed to tool",
    "commitment": "Ungrounded commitment",
    "ungrounded_fact": "Ungrounded fact",
    "policy_claim": "Ungrounded policy",
    "private_knowledge": "Private knowledge",
    "eval_fail": "Eval failed",
    "eval_incomplete": "Eval incomplete",
    "hallucination": "Hallucination",
    "hallucination_candidate": "Grounding candidate",
}

DIMENSION_LABELS: dict[str, str] = {
    "faithfulness": "Faithfulness",
    "tool_integrity": "Said vs done",
}

GROUNDING_LABELS: dict[str, str] = {
    GroundingKind.SYSTEM_PROMPT.value: "Prompt",
    GroundingKind.KNOWLEDGE.value: "Knowledge",
    GroundingKind.TOOL_RESULT.value: "Tool result",
}

_AXIS_ACCURACY = ("faithfulness", "tool_integrity")

SPEAKER_LABELS: dict[str, str] = {
    Speaker.USER.value: "Caller",
    Speaker.AGENT.value: "Agent",
    Speaker.SYSTEM.value: "System",
    Speaker.TOOL.value: "Tool",
    Speaker.UNKNOWN.value: "Speaker not reported",
}

EVAL_STATE_LABELS: dict[str, str] = {
    "pending": "not judged",
    "sampled_out": "skipped by sample",
    "budget_blocked": "budget blocked",
    "running": "running",
    "failed": "judge failed",
    "completed": "completed",
}

_FLAG_PREVIEW = 2

_LOST_HANGUPS = frozenset(
    {
        HangupReason.USER_HANGUP.value,
        HangupReason.SILENCE_TIMEOUT.value,
        HangupReason.INACTIVITY.value,
        HangupReason.ERROR_STT.value,
        HangupReason.ERROR_LLM.value,
        HangupReason.ERROR_TTS.value,
        HangupReason.ERROR_TOOL.value,
        HangupReason.ERROR_TELEPHONY.value,
        HangupReason.ERROR_UNKNOWN.value,
        HangupReason.DIAL_FAILED.value,
        HangupReason.BUSY.value,
        HangupReason.NO_ANSWER.value,
    }
)

_PREFERRED_LATENCY = ("e2e", "ttfa", "llm", "stt", "tts", "generation", "user_input")


def hangup_label(reason: str | None, *, provider_code: str | None = None) -> str:
    code = (provider_code or "").strip()
    if not reason or reason == ENDING_NOT_REPORTED:
        return HANGUP_LABELS[ENDING_NOT_REPORTED]
    if reason == ENDING_UNROOTED:
        return HANGUP_LABELS[ENDING_UNROOTED]
    if reason == HangupReason.UNKNOWN.value:
        return f"Unmapped ending ({code})" if code else "Unmapped ending"
    if reason == HangupReason.ERROR_UNKNOWN.value:
        base = HANGUP_LABELS[HangupReason.ERROR_UNKNOWN.value]
        return f"{base} ({code})" if code else base
    return HANGUP_LABELS.get(reason, reason.replace("_", " "))


def hangup_label_for(call: CallRevision) -> str:
    code = call.hangup.provider_code if call.hangup else None
    return hangup_label(hangup_bucket(call), provider_code=code)


def hangup_why(reason: str | None) -> str:
    return ""


def hangup_tone(reason: str | None) -> str:
    if not reason or reason in {ENDING_NOT_REPORTED, ENDING_UNROOTED}:
        return ""
    if reason in _LOST_HANGUPS:
        return "loss"
    if reason == HangupReason.COMPLETED.value:
        return "ok"
    return ""


def agent_label(agent_id: str | None) -> str:
    if not agent_id or agent_id == "unknown":
        return ""
    return agent_id


def speaker_label(speaker: str | None) -> str:
    if not speaker or speaker == Speaker.UNKNOWN.value:
        return SPEAKER_LABELS[Speaker.UNKNOWN.value]
    return SPEAKER_LABELS.get(speaker, speaker.replace("_", " ").title())


def stage_label(stage: str | None) -> str:
    if not stage:
        return "Stage not reported"
    return STAGE_LABELS.get(stage, stage.replace("_", " "))


def metric_label(metric: str | None) -> str:
    if not metric:
        return "duration"
    return METRIC_LABELS.get(metric, metric.replace("_", " "))


def fidelity_label(fidelity: str | None) -> str:
    if not fidelity:
        return FIDELITY_LABELS["none"]
    return FIDELITY_LABELS.get(fidelity, fidelity)


def flag_label(kind: str | None) -> str:
    if not kind:
        return "Flag"
    return FLAG_LABELS.get(kind, kind.replace("_", " "))


def format_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        seconds = value / 1000.0
        text = f"{seconds:.1f}".rstrip("0").rstrip(".")
        return f"{text} s"
    return f"{int(round(value))} ms"


def format_cost(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 0.01:
        return f"${value:.2f}"
    text = f"${value:.4f}".rstrip("0").rstrip(".")
    return text if text != "$" else "$0"


def format_call_duration(value_ms: float | None) -> str:
    if value_ms is None:
        return "—"
    total = max(0, int(round(value_ms / 1000.0)))
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d %b %H:%M UTC")


def short_id(call_id: str) -> str:
    if len(call_id) <= 12:
        return call_id
    return call_id[-8:]


def snippet(text: str | None, limit: int = 96) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def last_text(call: CallRevision, speaker: Speaker) -> str:
    for turn in reversed(call.turns):
        if turn.speaker is speaker and turn.text:
            return turn.text
    return ""


def present_flags(raw: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in raw or []:
        kind = str(row.get("kind") or "")
        if not kind:
            continue
        reason = (
            row.get("reason")
            or row.get("agent_span")
            or row.get("span")
            or row.get("claim")
            or row.get("span_text")
            or ""
        )
        verdict = str(row.get("verdict") or "")
        model = str(row.get("model") or "")
        heuristic = model.startswith("heuristic")
        missing = verdict == "evidence_missing"
        confirmed = verdict == "contradicted" and model.startswith("detector")
        pending = (
            bool(row.get("pending"))
            or heuristic
            or missing
            or verdict == "needs_review"
            or (not confirmed and verdict in {"contradicted", "unsupported"})
            or (
                bool(row.get("needs_llm"))
                and verdict not in {"contradicted", "unsupported", "grounded"}
            )
        )
        label = flag_label(kind)
        if missing:
            label = f"{label} — evidence missing"
        elif heuristic and not confirmed:
            label = f"{label} — heuristic, not confirmed"
        elif confirmed and model.startswith("detector"):
            label = (
                f"{label} — contradicted by tool result"
                if verdict == "contradicted"
                else f"{label} — {verdict}"
            )
        elif confirmed:
            extra = f" ({model})" if model else ""
            label = f"{label} — {verdict}{extra}"
        elif pending and not confirmed:
            label = f"{label} — not yet judged"
        turn_index = row.get("turn_index")
        items.append(
            {
                "kind": kind,
                "label": label,
                "reason": str(reason) if reason else "",
                "pending": pending and not confirmed,
                "tone": "loss" if confirmed else "warn",
                "turn_index": int(turn_index) if isinstance(turn_index, int) else None,
            }
        )
    return items


def present_call_row(
    call: CallRevision, flags: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    reason = hangup_bucket(call)
    code = call.hangup.provider_code if call.hangup else ""
    last_user = last_text(call, Speaker.USER)
    last_agent = last_text(call, Speaker.AGENT)
    latency_stage, latency_ms = _call_latency(call)
    presented_flags = present_flags(flags)
    return {
        "call_id": call.call_id,
        "short_id": short_id(call.call_id),
        "revision": call.revision,
        "agent_id": agent_label(call.agent_id),
        "source": call.source,
        "when": format_when(call.started_at or call.created_at),
        "duration": format_call_duration(call.duration_ms),
        "hangup": hangup_label(reason, provider_code=code),
        "hangup_reason": reason,
        "hangup_tone": hangup_tone(reason),
        "hangup_why": hangup_why(reason),
        "provider_code": code,
        "last_user": snippet(last_user),
        "last_agent": snippet(last_agent),
        "flags": presented_flags,
        "flags_preview": presented_flags[:_FLAG_PREVIEW],
        "flag_overflow": max(0, len(presented_flags) - _FLAG_PREVIEW),
        "latency": (
            f"highest {stage_label(latency_stage)} {format_ms(latency_ms)}" if latency_ms else ""
        ),
        "latency_ms": latency_ms,
        "cost": format_cost(call.cost),
        "loss_score": 0.0,
        "loss_reasons": [],
        "fidelity": call.timeline_fidelity.value,
        "fidelity_label": fidelity_label(call.timeline_fidelity.value),
        "decoder_version": call.decoder_version,
        "headline": _headline(call, last_user, reason, provider_code=code),
    }


def present_call_rows(
    calls: Sequence[CallRevision],
    flags_by_call: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    flags_by_call = flags_by_call or {}
    return [present_call_row(call, flags_by_call.get(call.call_id)) for call in calls]


def present_fleet(
    calls: Sequence[CallRevision],
    *,
    latency: Mapping[str, Any] | None = None,
    hangups: Mapping[str, Any] | None = None,
    quality: Mapping[str, Any] | None = None,
    tools: Mapping[str, Any] | None = None,
    range_qs: str = "",
) -> dict[str, Any]:
    n = len(calls)
    reasons = Counter(hangup_bucket(call) for call in calls)
    top_reason, top_count = reasons.most_common(1)[0] if reasons else ("", 0)
    slowest = _slowest_stage(latency or {})
    tool_failures = 0
    tool_invocations = int((tools or {}).get("invocation_count") or 0)
    for stats in ((tools or {}).get("by_tool") or {}).values():
        tool_failures += int(stats.get("failures") or 0)
    evals = (quality or {}).get("evals") or {}
    hallu = (quality or {}).get("hallucinations") or {}
    hallucinations = int(hallu.get("count") or 0)
    hallucination_candidates = int(hallu.get("candidate_count") or 0)
    scanned = int(hallu.get("scanned") or 0)
    top_cluster = None
    clusters = (hangups or {}).get("clusters") or []
    if clusters:
        top_cluster = clusters[0]
    return {
        "call_count": n,
        "lost_count": 0,
        "lost_pct": None,
        "top_hangup": hangup_label(top_reason) if top_reason else "—",
        "top_hangup_reason": top_reason,
        "top_hangup_count": top_count,
        "slowest": slowest,
        "tool_failures": tool_failures,
        "tool_invocations": tool_invocations,
        "hallucinations": hallucinations,
        "hallucination_candidates": hallucination_candidates,
        "eval_failed": int(evals.get("failed") or 0),
        "eval_completed": int(evals.get("completed") or 0),
        "eval_passed": int(evals.get("passed") or 0),
        "top_cluster_id": (top_cluster or {}).get("top_call_id") or "",
        "questions": _fleet_questions(
            n=n,
            top_hangup=hangup_label(top_reason) if top_reason else "",
            top_hangup_count=top_count,
            slowest=slowest,
            tool_failures=tool_failures,
            tool_invocations=tool_invocations,
            hallucinations=hallucinations,
            hallucination_candidates=hallucination_candidates,
            scanned=scanned,
            eval_failed=int(evals.get("failed") or 0),
            eval_passed=int(evals.get("passed") or 0),
            eval_completed=int(evals.get("completed") or 0),
            eligible=n,
            range_qs=range_qs,
        ),
    }


def present_hangups(
    rollup: Mapping[str, Any],
    call_count: int,
    calls: Sequence[CallRevision] | None = None,
) -> dict[str, Any]:
    clusters = []
    reason_sizes: Counter[str] = Counter()
    by_id = {call.call_id: present_call_row(call) for call in calls or []}
    for row in rollup.get("clusters") or []:
        size = int(row.get("size") or 0)
        reason = str(row.get("reason") or "")
        reason_sizes[reason] += size
        top_call_id = str(row.get("top_call_id") or "")
        call_ids = [str(item) for item in (row.get("call_ids") or []) if item]
        cluster_calls = [by_id[item] for item in call_ids if item in by_id]
        if not cluster_calls:
            cluster_calls = [{"call_id": item, "short_id": short_id(item)} for item in call_ids]
        clusters.append(
            {
                **row,
                "label": hangup_label(reason, provider_code=str(row.get("provider_code") or "")),
                "why": "",
                "hangup_tone": hangup_tone(reason),
                "party_label": speaker_label(str(row.get("party") or ""))
                if row.get("party")
                else "",
                "last_speaker_label": (
                    speaker_label(str(row.get("last_speaker") or ""))
                    if row.get("last_speaker")
                    else ""
                ),
                "share": _pct(size, call_count),
                "bar_pct": _bar_pct(size, call_count),
                "loss_pct": 0,
                "loss_reason_labels": [],
                "last_user_text": snippet(row.get("last_user_text") or "", 160),
                "last_agent_text": snippet(row.get("last_agent_text") or "", 160),
                "top_call_id": top_call_id,
                "top_short_id": short_id(top_call_id) if top_call_id else "",
                "calls": cluster_calls,
            }
        )
    shares = []
    for reason, size in reason_sizes.most_common():
        shares.append(
            {
                "reason": reason,
                "label": hangup_label(reason),
                "size": size,
                "share": _pct(size, call_count),
                "bar_pct": _bar_pct(size, call_count),
                "hangup_tone": hangup_tone(reason),
            }
        )
    return {
        "as_of_generation": rollup.get("as_of_generation") or "",
        "call_count": call_count,
        "shares": shares,
        "clusters": clusters,
        "note": (
            "How calls ended, clustered by hangup and last words. "
            "Ending not reported means the source sent no hangup — not that we failed to classify."
        ),
    }


def present_latency(
    rollup: Mapping[str, Any],
    calls: Sequence[CallRevision],
    tools: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for stage, stats in (rollup.get("sample_percentiles") or {}).items():
        if not isinstance(stats, dict):
            continue
        metric_rows = [
            (metric, payload)
            for metric, payload in stats.items()
            if isinstance(payload, dict) and "p50" in payload and metric not in {"by_agent"}
        ]
        if not metric_rows and "p50" in stats:
            metric_rows = [(str(stats.get("metric") or "duration"), stats)]
        seen: set[tuple[str, str]] = set()
        for metric, payload in metric_rows:
            key = (str(stage), str(payload.get("metric") or metric))
            if key in seen:
                continue
            seen.add(key)
            samples.append(
                {
                    "stage": stage,
                    "stage_label": stage_label(str(stage)),
                    "metric": payload.get("metric") or metric,
                    "metric_label": metric_label(str(payload.get("metric") or metric)),
                    "p50": payload.get("p50"),
                    "p95": payload.get("p95"),
                    "n": payload.get("n") or payload.get("count") or 0,
                    "p50_text": format_ms(payload.get("p50")),
                    "p95_text": format_ms(payload.get("p95")),
                }
            )
    max_p95 = max((item["p95"] or 0) for item in samples) if samples else 0
    for item in samples:
        item["bar_pct"] = _bar_pct(item["p95"], max_p95)
    agents: list[dict[str, Any]] = []
    for agent_id, stages in (rollup.get("by_agent") or {}).items():
        for stage, stats in (stages or {}).items():
            if not isinstance(stats, dict):
                continue
            agents.append(
                {
                    "agent_id": agent_id,
                    "stage": stage,
                    "stage_label": stage_label(str(stage)),
                    "metric_label": metric_label(str(stats.get("metric") or "duration")),
                    "p50_text": format_ms(stats.get("p50")),
                    "p95_text": format_ms(stats.get("p95")),
                    "p95": stats.get("p95"),
                    "n": stats.get("n") or 0,
                }
            )
    aggregates = []
    for row in rollup.get("provider_aggregates") or []:
        call_id = str(row.get("call_id") or "")
        aggregates.append(
            {
                **row,
                "stage_label": stage_label(str(row.get("stage") or "")),
                "value_text": format_ms(row.get("value_ms")),
                "short_id": short_id(call_id) if call_id else "",
            }
        )
    aggregate_total = len(aggregates)
    aggregates = aggregates[:20]
    tool_rows = []
    for name, stats in ((tools or {}).get("by_tool") or {}).items():
        tool_rows.append(
            {
                "name": name,
                "count": stats.get("count") or 0,
                "failures": stats.get("failures") or 0,
                "successes": stats.get("successes") or 0,
                "success_rate": stats.get("success_rate"),
                "success_pct": _pct(int(stats.get("successes") or 0), int(stats.get("count") or 0)),
                "duration_p50": format_ms(stats.get("duration_p50_ms")),
                "duration_p95": format_ms(stats.get("duration_p95_ms")),
                "duration_n": stats.get("duration_n") or 0,
            }
        )
    agent_ids = {row["agent_id"] for row in agents}
    group_max: dict[tuple[str, str], float] = {}
    group_agents: dict[tuple[str, str], set[str]] = {}
    group_values: dict[tuple[str, str], set[float]] = {}
    for row in agents:
        raw = row.get("p95")
        value = float(raw) if isinstance(raw, (int, float)) else 0.0
        row["_p95"] = value
        key = (str(row.get("stage") or ""), str(row.get("metric") or row.get("metric_label") or ""))
        group_max[key] = max(group_max.get(key, 0.0), value)
        group_agents.setdefault(key, set()).add(str(row.get("agent_id") or ""))
        if value:
            group_values.setdefault(key, set()).add(value)
        row["slower"] = False
    if len(agent_ids) > 1:
        for row in agents:
            key = (
                str(row.get("stage") or ""),
                str(row.get("metric") or row.get("metric_label") or ""),
            )
            peak = group_max.get(key, 0.0)
            comparable = len(group_agents.get(key, ())) > 1 and len(group_values.get(key, ())) > 1
            row["slower"] = bool(comparable and peak and row["_p95"] >= peak)
            row.pop("_p95", None)
    else:
        for row in agents:
            row.pop("_p95", None)
    slowest_calls = _slowest_calls(calls)
    return {
        "as_of_generation": rollup.get("as_of_generation") or "",
        "note": "P50 / P95 from real stage measurements only.",
        "aggregate_lead_note": (
            "These are the provider's published stats, not our samples. "
            "They are not mixed into sample p50/p95."
        ),
        "samples": samples,
        "by_agent": agents,
        "agent_count": len(agent_ids),
        "provider_aggregates": aggregates,
        "provider_aggregate_count": aggregate_total,
        "slowest_calls": slowest_calls,
        "tools": tool_rows,
        "call_count": len(calls),
        "has_samples": bool(samples),
        "has_aggregates": bool(aggregates),
        "aggregates_primary": bool(aggregates) and not samples,
        "has_tools": bool(tool_rows),
        "has_slowest": bool(slowest_calls),
    }


def present_quality(
    rollup: Mapping[str, Any],
    spend_usd: float,
    budget_usd: float,
    calls: Sequence[CallRevision] | None = None,
) -> dict[str, Any]:
    evals = rollup.get("evals") or {}
    completed = int(evals.get("completed") or 0)
    passed = int(evals.get("passed") or 0)
    failed = int(evals.get("failed") or 0)
    hallucinations = rollup.get("hallucinations") or {}
    kinds = [
        {
            "kind": kind,
            "label": flag_label(str(kind)),
            "count": count,
        }
        for kind, count in (hallucinations.get("by_kind") or {}).items()
    ]
    by_id = {call.call_id: present_call_row(call) for call in calls or []}
    queue = []
    for item in rollup.get("review_queue") or []:
        call_id = str(item.get("call_id") or "")
        row = by_id.get(call_id) or {}
        queue.append(
            {
                **item,
                "label": flag_label(str(item.get("kind") or "")),
                "state_label": str(item.get("state") or "").replace("_", " "),
                "passed_label": _passed_label(
                    item.get("passed"), state=str(item.get("state") or "")
                ),
                "short_id": short_id(call_id) if call_id else "",
                "severity": str(item.get("severity") or ""),
                "severity_label": str(item.get("severity") or "").replace("_", " ") or "—",
                "hangup": row.get("hangup") or "",
                "hangup_tone": row.get("hangup_tone") or "",
                "last_user": row.get("last_user") or "",
                "last_agent": row.get("last_agent") or "",
            }
        )
    pass_pct = _pct(passed, completed)
    eligible = int(evals.get("eligible") or 0)
    confirmed = int(hallucinations.get("count") or 0)
    missing = int(hallucinations.get("evidence_missing") or 0)
    candidates = int(hallucinations.get("candidate_count") or 0)
    not_settled = int(hallucinations.get("not_settled") or 0)
    judged_caption = (
        f"{completed} evals judged · {confirmed} calls with a confirmed flag"
        if eligible
        else "No calls in this range"
    )
    candidate_kinds = [
        {"kind": kind, "label": flag_label(str(kind)), "count": count}
        for kind, count in (hallucinations.get("candidates_by_kind") or {}).items()
    ]
    card = rollup.get("quality_card") or {}
    return {
        "as_of_generation": rollup.get("as_of_generation") or "",
        "note": (
            "Not a scorecard. Missing judge output is never a pass. "
            "Detectors still run on every call. English judges need a runner. Pills count calls."
        ),
        "evals": evals,
        "pass_pct": pass_pct,
        "pass_text": f"{pass_pct}%" if pass_pct is not None else "Not judged",
        "fail_pct": _pct(failed, completed),
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "eligible": eligible,
        "judged_caption": judged_caption,
        "headline": (
            f"{confirmed} calls with a confirmed flag · {missing} evidence missing"
            f" · {not_settled} not settled."
        ),
        "baseline": rollup.get("baseline") or {},
        "hallucination_count": confirmed,
        "hallucination_claim_count": int(hallucinations.get("claim_count") or 0),
        "hallucination_kinds": kinds,
        "candidate_count": candidates,
        "candidate_claim_count": int(hallucinations.get("candidate_claim_count") or 0),
        "candidate_kinds": candidate_kinds,
        "scanned": int(hallucinations.get("scanned") or 0),
        "unscannable": int(hallucinations.get("unscannable") or 0),
        "checkable": int(hallucinations.get("checkable") or 0),
        "not_settled_calls": not_settled,
        "evidence_missing_calls": missing,
        "card_calls": int(card.get("calls") or 0),
        "card_critical": int(card.get("critical") or 0),
        "accuracy_dims": [],
        "experience_dims": [],
        "review_queue": queue,
        "spend_usd": spend_usd,
        "budget_usd": budget_usd,
        "by_rubric": evals.get("by_rubric") or {},
        "groundedness": rollup.get("groundedness") or {},
    }


def present_search(
    hits: Sequence[Mapping[str, Any]],
    calls: Sequence[CallRevision],
    query: str,
) -> list[dict[str, Any]]:
    by_id = {call.call_id: call for call in calls}
    rows: list[dict[str, Any]] = []
    for hit in hits:
        call_id = str(hit.get("call_id") or "")
        call = by_id.get(call_id)
        row = {
            "call_id": call_id,
            "short_id": short_id(call_id),
            "agent_id": hit.get("agent_id") or (call.agent_id if call else ""),
            "source": hit.get("source") or (call.source if call else ""),
            "hangup": "",
            "hangup_tone": "",
            "last_user": "",
            "last_agent": "",
            "when": "",
            "duration": "",
            "snippet": "",
        }
        if call is not None:
            presented = present_call_row(call)
            row["hangup"] = presented["hangup"]
            row["hangup_tone"] = presented["hangup_tone"]
            row["agent_id"] = presented["agent_id"]
            row["last_user"] = presented["last_user"]
            row["last_agent"] = presented["last_agent"]
            row["when"] = presented["when"]
            row["duration"] = presented["duration"]
            row["snippet"] = _match_snippet(call, query)
        rows.append(row)
    return rows


def present_quality_card(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    dimensions = (payload or {}).get("dimensions") or {}
    rows = []
    for name, dim in dimensions.items():
        if not isinstance(dim, dict):
            continue
        reason = str(dim.get("reason") or "")
        status = card_dimension_status(dim)
        tone = "ok" if status == "pass" else "loss" if status == "fail" else "warn"
        rows.append(
            {
                "name": name,
                "label": DIMENSION_LABELS.get(name, name.replace("_", " ")),
                "status": status,
                "status_label": status.replace("_", " ") or "not judged",
                "reason": reason,
                "tone": tone,
            }
        )
    return rows


def present_quality_card_axes(payload: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    by_name = {row["name"]: row for row in present_quality_card(payload)}
    return {
        "accuracy": [by_name[name] for name in _AXIS_ACCURACY if name in by_name],
        "experience": [],
    }


def present_evidence_flags(
    raw: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    confirmed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in present_flags(raw):
        source = next(
            (
                item
                for item in raw or []
                if str(item.get("kind") or "") == row["kind"]
                and item.get("turn_index") == row.get("turn_index")
            ),
            {},
        )
        entry = {
            **row,
            "agent_span": str(
                source.get("agent_span") or source.get("evidence") or row.get("reason") or ""
            ),
            "span_text": str(source.get("span_text") or ""),
            "evidence": _evidence_line(source),
        }
        if row.get("pending"):
            candidates.append(entry)
        else:
            confirmed.append(entry)
    return confirmed, candidates


def _evidence_line(row: Mapping[str, Any]) -> str:
    spans = row.get("evidence_spans") or []
    if isinstance(spans, list):
        for span in spans:
            if not isinstance(span, dict):
                continue
            name = str(span.get("tool_name") or "")
            text = str(span.get("text") or "")
            if name and text:
                return f"{name}  {text}"
            if text:
                return text
    evidence = row.get("evidence")
    if isinstance(evidence, list) and evidence:
        return str(evidence[0])
    return str(evidence or "")


def groundedness_parts(
    text: str,
    spans: Sequence[Mapping[str, Any]],
    *,
    title: str,
) -> list[dict[str, Any]]:
    """Character spans on already-redacted text. Out of range spans are skipped."""
    if not text:
        return []
    usable: list[tuple[int, int]] = []
    for span in spans:
        try:
            start = int(span.get("start") or 0)
            end = int(span.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if start < 0 or end > len(text) or end <= start:
            continue
        usable.append((start, end))
    if not usable:
        return []
    usable.sort()
    parts: list[dict[str, Any]] = []
    cursor = 0
    for start, end in usable:
        start = max(start, cursor)
        if end <= start:
            continue
        if start > cursor:
            parts.append({"text": text[cursor:start], "mark": False, "title": ""})
        parts.append({"text": text[start:end], "mark": True, "title": title})
        cursor = end
    if cursor < len(text):
        parts.append({"text": text[cursor:], "mark": False, "title": ""})
    return parts


def present_call_detail(
    call: CallRevision,
    timeline: Mapping[str, Any],
    flags: Sequence[Mapping[str, Any]],
    evals: Sequence[Mapping[str, Any]],
    quality_card: Mapping[str, Any] | None = None,
    groundedness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    call_row = present_call_row(call, flags)
    historical = bool((quality_card or {}).get("dimensions"))
    axes = (
        present_quality_card_axes(quality_card)
        if historical
        else {"accuracy": [], "experience": []}
    )
    card_rows = (
        [dim for dim in present_quality_card(quality_card) if dim["name"] in _AXIS_ACCURACY]
        if historical
        else []
    )
    confirmed_evidence, candidate_evidence = present_evidence_flags(flags)
    evals_shown = [
        present_eval(item)
        for item in evals
        if not item.get("shadow") and str(item.get("analyzer_id") or "") != "hallucination"
    ]
    evals_shadow = [
        present_eval(item)
        for item in evals
        if item.get("shadow") and str(item.get("analyzer_id") or "") != "hallucination"
    ]
    flags_shown = present_flags(flags)
    flagged_by_turn: dict[int, list[str]] = {}
    for flag in flags_shown:
        idx = flag.get("turn_index")
        if isinstance(idx, int):
            flagged_by_turn.setdefault(idx, []).append(str(flag.get("label") or flag.get("kind")))
    span_title = str((groundedness or {}).get("model") or "")
    by_turn_spans: dict[int, list[Mapping[str, Any]]] = {}
    for span in (groundedness or {}).get("spans") or []:
        if not isinstance(span, dict):
            continue
        idx = span.get("turn_index")
        if isinstance(idx, int):
            by_turn_spans.setdefault(idx, []).append(span)
    turns = []
    for turn in call.turns:
        turn_row = present_turn(turn)
        turn_row["flagged"] = flagged_by_turn.get(turn.index, [])
        turn_row["html_parts"] = groundedness_parts(
            turn.text, by_turn_spans.get(turn.index, []), title=span_title
        )
        turns.append(turn_row)
    return {
        **call_row,
        "pipeline": call.pipeline_architecture.value,
        "status": call.status.value,
        "direction": call.direction.value,
        "started_at": format_when(call.started_at),
        "ended_at": format_when(call.ended_at),
        "provider_code": call.hangup.provider_code if call.hangup else "",
        "timeline": present_timeline(call, timeline),
        "turns": turns,
        "tools": [present_tool(tool) for tool in call.tools],
        "flags": flags_shown,
        "grounding": present_grounding(call),
        "recording": present_recording(call),
        "cost": format_cost(call.cost),
        "evals": evals_shown,
        "evals_shadow": evals_shadow,
        "coverage": [
            {
                "signal": item.signal.value,
                "status": item.status.value,
                "reason": item.reason or "",
                "source_path": item.source_path or "",
                "decoder_version": item.decoder_version,
            }
            for item in call.coverage
        ],
        "measurements": [_present_measurement(item) for item in call.stage_measurements],
        "next_action": _next_action(call, flags),
        "assembler_version": call.assembler_version,
        "decoder_version": call.decoder_version,
        "revision": call.revision,
        "grounding_empty": not any(grounding_corpus(call)),
        "coverage_strip": _coverage_strip(call),
        "quality_card": card_rows,
        "quality_axes": axes,
        "quality_historical": historical,
        "evidence_confirmed": confirmed_evidence,
        "evidence_candidates": candidate_evidence,
        "quality_critical": bool((quality_card or {}).get("critical_failure")),
        "groundedness": dict(groundedness or {}),
        "groundedness_span_count": len((groundedness or {}).get("spans") or []),
    }


def present_timeline(call: CallRevision, timeline: Mapping[str, Any]) -> dict[str, Any]:
    raw_intervals = list(timeline.get("stage_intervals") or [])
    geometry = _interval_geometry(raw_intervals)
    presented_intervals = []
    for item, geom in zip(raw_intervals, geometry, strict=True):
        row = dict(item)
        row.update(geom)
        row["stage_label"] = stage_label(str(row.get("stage") or ""))
        row["value_text"] = format_ms(row.get("value_ms"))
        presented_intervals.append(row)

    def _chip(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **item,
            "stage_label": stage_label(str(item.get("stage") or "")),
            "metric_label": metric_label(str(item.get("metric") or "")),
            "value_text": format_ms(item.get("value_ms")),
        }

    unplaced = [_chip(item) for item in timeline.get("unplaced_stage_chips") or []]
    anchored = [_chip(item) for item in timeline.get("anchored_stage_chips") or []]
    duration_rows = _rank_duration_rows(unplaced + anchored)
    aggregates = [
        {
            **item,
            "stage_label": stage_label(str(item.get("stage") or "")),
            "value_text": format_ms(item.get("value_ms")),
        }
        for item in timeline.get("aggregates") or []
    ]
    turns: list[dict[str, Any]] = []
    turn_durations: list[float] = []
    for turn in call.turns:
        duration_ms = _turn_duration_ms(turn)
        if duration_ms is not None:
            turn_durations.append(duration_ms)
        turns.append(
            {
                "index": turn.index,
                "speaker": turn.speaker.value,
                "speaker_label": speaker_label(turn.speaker.value),
                "text": turn.text,
                "duration": format_ms(duration_ms) if duration_ms is not None else "",
                "duration_ms": duration_ms,
                "interrupted": turn.interrupted,
                "bar_pct": 0,
            }
        )
    max_turn = max(turn_durations) if turn_durations else 0
    for row in turns:
        if row["duration_ms"] is not None:
            row["bar_pct"] = _bar_pct(row["duration_ms"], max_turn)
    return {
        "reason": timeline.get("reason") or "",
        "fidelity_label": fidelity_label(str(timeline.get("timeline_fidelity") or "")),
        "draw_stage_waterfall": bool(timeline.get("draw_stage_waterfall")),
        "stage_intervals": presented_intervals,
        "unplaced_stage_chips": unplaced,
        "anchored_stage_chips": anchored,
        "duration_rows": duration_rows,
        "aggregates": aggregates,
        "turns": turns,
        "has_turn_bars": bool(turn_durations),
        "chips": duration_rows,
        "waterfall_caption": "Stage waterfall from real start and end clocks.",
        "duration_caption": (
            "The source measured these durations without start and end clocks, "
            "so they are not placed on the call."
        ),
        "aggregate_caption": "Provider-published stats, not mixed into our samples.",
    }


def present_recording(call: CallRevision) -> dict[str, str] | None:
    for item in call.evidence:
        if item.kind is not EvidenceKind.RECORDING:
            continue
        uri = (item.uri or "").strip()
        if not (uri.startswith("https://") or uri.startswith("http://")):
            continue
        return {"uri": uri, "label": "Open source recording"}
    return None


def present_grounding(call: CallRevision) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in call.grounding:
        if item.kind is GroundingKind.USER_TEXT or not (item.content or "").strip():
            continue
        rows.append(
            {
                "kind": item.kind.value,
                "label": GROUNDING_LABELS.get(item.kind.value, item.kind.value.replace("_", " ")),
                "text": snippet(item.content, 220),
            }
        )
        if len(rows) >= 8:
            break
    return rows


def _tool_result_text(tool: Any) -> str:
    if getattr(tool, "error", None):
        return snippet(str(tool.error), 180)
    result = getattr(tool, "result", None)
    if result in (None, ""):
        return ""
    if isinstance(result, str):
        return snippet(result, 180)
    return snippet(str(result), 180)


def present_turn(turn: Turn) -> dict[str, Any]:
    duration_ms = _turn_duration_ms(turn)
    confidence = turn.confidence
    confidence_text = ""
    if isinstance(confidence, (int, float)):
        confidence_text = f"{int(round(float(confidence) * 100))}%"
    return {
        "index": turn.index,
        "speaker": turn.speaker.value,
        "speaker_label": speaker_label(turn.speaker.value),
        "text": turn.text,
        "duration": format_ms(duration_ms) if duration_ms is not None else "",
        "interrupted": turn.interrupted,
        "confidence": confidence,
        "confidence_text": confidence_text,
        "flagged": [],
    }


def present_tool(tool: Any) -> dict[str, Any]:
    from obsalt.analysis.hallucination import tool_effectively_failed, tool_effectively_succeeded

    if tool_effectively_failed(tool):
        status = "failed"
    elif tool_effectively_succeeded(tool):
        status = "success"
    else:
        status = tool.status.value if hasattr(tool.status, "value") else str(tool.status)
    duration_ms = tool.duration_ms
    reported = duration_ms is not None and float(duration_ms) > 0
    return {
        "name": tool.name,
        "status": status,
        "status_label": status.replace("_", " "),
        "duration": format_ms(duration_ms) if reported else "not reported",
        "duration_reported": reported,
        "retries": tool.retry_count,
        "error": tool.error or "",
        "result": _tool_result_text(tool),
        "turn_index": tool.turn_index,
    }


def present_eval(item: Mapping[str, Any]) -> dict[str, Any]:
    passed = item.get("passed")
    state = str(item.get("state") or "")
    verdict = str(item.get("verdict") or "")
    analyzer = str(item.get("analyzer_id") or "")
    label = str(item.get("rubric_id") or item.get("judge_id") or analyzer or "eval")
    if label.startswith("pack:"):
        label = label[5:]
    if label.startswith("rubric:"):
        label = label[7:]
    shadow = bool(item.get("shadow"))
    judged = passed is True or passed is False
    return {
        **item,
        "label": label,
        "state_label": eval_state_label(state),
        "passed_label": _passed_label(passed, state=state, verdict=verdict),
        "fail_closed": judged and passed is not True and not shadow,
        "shadow": shadow,
        "score": item.get("score") if judged and not shadow else None,
    }


ROLE_LABELS: dict[str, str] = {
    Role.OWNER.value: "Owner",
    Role.ADMIN.value: "Admin",
    Role.ANALYST.value: "Analyst",
    Role.REVIEWER.value: "Reviewer",
}

SETTING_NOTICES: dict[str, str] = {
    "replayed": "Replay queued. The worker promotes a new revision.",
    "connection_deleted": "Connection deleted. Ingest URLs for it no longer authenticate.",
    "rubric_saved": "Rubric saved. Editing created a new version; historical scores keep the old one.",
    "rubric_created": "Eval saved. Predicates run on every call. English rubrics need a judge.",
    "rubric_deleted": "Rubric deleted. Historical results stay on the version they were judged under.",
    "webhook_deleted": "Outbound destination updated.",
    "deletion_accepted": "Deletion accepted. Done means completed_at is set. Warehouse copies cannot be revoked.",
    "backfill_queued": "Backfill queued. It is bounded by provider retention.",
    "seeded": "Sample calls queued. Decode still runs in the worker — refresh Calls in a few seconds.",
    "dlq_cleared": "Dead-letter queue cleared for this org. Failed raw envelopes stay; they will not retry until you replay.",
    "eval_runner_saved": "Judge runner saved. The API key is stored encrypted and is never shown again.",
    "eval_runner_deleted": "Judge runner deleted. English evals stay not judged until another runner is enabled.",
    "eval_runner_pinged": "Judge endpoint answered. Enable LLM evals with a monthly cap to spend on calls.",
    "eval_policy_saved": "Eval policy saved. LLM evals stay off until a runner and a cap greater than $0 are set.",
}

EVAL_RESULT_OPTIONS: list[tuple[str, str]] = [
    ("pass", "Eval passed"),
    ("fail", "Eval failed"),
]


def role_label(role: Role | str | None) -> str:
    if role is None:
        return ""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_LABELS.get(value, value.replace("_", " "))


def present_can(role: Role | None) -> dict[str, bool]:
    return can_map(role)


def present_health(
    *,
    plugins: Sequence[str],
    insecure_defaults: bool,
    inbox_age_seconds: float = 0.0,
    outbox_depth: int = 0,
    dlq_depth: int = 0,
    orphan_blobs: int = 0,
    deletion_backlog: int = 0,
    environment: str = "",
) -> dict[str, Any]:
    issues: list[str] = []
    if not plugins:
        issues.append("No source plugins loaded. Core ships none — install the provider you use.")
    if dlq_depth:
        if outbox_depth:
            issues.append(f"Dead-letter queue has {dlq_depth} envelope(s). Decode is failing.")
        else:
            issues.append(
                f"Dead-letter queue has {dlq_depth} envelope(s) from earlier decode failures. "
                "The worker is caught up. Settings → Status has the error."
            )
    if outbox_depth and inbox_age_seconds >= 30:
        issues.append(
            f"Outbox depth {outbox_depth}, oldest inbox {int(inbox_age_seconds)}s. "
            "The worker may be down — decode never runs on the webhook ack."
        )
    elif outbox_depth > 50:
        issues.append(f"Outbox depth {outbox_depth}. The worker is behind.")
    if deletion_backlog:
        issues.append(f"{deletion_backlog} deletion request(s) have no completed_at.")
    env = environment.lower()
    if insecure_defaults and env not in {"dev", "test", "testing"}:
        issues.append("Insecure defaults: replace every change-me and dev-key.")
    return {
        "ok": not issues,
        "show": bool(issues),
        "issues": issues,
        "summary": issues[0] if issues else "",
        "plugin_count": len(plugins),
        "plugins": list(plugins),
        "insecure_defaults": insecure_defaults,
        "inbox_age_seconds": int(inbox_age_seconds),
        "outbox_depth": int(outbox_depth),
        "dlq_depth": int(dlq_depth),
        "orphan_blobs": int(orphan_blobs),
        "deletion_backlog": int(deletion_backlog),
        "environment": environment,
    }


def present_dlq(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row.get("error") or "unknown") for row in rows)
    return {
        "count": len(rows),
        "by_error": [{"error": error, "count": count} for error, count in counts.most_common(8)],
    }


def present_plugin_rows(plugins: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plugin in plugins:
        caps = sorted(
            str(item.value if hasattr(item, "value") else item) for item in plugin.capabilities
        )
        cap_set = set(caps)
        manifest = getattr(plugin, "manifest", None)
        fidelity = getattr(plugin, "fidelity", None)
        secret_fields = sorted(getattr(manifest, "secret_fields", ()) or ())
        schema = dict(getattr(manifest, "config_schema", None) or {})
        settings_fields = []
        for name, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict):
                continue
            settings_fields.append(
                {
                    "name": str(name),
                    "label": str(name).replace("_", " "),
                    "choices": [str(item) for item in (spec.get("enum") or [])],
                }
            )
        rows.append(
            {
                "name": plugin.name,
                "display_name": getattr(plugin, "display_name", None) or plugin.name,
                "capabilities": caps,
                "capability_labels": [item.replace("_", " ") for item in caps],
                "webhook": Capability.WEBHOOK_SOURCE.value in cap_set,
                "otlp": Capability.OTLP_MAPPER.value in cap_set,
                "backfill": Capability.REST_BACKFILL.value in cap_set,
                "trust": getattr(manifest, "trust", "") or "operator_installed",
                "source_format": getattr(fidelity, "source_format", "") or "",
                "secret_fields": [
                    {"name": name, "label": name.replace("_", " ")}
                    for name in (
                        secret_fields
                        or (["hmac_secret"] if Capability.WEBHOOK_SOURCE.value in cap_set else [])
                    )
                ],
                "settings_fields": settings_fields,
            }
        )
    rows.sort(key=lambda row: str(row["display_name"]).lower())
    return rows


def present_connection_rows(configs: Sequence[Any], plugins: Sequence[Any]) -> list[dict[str, Any]]:
    backfill = {
        plugin.name
        for plugin in plugins
        if Capability.REST_BACKFILL in getattr(plugin, "capabilities", ())
    }
    labels = {plugin.name: getattr(plugin, "display_name", plugin.name) for plugin in plugins}
    rows = []
    for cfg in configs:
        provider = getattr(cfg, "provider", "")
        rows.append(
            {
                "provider": provider,
                "display_name": labels.get(provider, provider),
                "connection_id": getattr(cfg, "connection_id", ""),
                "org_id": getattr(cfg, "org_id", ""),
                "secret_fields": sorted(getattr(cfg, "secrets", {}) or {}),
                "settings": dict(getattr(cfg, "settings", None) or {}),
                "can_backfill": provider in backfill,
            }
        )
    rows.sort(key=lambda row: (str(row["display_name"]).lower(), str(row["connection_id"])))
    return rows


def empty_calls(
    *,
    signed_in: bool,
    has_connections: bool = True,
    can_connect: bool = False,
    can_seed: bool = False,
    worker_stuck: bool = False,
) -> dict[str, Any]:
    if not signed_in:
        return {
            "title": "Sign in to see live calls",
            "body": (
                "obsalt is the production call record for voice agents: latency, hangups, "
                "hallucination, tools, evals, and search. Paste an API key — locally that is "
                "dev-key."
            ),
            "cta_login": True,
            "cta_connect": False,
            "cta_seed": False,
        }
    if worker_stuck:
        return {
            "title": "Calls are waiting on the worker",
            "body": (
                "Traffic reached ingest, but decode never runs on the webhook ack. "
                "Start `obsalt worker` and watch the health strip — outbox depth should drain."
            ),
            "cta_login": False,
            "cta_connect": False,
            "cta_seed": False,
        }
    if not has_connections:
        return {
            "title": "Connect an agent",
            "body": (
                "Hosted platforms POST a signed webhook. Custom agents emit OTLP with an API key. "
                "Create a connection, then place a real call. Locally you can load vendored samples."
            ),
            "cta_login": False,
            "cta_connect": can_connect,
            "cta_seed": can_seed,
        }
    return {
        "title": "No calls in this range",
        "body": (
            "obsalt observes live traffic. It does not invent a demo. Place a real call, "
            "or widen the UTC window. If you already sent traffic, the worker may not have "
            "promoted a revision yet — decode never happens on the webhook ack."
        ),
        "cta_login": False,
        "cta_connect": False,
        "cta_seed": can_seed,
    }


def _headline(
    call: CallRevision, last_user: str, reason: str | None, *, provider_code: str | None = None
) -> str:
    parts = [hangup_label(reason, provider_code=provider_code)]
    duration = format_call_duration(call.duration_ms)
    if duration != "—":
        parts.append(f"after {duration}")
    if last_user:
        parts.append(f"last said “{snippet(last_user, 72)}”")
    return " · ".join(parts)


def _call_latency(call: CallRevision) -> tuple[str | None, float | None]:
    by_stage: dict[str, list[float]] = {}
    for item in call.stage_measurements:
        by_stage.setdefault(item.stage.value, []).append(item.value_ms)
    for stage in _PREFERRED_LATENCY:
        values = by_stage.get(stage)
        if values:
            return stage, max(values)
    if call.stage_measurements:
        item = max(call.stage_measurements, key=lambda row: row.value_ms)
        return item.stage.value, item.value_ms
    return None, None


def _slowest_stage(latency: Mapping[str, Any]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for stage, stats in (latency.get("sample_percentiles") or {}).items():
        if not isinstance(stats, dict):
            continue
        p95 = stats.get("p95")
        if p95 is None:
            continue
        if best is None or float(p95) > float(best["p95"]):
            best = {
                "stage": stage,
                "stage_label": stage_label(str(stage)),
                "p95": p95,
                "p95_text": format_ms(float(p95)),
                "n": stats.get("n") or 0,
            }
    return best


def _slowest_calls(calls: Sequence[CallRevision], limit: int = 8) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for call in calls:
        stage, value = _call_latency(call)
        if value is None:
            continue
        ranked.append(
            (
                value,
                {
                    "call_id": call.call_id,
                    "short_id": short_id(call.call_id),
                    "agent_id": agent_label(call.agent_id),
                    "source": call.source,
                    "when": format_when(call.started_at or call.created_at),
                    "duration": format_call_duration(call.duration_ms),
                    "stage_label": stage_label(stage),
                    "value_text": format_ms(value),
                    "latency": f"{stage_label(stage)} {format_ms(value)}",
                    "hangup": hangup_label_for(call),
                    "hangup_tone": hangup_tone(hangup_bucket(call)),
                    "last_user": snippet(last_text(call, Speaker.USER), 72),
                    "last_agent": snippet(last_text(call, Speaker.AGENT), 72),
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]["call_id"]))
    return [row for _value, row in ranked[:limit]]


def _fleet_questions(
    *,
    n: int,
    top_hangup: str,
    top_hangup_count: int,
    slowest: Mapping[str, Any] | None,
    tool_failures: int,
    hallucinations: int,
    hallucination_candidates: int = 0,
    scanned: int = 0,
    eval_failed: int,
    eval_passed: int = 0,
    eval_completed: int = 0,
    eligible: int = 0,
    range_qs: str = "",
    tool_invocations: int = 0,
) -> list[dict[str, str]]:
    if n == 0:
        return []
    suffix = f"?{range_qs}" if range_qs else ""
    if scanned <= 0:
        invent = "No agent transcript to scan"
    else:
        invent = f"{hallucinations} confirmed on {scanned} scanned"
    not_judged = max(0, (eligible or n) - eval_completed)
    bar = (
        f"{eval_failed} fail · {eval_passed} pass · {not_judged} not judged"
        if eval_completed or eval_failed or eval_passed
        else f"0 judged · {eligible or n} not judged"
    )
    hangup_value = f"{top_hangup} · {top_hangup_count} of {n}" if top_hangup else f"0 of {n}"
    questions = [
        {
            "href": f"/v1/ui/hangups{suffix}",
            "question": "Why did we lose callers?",
            "value": hangup_value,
            "kind": "stat",
            "tone": "loss" if top_hangup_count and "hung up" in top_hangup.lower() else "",
        },
        {
            "href": f"/v1/ui/latency{suffix}",
            "question": "Where did time go?",
            "value": (
                f"{slowest['stage_label']} p95 {slowest['p95_text']}" if slowest else "No samples"
            ),
            "kind": "stat",
            "tone": "" if slowest else "warn",
        },
        {
            "href": f"/v1/ui/quality{suffix}",
            "question": "Did the agent invent facts?",
            "value": invent,
            "kind": "stat",
            "tone": "warn" if scanned <= 0 else "loss" if hallucinations else "ok",
        },
        {
            "href": f"/v1/ui/quality{suffix}",
            "question": "Did we meet our bar?",
            "value": bar,
            "kind": "stat",
            "tone": "loss" if eval_failed else "warn" if not eval_completed else "ok",
        },
        {
            "href": f"/v1/ui/latency{suffix}",
            "question": "Which tools failed?",
            "value": "No tools in this range" if tool_invocations == 0 else str(tool_failures),
            "kind": "stat",
            "tone": ("warn" if tool_invocations == 0 else "loss" if tool_failures else "ok"),
        },
        {
            "href": f"/v1/ui/search{suffix}",
            "question": "Find a conversation",
            "value": "",
            "kind": "door",
            "tone": "",
        },
    ]
    return questions


def _next_action(call: CallRevision, flags: Sequence[Mapping[str, Any]]) -> str:
    kinds = {str(item.get("kind") or "") for item in flags}
    reason = hangup_bucket(call)
    confirmed = [
        item
        for item in flags
        if str(item.get("verdict") or "") == "contradicted"
        and str(item.get("model") or "").startswith("detector")
    ]
    if confirmed or "phantom_tool_success" in kinds:
        return "A detector flag settled on this call. Compare the claim to the tool result and grounding."
    if reason == ENDING_NOT_REPORTED:
        return (
            "The source did not send an ending. Custom agents emit "
            "obsalt.hangup.reason or call VoiceCall.end."
        )
    if reason == ENDING_UNROOTED:
        return "This trace never rooted. We did not invent an ending."
    return ""


def _interval_geometry(intervals: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """Pixel-free percentages from real start/end only. Empty when clocks are missing."""
    stamped: list[tuple[datetime, datetime]] = []
    for item in intervals:
        start = _parse_iso(item.get("started_at"))
        end = _parse_iso(item.get("ended_at"))
        if start is None or end is None or end <= start:
            stamped.append((datetime.min, datetime.min))
            continue
        stamped.append((start, end))
    valid = [(start, end) for start, end in stamped if start != datetime.min]
    if not valid:
        return [{"left_pct": 0.0, "width_pct": 0.0} for _ in intervals]
    origin = min(start for start, _end in valid)
    close = max(end for _start, end in valid)
    span = (close - origin).total_seconds()
    if span <= 0:
        return [{"left_pct": 0.0, "width_pct": 0.0} for _ in intervals]
    geometry: list[dict[str, float]] = []
    for start, end in stamped:
        if start == datetime.min:
            geometry.append({"left_pct": 0.0, "width_pct": 0.0})
            continue
        left = (start - origin).total_seconds() / span * 100.0
        width = (end - start).total_seconds() / span * 100.0
        geometry.append(
            {
                "left_pct": round(max(0.0, min(left, 100.0)), 2),
                "width_pct": round(max(0.5, min(width, 100.0)), 2),
            }
        )
    return geometry


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _turn_duration_ms(turn: Turn) -> float | None:
    if turn.started_at is None or turn.ended_at is None:
        return None
    ms = (turn.ended_at - turn.started_at).total_seconds() * 1000.0
    if ms < 0:
        return None
    return ms


def _match_snippet(call: CallRevision, query: str) -> str:
    tokens = [token for token in query.lower().split() if token]
    for turn in call.turns:
        lowered = turn.text.lower()
        if tokens and any(token in lowered for token in tokens):
            return snippet(turn.text, 140)
    return snippet(last_text(call, Speaker.USER), 140)


def _present_measurement(item: StageMeasurement) -> dict[str, Any]:
    return {
        "stage": item.stage.value,
        "stage_label": stage_label(item.stage.value),
        "metric": item.metric.value,
        "metric_label": metric_label(item.metric.value),
        "value_text": format_ms(item.value_ms),
        "value_ms": item.value_ms,
        "provenance": item.provenance.value,
        "placement": item.placement.value,
        "source_path": item.source_path or "",
        "derivation": item.derivation or "",
    }


def _pct(part: int, whole: int) -> int | None:
    if whole <= 0:
        return None
    return int(round(100.0 * part / whole))


def _rank_duration_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One bar per stage+metric, slowest first. Zeros omitted. Not a waterfall."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for item in rows:
        value = item.get("value_ms")
        if not value:
            continue
        key = (str(item.get("stage") or ""), str(item.get("metric") or "duration"))
        prev = best.get(key)
        if prev is None or float(value) > float(prev.get("value_ms") or 0):
            best[key] = dict(item)
    ranked = sorted(
        best.values(),
        key=lambda row: (-float(row.get("value_ms") or 0), str(row.get("stage") or "")),
    )
    ceiling = float(ranked[0]["value_ms"]) if ranked else 0.0
    for item in ranked:
        item["bar_pct"] = _bar_pct(item.get("value_ms"), ceiling)
    return ranked


def _bar_pct(value: float | None, ceiling: float) -> int:
    if not value or ceiling <= 0:
        return 0
    return max(4, min(100, int(round(100.0 * float(value) / ceiling))))


_COVERAGE_STATUS = {
    "present": "present",
    "absent": "not sent",
    "unsupported": "this source cannot send it",
    "redacted": "redacted",
    "decode_failed": "decode failed",
}


def _coverage_strip(call: CallRevision) -> list[dict[str, str]]:
    by_signal = {item.signal.value: item for item in call.coverage}

    def _row(signal: str, title: str) -> dict[str, str]:
        item = by_signal.get(signal)
        status = item.status.value if item else "absent"
        return {
            "signal": title,
            "status": status,
            "label": _COVERAGE_STATUS.get(status, status.replace("_", " ")),
        }

    grounding_rows = [item for key, item in by_signal.items() if key.startswith("grounding_")]
    if any(item.status.value == "present" for item in grounding_rows):
        grounding = {"signal": "Grounding", "status": "present", "label": "present"}
    elif grounding_rows and all(item.status.value == "unsupported" for item in grounding_rows):
        grounding = {
            "signal": "Grounding",
            "status": "unsupported",
            "label": "this source cannot send it",
        }
    else:
        grounding = {"signal": "Grounding", "status": "absent", "label": "empty"}
    rows = [
        _row("transcript", "Transcript"),
        _row("hangup", "Ending"),
        grounding,
        _row("stage_interval", "Stage clocks"),
    ]
    recording = by_signal.get("recording")
    if recording is not None and recording.status.value == "present":
        rows.append(_row("recording", "Recording"))
    cost = by_signal.get("cost")
    if cost is not None and cost.status.value == "present":
        rows.append(_row("cost", "Cost"))
    return rows


def eval_state_label(state: str | None) -> str:
    if not state:
        return EVAL_STATE_LABELS["pending"]
    return EVAL_STATE_LABELS.get(state, state.replace("_", " "))


def _passed_label(passed: Any, *, state: str | None = None, verdict: str | None = None) -> str:
    if verdict in {"not_applicable", "evidence_missing", "maybe", "not_judged"}:
        return verdict.replace("_", " ")
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    if state == "budget_blocked":
        return "budget blocked"
    if state == "sampled_out":
        return "skipped by sample"
    if state == "failed":
        return "judge failed"
    return "not judged"


def hangup_option_groups() -> list[dict[str, Any]]:
    loss: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    reported = {ENDING_NOT_REPORTED, ENDING_UNROOTED, HangupReason.UNKNOWN.value}
    for reason in HangupReason:
        if reason.value in reported:
            continue
        item = (reason.value, hangup_label(reason.value))
        if reason.value in _LOST_HANGUPS:
            loss.append(item)
        else:
            other.append(item)
    return [
        {"label": "Lost callers", "options": loss},
        {"label": "Other endings", "options": other},
        {
            "label": "Not reported",
            "options": [
                (ENDING_NOT_REPORTED, hangup_label(ENDING_NOT_REPORTED)),
                (ENDING_UNROOTED, hangup_label(ENDING_UNROOTED)),
                (HangupReason.UNKNOWN.value, hangup_label(HangupReason.UNKNOWN.value)),
            ],
        },
    ]
