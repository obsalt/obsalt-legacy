"""Deterministic claim pre-filter (Tier 1). LLM entailment is Tier 2."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from obsalt.analysis.tool_values import (
    ToolScalar,
    content_scalars,
    has_month_day,
    id_like_for_spoken,
    money_equal,
    month_day_span,
    normalize_id,
    parse_datetime_span,
    parse_money,
    tool_arg_scalars,
    tool_result_text,
    tool_scalar_values,
)
from obsalt.domain.enums import (
    ClaimSeverity,
    ClaimVerdict,
    EvidenceNeed,
    GroundingKind,
    HallucinationKind,
    ToolStatus,
)
from obsalt.domain.models import CallRevision, ToolInvocation

PAGEABLE_SEVERITIES = frozenset({ClaimSeverity.CRITICAL.value, ClaimSeverity.HIGH.value})

DETECTOR_VERSION = "detector/3"
HALLUCINATION_ANALYZER_VERSION = "3"

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_MONEY_CURRENCY_RE = re.compile(
    r"\$\s?\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?"
    r"|\b\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?\s?(?:dollars|usd)\b",
    re.I,
)
_DECIMAL_RE = re.compile(r"\b\d{1,6}\.\d{1,2}\b")
_MONEY_WORD_RE = re.compile(r"\b(?:total|price|cost|charge|refund|owe|amount|paid|invoice)\b", re.I)
_ID_RE = re.compile(
    r"\b(?:order|confirmation|booking|ticket|invoice|reference|case)\s*"
    r"(?:number|#|id)?\s*[:#]?\s*([A-Z0-9][-A-Z0-9]{4,})\b",
    re.I,
)
_USER_STATED_ID_RE = re.compile(
    r"\b(?:my|the)\s+(?:order|confirmation|booking|ticket|invoice|reference|case)\s*"
    r"(?:number|#|id)?\s*(?:is|:|#)?\s*([A-Z0-9][-A-Z0-9]{4,})\b",
    re.I,
)
_ACTION_RE = re.compile(
    r"\bI(?:'ve| have)\s+(?:just\s+)?"
    r"(booked|scheduled|cancelled|canceled|sent|processed|refunded|updated|confirmed|placed|created)\b",
    re.I,
)
_FAILURE_MARKERS = ("error", "not_found", "failed", "timeout", "timed out")
_LIVE_GROUNDING = frozenset(
    {GroundingKind.KNOWLEDGE, GroundingKind.TOOL_RESULT, GroundingKind.USER_TEXT}
)
_FAILURE_CLAIM_RE = re.compile(
    r"\b(?:can'?t|cannot|unable to|couldn'?t)\s+(?:find|locate|pull up|retrieve|access)\b"
    r"|\bno\s+(?:record|records|results?|orders?|bookings?|account)\b"
    r"|\b(?:system|service|site|app|payment\s+system)\s+is\s+(?:down|offline|unavailable)\b",
    re.I,
)
_DATE_CONTEXT_RE = re.compile(
    r"(?:\bon\b|\bat\b|\bfor\b|\bby\b|\bthis\b|\bis\b)\s+"
    r"("
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}"
    r"(?:st|nd|rd|th)?(?:,?\s*20\d{2})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)"
    r"|\d{1,2}\s*(?:am|pm)"
    r")",
    re.I,
)
_TIME_SPAN_RE = re.compile(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", re.I)
_COUNT_CANDIDATE_RE = re.compile(
    r"\b(?:i\s+see|we\s+(?:have|found|show)|you\s+(?:have|got)|there\s+are|"
    r"it\s+(?:shows|says)|showing|found)\s+(\d{1,4})\b",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\b\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_ARGS_PLACEHOLDER_VALUES = frozenset({"", "unknown", "null", "none", "na", "n/a", "tbd"})
_REDACTED_RE = re.compile(r"<[^>]*>$")

_KIND_NEED = {
    HallucinationKind.PRICE_CLAIM: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.FABRICATED_ID: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.UNGROUNDED_FACT: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.POLICY_CLAIM: EvidenceNeed.POLICY_TEXT,
    HallucinationKind.COMMITMENT: EvidenceNeed.TOOL_RESULT,
    HallucinationKind.PHANTOM_TOOL_SUCCESS: EvidenceNeed.TOOL_RESULT,
    HallucinationKind.PHANTOM_TOOL_FAILURE: EvidenceNeed.TOOL_RESULT,
    HallucinationKind.PRIVATE_KNOWLEDGE: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.DATE_TIME_CLAIM: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.COUNT_CLAIM: EvidenceNeed.GROUNDING_SPAN,
    HallucinationKind.ARGS_MISMATCH: EvidenceNeed.TOOL_RESULT,
}

_SETTLED = frozenset(
    {
        ClaimVerdict.GROUNDED.value,
        ClaimVerdict.CONTRADICTED.value,
        ClaimVerdict.EVIDENCE_MISSING.value,
    }
)


def extract_candidate_claims(call: CallRevision) -> list[dict[str, object]]:
    """Spans only. No verb map, no blob membership."""
    flags: list[dict[str, object]] = []
    for turn in call.agent_turns():
        for sentence in [s.strip() for s in _SENTENCE_RE.split(turn.text) if s.strip()]:
            for span in _money_spans(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.PRICE_CLAIM,
                        sentence,
                        turn.index,
                        span,
                    )
                )
            for span in _id_spans(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.FABRICATED_ID,
                        sentence,
                        turn.index,
                        span,
                    )
                )
            for span in _datetime_spans(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.DATE_TIME_CLAIM,
                        sentence,
                        turn.index,
                        span,
                    )
                )
            for span in _count_spans(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.COUNT_CLAIM,
                        sentence,
                        turn.index,
                        span,
                    )
                )
            for span in _pii_spans(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.PRIVATE_KNOWLEDGE,
                        sentence,
                        turn.index,
                        span,
                    )
                )
            if _FAILURE_CLAIM_RE.search(sentence):
                flags.append(
                    _flag(
                        HallucinationKind.PHANTOM_TOOL_FAILURE,
                        sentence,
                        turn.index,
                        sentence,
                    )
                )
            action = _ACTION_RE.search(sentence)
            if action:
                flags.append(
                    _flag(
                        HallucinationKind.COMMITMENT,
                        sentence,
                        turn.index,
                        action.group(0),
                    )
                )
    return flags


def detect_claims(call: CallRevision) -> list[dict[str, object]]:
    """Structured exact. Always a verdict. Unbound success-claims are needs_review candidates."""
    money_scalars, id_scalars, datetime_scalars, count_scalars, citations = _evidence_pack(call)
    tools_present = bool(call.tools)
    live = _has_live_evidence(call)
    out: list[dict[str, object]] = []
    for claim in extract_candidate_claims(call):
        item = dict(claim)
        kind = str(item.get("kind") or "")
        evidence = item.get("evidence")
        token = str(evidence[0]) if isinstance(evidence, list) and evidence else ""
        turn_index = item.get("turn_index")
        if kind == HallucinationKind.PRICE_CLAIM.value:
            _detect_money(item, token, money_scalars, citations, tools_present, live)
        elif kind == HallucinationKind.FABRICATED_ID.value:
            _detect_id(item, token, id_scalars, citations, tools_present, live)
        elif kind == HallucinationKind.DATE_TIME_CLAIM.value:
            _detect_datetime(item, token, datetime_scalars, citations, live, call, turn_index)
        elif kind == HallucinationKind.COUNT_CLAIM.value:
            _detect_count(item, token, count_scalars, citations, tools_present, live)
        elif kind == HallucinationKind.PRIVATE_KNOWLEDGE.value:
            _detect_private_knowledge(item, token, call, tools_present)
        elif kind == HallucinationKind.PHANTOM_TOOL_FAILURE.value:
            if _detect_failure_claim(item, call, turn_index):
                continue
        elif kind == HallucinationKind.COMMITMENT.value:
            bound = bind_success_claim(call, turn_index if isinstance(turn_index, int) else None)
            if bound is None:
                if not tools_present:
                    _settle(item, ClaimVerdict.EVIDENCE_MISSING)
                else:
                    _settle(item, ClaimVerdict.NEEDS_REVIEW, settled=False)
            else:
                tool, rule = bound
                item["binding"] = {"tool_id": tool.id, "rule": rule}
                if not tool_effectively_failed(tool):
                    continue
                item["kind"] = HallucinationKind.PHANTOM_TOOL_SUCCESS.value
                item["evidence_need"] = EvidenceNeed.TOOL_RESULT.value
                _settle(item, ClaimVerdict.CONTRADICTED)
                item["evidence_spans"] = [_tool_evidence(tool)]
        item["severity"] = assign_claim_severity(item)
        out.append(item)
        _observe_claim(item)
    out.extend(_args_mismatch_claims(call))
    return out


def bind_success_claim(
    call: CallRevision, turn_index: int | None, *, allow_unique: bool = True
) -> tuple[ToolInvocation, str] | None:
    """None-safe bind. First unique hit wins."""
    if turn_index is not None:
        indexed = [tool for tool in call.tools if isinstance(tool.turn_index, int)]
        equal = [tool for tool in indexed if tool.turn_index == turn_index]
        before = [
            tool for tool in indexed if tool.turn_index is not None and tool.turn_index < turn_index
        ]
        latest_before = []
        if before:
            max_before = max(tool.turn_index for tool in before if tool.turn_index is not None)
            latest_before = [tool for tool in before if tool.turn_index == max_before]
        temporal_set: list[ToolInvocation] = []
        for tool in equal + latest_before:
            if tool not in temporal_set:
                temporal_set.append(tool)
        temporal = _unique(temporal_set)
        if temporal is not None:
            rule = "equal_turn_index" if temporal in equal else "preceding_turn_index"
            return temporal, rule
    agent_started = None
    if turn_index is not None:
        for turn in call.turns:
            if turn.index == turn_index:
                agent_started = turn.started_at
                break
    if agent_started is not None:
        clocked = [
            tool
            for tool in call.tools
            if tool.started_at is not None and tool.started_at <= agent_started
        ]
        if clocked:
            latest = max(clocked, key=lambda tool: tool.started_at or agent_started)
            if sum(1 for tool in clocked if tool.started_at == latest.started_at) == 1:
                return latest, "preceding_clock"
    if allow_unique and len(call.tools) == 1:
        return call.tools[0], "unique_on_call"
    return None


def hallucination_claim_list(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Concat confirmed + candidates. Do not OR — mixed calls keep both."""
    if not payload:
        return []
    out: list[dict[str, Any]] = []
    for item in list(payload.get("claims") or []) + list(payload.get("candidates") or []):
        if isinstance(item, dict):
            out.append(item)
    return out


def split_detector_claims(
    claims: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settled: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for raw in claims:
        item = dict(raw)
        verdict = str(item.get("verdict") or "")
        if verdict == ClaimVerdict.NEEDS_REVIEW.value:
            candidates.append(item)
        elif verdict in _SETTLED:
            settled.append(item)
        else:
            candidates.append(item)
    return settled, candidates


def detector_payload(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    settled, candidates = split_detector_claims(claims)
    return {
        "claims": settled,
        "candidates": candidates,
        "model": DETECTOR_VERSION,
        "selection": "detector",
        "passed": not any(str(item.get("verdict") or "") == "contradicted" for item in settled),
    }


def assign_claim_severity(claim: dict[str, object]) -> str:
    """Harm first. Missing evidence is a review hole, not a page."""
    verdict = str(claim.get("verdict") or "")
    kind = str(claim.get("kind") or "")
    if verdict in {
        ClaimVerdict.EVIDENCE_MISSING.value,
        ClaimVerdict.NEEDS_REVIEW.value,
    } or (claim.get("needs_llm") and verdict not in {ClaimVerdict.CONTRADICTED.value}):
        return ClaimSeverity.NEEDS_REVIEW.value
    if verdict == ClaimVerdict.GROUNDED.value:
        return ClaimSeverity.LOW.value
    if kind in {
        HallucinationKind.PRICE_CLAIM.value,
        HallucinationKind.FABRICATED_ID.value,
        HallucinationKind.DATE_TIME_CLAIM.value,
        HallucinationKind.PRIVATE_KNOWLEDGE.value,
    }:
        return ClaimSeverity.CRITICAL.value
    if kind in {
        HallucinationKind.PHANTOM_TOOL_SUCCESS.value,
        HallucinationKind.PHANTOM_TOOL_FAILURE.value,
        HallucinationKind.COMMITMENT.value,
        HallucinationKind.ARGS_MISMATCH.value,
        HallucinationKind.COUNT_CLAIM.value,
    }:
        return ClaimSeverity.HIGH.value
    if kind == HallucinationKind.POLICY_CLAIM.value:
        return ClaimSeverity.MEDIUM.value
    return ClaimSeverity.MEDIUM.value


def grounding_corpus(call: CallRevision) -> list[str]:
    """Prompt, knowledge, tool results/errors, and caller statements (§9.4)."""
    parts = [turn.text for turn in call.user_turns() if turn.text]
    parts.extend(item.content for item in call.grounding if item.content)
    for tool in call.tools:
        parts.append(_tool_body(tool))
    return parts


def tool_effectively_failed(tool: ToolInvocation) -> bool:
    """Status plus result body. `success` + `{\"error\":\"not_found\"}` is a failure."""
    if tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
        return True
    body = _tool_body(tool).lower()
    return any(marker in body for marker in _FAILURE_MARKERS)


def tool_effectively_succeeded(tool: ToolInvocation) -> bool:
    if tool_effectively_failed(tool):
        return False
    return tool.status is ToolStatus.SUCCESS


def claim_is_settled(claim: dict[str, object]) -> bool:
    return str(claim.get("verdict") or "") in _SETTLED and not bool(claim.get("needs_llm"))


def _detect_datetime(
    item: dict[str, object],
    token: str,
    datetime_scalars: list[Any],
    citations: list[dict[str, str]],
    live: bool,
    call: CallRevision,
    turn_index: object,
) -> None:
    anchor = call.started_at
    if anchor is None and isinstance(turn_index, int):
        anchor = next(
            (
                turn.started_at
                for turn in call.turns
                if turn.index == turn_index and turn.started_at
            ),
            None,
        )
    parsed: datetime | None = None
    if anchor is not None and live:
        parsed = parse_datetime_span(token, anchor=anchor)
        if parsed is not None and not has_month_day(token):
            # A bare time ("3pm") inherits the sentence's date when one is stated.
            span_text = str(item.get("span_text") or "")
            month_day = month_day_span(span_text)
            if month_day is not None:
                combined = parse_datetime_span(f"{month_day} {token}", anchor=anchor)
                if combined is not None:
                    parsed = combined
    if parsed is None:
        _settle(item, ClaimVerdict.EVIDENCE_MISSING)
        return
    has_time = bool(_TIME_SPAN_RE.search(token))
    # Date-less time claims ("we close at 6pm") are about policy, not a record.
    can_contradict = (
        has_month_day(token) or month_day_span(str(item.get("span_text") or "")) is not None
    )
    hit = next(
        (
            scalar
            for scalar in datetime_scalars
            if _datetime_match(scalar.as_datetime, parsed, has_time)
        ),
        None,
    )
    if hit is not None:
        _settle(item, ClaimVerdict.GROUNDED)
        item["evidence_spans"] = [{"source": "grounding", "path": hit.path, "text": str(hit.value)}]
        return
    # Without datetime evidence there is nothing to contradict — never fail on absence.
    if datetime_scalars and can_contradict:
        _settle(item, ClaimVerdict.CONTRADICTED)
        item["evidence_spans"] = citations
        return
    _settle(item, ClaimVerdict.EVIDENCE_MISSING)


def _datetime_match(source: datetime | None, claimed: datetime, has_time: bool) -> bool:
    if source is None:
        return False
    left = source if source.tzinfo else source.replace(tzinfo=UTC)
    right = claimed if claimed.tzinfo else claimed.replace(tzinfo=UTC)
    if left.date() != right.date():
        return False
    if not has_time:
        return True
    if (left.hour, left.minute) == (0, 0):
        return True  # date-only source cannot contradict a stated time
    return (left.hour, left.minute) == (right.hour, right.minute)


def _detect_count(
    item: dict[str, object],
    token: str,
    count_scalars: list[Any],
    citations: list[dict[str, str]],
    tools_present: bool,
    live: bool,
) -> None:
    claimed = int(token) if token.isdigit() else None
    if claimed is None or not live:
        _settle(item, ClaimVerdict.EVIDENCE_MISSING)
        return
    hit = next((scalar for scalar in count_scalars if scalar.as_count == claimed), None)
    if hit is not None:
        _settle(item, ClaimVerdict.GROUNDED)
        item["evidence_spans"] = [{"source": "grounding", "path": hit.path, "text": str(hit.value)}]
        return
    if count_scalars:
        _settle(item, ClaimVerdict.CONTRADICTED)
        item["evidence_spans"] = citations
        return
    _settle(item, ClaimVerdict.EVIDENCE_MISSING)


def _detect_private_knowledge(
    item: dict[str, object],
    token: str,
    call: CallRevision,
    tools_present: bool,
) -> None:
    sources = _private_sources(call)
    if _contains_private_value(sources, token):
        _settle(item, ClaimVerdict.GROUNDED)
        return
    if _near_miss_private_value(sources, token):
        _settle(item, ClaimVerdict.CONTRADICTED)
        return
    # No source anywhere: ungrounded PII is a candidate, not a fail.
    _settle(item, ClaimVerdict.NEEDS_REVIEW, settled=False)
    if not tools_present:
        item["evidence_need"] = EvidenceNeed.GROUNDING_SPAN.value


def _detect_failure_claim(item: dict[str, object], call: CallRevision, turn_index: object) -> bool:
    """True when the item must be dropped (honest failure, nothing to flag).

    Failure claims never bind by single-tool uniqueness: "the system is down"
    is about the service, not whichever tool happened to run.
    """
    bound = bind_success_claim(
        call, turn_index if isinstance(turn_index, int) else None, allow_unique=False
    )
    if bound is None:
        if call.tools:
            _settle(item, ClaimVerdict.NEEDS_REVIEW, settled=False)
        else:
            _settle(item, ClaimVerdict.EVIDENCE_MISSING)
        return False
    tool, rule = bound
    item["binding"] = {"tool_id": tool.id, "rule": rule}
    if tool_effectively_succeeded(tool) and tool_result_text(tool):
        _settle(item, ClaimVerdict.CONTRADICTED)
        item["evidence_spans"] = [_tool_evidence(tool)]
        return False
    return True


def _private_sources(call: CallRevision) -> list[str]:
    """Caller turns, tool bodies, live grounding. Agent turns are never self-grounding."""
    sources = [turn.text for turn in call.user_turns() if turn.text]
    sources.extend(_tool_body(tool) for tool in call.tools)
    sources.extend(
        ref.content for ref in call.grounding if ref.kind in _LIVE_GROUNDING and ref.content
    )
    return sources


def _contains_private_value(sources: Sequence[str], token: str) -> bool:
    if "@" in token:
        needle = token.lower()
        return any(needle in source.lower() for source in sources)
    digits = normalize_id(token)
    if not digits:
        return False
    return any(digits in _digit_blob(source) for source in sources)


def _near_miss_private_value(sources: Sequence[str], token: str) -> bool:
    if "@" in token:
        local, _, domain = token.lower().partition("@")
        for source in sources:
            for candidate in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", source, re.I):
                c_local, _, c_domain = candidate.lower().partition("@")
                if c_domain != domain:
                    continue
                if SequenceMatcher(None, local, c_local).ratio() >= 0.8:
                    return True
        return False
    digits = normalize_id(token)
    if len(digits) < 7:
        return False
    for source in sources:
        blob = _digit_blob(source)
        for start in range(0, max(0, len(blob) - len(digits) + 1)):
            window = blob[start : start + len(digits)]
            if window == digits:
                continue
            if window[:3] == digits[:3] or window[-4:] == digits[-4:]:
                return True
    return False


def _digit_blob(source: str) -> str:
    return re.sub(r"\D", "", source)


def _user_ids_before(call: CallRevision, turn_index: int) -> list[Any]:
    """Caller-named ids. Requires an explicit digit-bearing id, never prose."""
    stated: list[Any] = []
    for turn in call.user_turns():
        if not isinstance(turn.index, int) or turn.index >= turn_index or not turn.text:
            continue
        for match in _USER_STATED_ID_RE.finditer(turn.text):
            span = match.group(1)
            if not any(char.isdigit() for char in span):
                continue
            stated.append(
                ToolScalar(
                    path="$text",
                    value=span,
                    as_id=normalize_id(span),
                    id_like=True,
                )
            )
    return stated


def _args_mismatch_claims(call: CallRevision) -> list[dict[str, object]]:
    """Caller named one id; the agent sent a placeholder for it. Top-level args only."""
    out: list[dict[str, object]] = []
    for tool in call.tools:
        if not isinstance(tool.turn_index, int):
            continue
        stated = _user_ids_before(call, tool.turn_index)
        if len({scalar.as_id for scalar in stated}) != 1:
            continue
        user_scalar = stated[0]
        for scalar in tool_arg_scalars(tool):
            value = str(scalar.value)
            if (
                not scalar.id_like
                or _REDACTED_RE.search(value)
                or normalize_id(value) not in _ARGS_PLACEHOLDER_VALUES
            ):
                continue
            item = _flag(
                HallucinationKind.ARGS_MISMATCH,
                f"{tool.name} sent {value!r} for {scalar.path}",
                tool.turn_index,
                value,
            )
            _settle(item, ClaimVerdict.CONTRADICTED)
            item["evidence_spans"] = [
                {
                    "source": "tool",
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "path": scalar.path,
                    "text": value,
                },
                {"source": "grounding", "path": "$text", "text": str(user_scalar.value)},
            ]
            item["severity"] = assign_claim_severity(item)
            out.append(item)
            _observe_claim(item)
    return out


def _detect_money(
    item: dict[str, object],
    token: str,
    money_scalars: list[Any],
    citations: list[dict[str, str]],
    tools_present: bool,
    live: bool,
) -> None:
    amount = parse_money(token)
    if amount is None or not live:
        _settle(item, ClaimVerdict.EVIDENCE_MISSING)
        return
    hit = next(
        (
            scalar
            for scalar in money_scalars
            if scalar.money_like
            and scalar.as_decimal is not None
            and money_equal(amount, scalar.as_decimal)
        ),
        None,
    )
    if hit is not None:
        _settle(item, ClaimVerdict.GROUNDED)
        item["evidence_spans"] = [{"source": "grounding", "path": hit.path, "text": str(hit.value)}]
        return
    if tools_present:
        _settle(item, ClaimVerdict.CONTRADICTED)
        item["evidence_spans"] = citations
        return
    _settle(item, ClaimVerdict.EVIDENCE_MISSING)


def _detect_id(
    item: dict[str, object],
    token: str,
    id_scalars: list[Any],
    citations: list[dict[str, str]],
    tools_present: bool,
    live: bool,
) -> None:
    needle = normalize_id(token)
    if not needle or not live:
        _settle(item, ClaimVerdict.EVIDENCE_MISSING)
        return
    hit = next(
        (
            scalar
            for scalar in id_scalars
            if scalar.as_id == needle and id_like_for_spoken(scalar, token)
        ),
        None,
    )
    if hit is not None:
        _settle(item, ClaimVerdict.GROUNDED)
        item["evidence_spans"] = [{"source": "grounding", "path": hit.path, "text": str(hit.value)}]
        return
    if tools_present:
        _settle(item, ClaimVerdict.CONTRADICTED)
        item["evidence_spans"] = citations
        return
    _settle(item, ClaimVerdict.EVIDENCE_MISSING)


def _evidence_pack(
    call: CallRevision,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[dict[str, str]]]:
    money: list[Any] = []
    ids: list[Any] = []
    datetimes: list[Any] = []
    counts: list[Any] = []
    citations: list[dict[str, str]] = []
    for tool in call.tools:
        scalars = tool_scalar_values(tool)
        money.extend(scalar for scalar in scalars if scalar.money_like)
        ids.extend(scalar for scalar in scalars if scalar.as_id)
        datetimes.extend(scalar for scalar in scalars if scalar.as_datetime)
        counts.extend(scalar for scalar in scalars if scalar.as_count is not None)
        body = tool_result_text(tool)
        citations.append(_tool_evidence(tool, body=body))
    for ref in call.grounding:
        if ref.kind not in _LIVE_GROUNDING or not ref.content:
            continue
        parsed = content_scalars(ref.content)
        if parsed is not None:
            money.extend(scalar for scalar in parsed if scalar.money_like)
            ids.extend(scalar for scalar in parsed if scalar.as_id)
            datetimes.extend(scalar for scalar in parsed if scalar.as_datetime)
            counts.extend(scalar for scalar in parsed if scalar.as_count is not None)
        else:
            money.extend(_text_money_scalars(ref.content))
            ids.extend(_text_id_scalars(ref.content))
    for turn in call.user_turns():
        if turn.text:
            money.extend(_text_money_scalars(turn.text))
            ids.extend(_text_id_scalars(turn.text))
            datetimes.extend(_text_datetime_scalars(turn.text, turn.started_at or call.started_at))
            counts.extend(_text_count_scalars(turn.text))
    return money, ids, datetimes, counts, citations


def _text_money_scalars(text: str) -> list[Any]:
    from obsalt.analysis.tool_values import ToolScalar

    found: list[Any] = []
    for span in _money_spans(text):
        amount = parse_money(span)
        if amount is None:
            continue
        found.append(
            ToolScalar(
                path="$text",
                value=span,
                as_decimal=amount,
                money_like=True,
            )
        )
    return found


def _text_id_scalars(text: str) -> list[Any]:
    from obsalt.analysis.tool_values import ToolScalar

    found: list[Any] = []
    for span in _id_spans(text):
        found.append(
            ToolScalar(
                path="$text",
                value=span,
                as_id=normalize_id(span),
                id_like=True,
            )
        )
    return found


def _text_datetime_scalars(text: str, anchor: datetime | None) -> list[Any]:
    if anchor is None:
        return []
    found: list[Any] = []
    for span in _datetime_spans(text):
        parsed = parse_datetime_span(span, anchor=anchor)
        if parsed is None:
            continue
        found.append(
            ToolScalar(
                path="$text",
                value=span,
                as_datetime=parsed,
            )
        )
    return found


def _text_count_scalars(text: str) -> list[Any]:
    found: list[Any] = []
    for span in _count_spans(text):
        if not span.isdigit():
            continue
        found.append(
            ToolScalar(
                path="$text",
                value=span,
                as_decimal=Decimal(int(span)).quantize(Decimal("0.01")),
                as_count=int(span),
                count_like=True,
            )
        )
    return found


def _has_live_evidence(call: CallRevision) -> bool:
    if call.tools:
        return True
    if any(ref.kind in _LIVE_GROUNDING and ref.content for ref in call.grounding):
        return True
    return any(turn.text for turn in call.user_turns())


def _money_spans(sentence: str) -> list[str]:
    found: list[str] = []
    occupied: list[tuple[int, int]] = []
    for match in _MONEY_CURRENCY_RE.finditer(sentence):
        found.append(match.group(0).strip())
        occupied.append(match.span())
    if _MONEY_WORD_RE.search(sentence):
        for match in _DECIMAL_RE.finditer(sentence):
            if any(
                start <= match.start() < end or start < match.end() <= end
                for start, end in occupied
            ):
                continue
            found.append(match.group(0))
    return found


def _id_spans(sentence: str) -> list[str]:
    return [match.group(1) for match in _ID_RE.finditer(sentence)]


def _datetime_spans(sentence: str) -> list[str]:
    return [match.group(1).strip() for match in _DATE_CONTEXT_RE.finditer(sentence)]


def _count_spans(sentence: str) -> list[str]:
    return [match.group(1) for match in _COUNT_CANDIDATE_RE.finditer(sentence)]


def _pii_spans(sentence: str) -> list[str]:
    return [match.group(0) for match in _EMAIL_RE.finditer(sentence)] + [
        match.group(0) for match in _PHONE_RE.finditer(sentence)
    ]


def _flag(kind: HallucinationKind, span: str, turn_index: int, evidence: str) -> dict[str, object]:
    return {
        "kind": kind.value,
        "span_text": span,
        "turn_index": turn_index,
        "agent_span": evidence,
        "evidence": [evidence],
        "evidence_need": _KIND_NEED[kind].value,
        "needs_llm": False,
    }


def _settle(
    item: dict[str, object],
    verdict: ClaimVerdict,
    *,
    settled: bool = True,
) -> None:
    item["verdict"] = verdict.value
    item["needs_llm"] = False
    item["detector"] = DETECTOR_VERSION
    if settled:
        item["model"] = DETECTOR_VERSION
    elif verdict is ClaimVerdict.NEEDS_REVIEW:
        item["model"] = DETECTOR_VERSION


def _tool_evidence(tool: ToolInvocation, body: str | None = None) -> dict[str, str]:
    text = body if body is not None else tool_result_text(tool)
    return {
        "source": "tool",
        "tool_id": tool.id,
        "tool_name": tool.name,
        "path": "$",
        "text": text,
    }


def _tool_body(tool: ToolInvocation) -> str:
    status = tool.status.value
    result = getattr(tool, "result", None)
    if tool.error:
        return f"{tool.name} {status}: {tool.error}"
    if result not in (None, ""):
        return f"{tool.name} {status}: {result if isinstance(result, str) else str(result)}"
    if tool.result_ref:
        return f"{tool.name} {status}: {tool.result_ref}"
    return f"{tool.name} {status}"


def _observe_claim(item: Mapping[str, object]) -> None:
    try:
        from obsalt.metrics import detector_claims_total

        detector_claims_total.labels(
            kind=str(item.get("kind") or ""),
            verdict=str(item.get("verdict") or ""),
            version=DETECTOR_VERSION,
        ).inc()
    except Exception:
        return


def _unique(tools: Sequence[ToolInvocation]) -> ToolInvocation | None:
    if len(tools) == 1:
        return tools[0]
    return None
