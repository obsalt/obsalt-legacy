"""Compose a revision-keyed Quality Card. Flags, not a dimension scorecard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from obsalt.analysis.hallucination import detect_claims
from obsalt.domain.enums import AnalysisState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.util import sha256_text

ANALYZER_ID = "quality_card"
ANALYZER_VERSION = "3"
SCHEMA = "obsalt.quality_card/3"

STATUS_PASS = "pass"
STATUS_NOT_JUDGED = "not_judged"

VACUOUS_PASS_REASONS = frozenset(
    {
        "no factual claims to check",
        "no tools on this call",
        "no tool claims to check",
        "no bound tool-success claim",
        "tools did not contradict the agent",
    }
)
_PAGEABLE = frozenset({"critical", "high"})


def card_dimension_status(dim: Mapping[str, Any]) -> str:
    """Stored vacuous passes are coverage, not a detector pass."""
    status = str(dim.get("status") or "")
    reason = str(dim.get("reason") or "")
    if status == STATUS_PASS and reason in VACUOUS_PASS_REASONS:
        return STATUS_NOT_JUDGED
    return status


def compose_quality_card(
    call: CallRevision,
    *,
    claims: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Detector-backed card. No dimension matrix. Does not copy the claim list."""
    resolved = [dict(item) for item in (claims if claims is not None else detect_claims(call))]
    confirmed = [item for item in resolved if _confirmed(item)]
    candidates = [item for item in resolved if str(item.get("verdict") or "") == "needs_review"]
    missing = [item for item in resolved if str(item.get("verdict") or "") == "evidence_missing"]
    critical = any(str(item.get("severity") or "") in _PAGEABLE for item in confirmed)
    return {
        "schema": SCHEMA,
        "selection": "detector",
        "flag_kinds": sorted(
            {str(item.get("kind") or "") for item in confirmed if item.get("kind")}
        ),
        "candidate_kinds": sorted(
            {str(item.get("kind") or "") for item in candidates if item.get("kind")}
        ),
        "critical_failure": critical,
        "needs_human_review": bool(confirmed or candidates or missing),
        "counts": {
            "confirmed": len(confirmed),
            "candidates": len(candidates),
            "evidence_missing": len(missing),
        },
    }


def quality_card_result(
    call: CallRevision,
    *,
    claims: Sequence[Mapping[str, Any]] | None = None,
) -> AnalysisResult:
    payload = compose_quality_card(call, claims=claims)
    return AnalysisResult(
        execution=AnalysisExecution(
            call_id=call.call_id,
            revision=call.revision,
            analyzer_id=ANALYZER_ID,
            analyzer_version=ANALYZER_VERSION,
            state=AnalysisState.COMPLETED,
            content_hash=sha256_text(call.revision + ANALYZER_ID),
        ),
        payload=payload,
    )


def _confirmed(claim: Mapping[str, Any]) -> bool:
    if str(claim.get("verdict") or "") != "contradicted":
        return False
    return str(claim.get("model") or "").startswith("detector")
