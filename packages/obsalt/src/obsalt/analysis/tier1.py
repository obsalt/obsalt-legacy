"""Deterministic analysis that runs on every assembled revision."""

from __future__ import annotations

from obsalt.analysis.flags import rule_flags
from obsalt.analysis.tools import tool_telemetry
from obsalt.domain.enums import AnalysisExecutionState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.hangup.taxonomy import customer_loss_score

ANALYZER_VERSION = "tier1/1"


def analyze_tier1(revision: CallRevision) -> tuple[AnalysisExecution, list[AnalysisResult]]:
    loss, factors = customer_loss_score(revision)
    flags = rule_flags(revision)
    tools = tool_telemetry(revision)
    results = [
        AnalysisResult(
            org_id=revision.org_id,
            call_id=revision.call_id,
            revision=revision.revision,
            analyzer_id="tier1.hangup_loss",
            analyzer_version=ANALYZER_VERSION,
            kind="hangup_loss",
            score=loss,
            label=",".join(factors) if factors else "none",
            payload={"factors": factors},
        ),
        AnalysisResult(
            org_id=revision.org_id,
            call_id=revision.call_id,
            revision=revision.revision,
            analyzer_id="tier1.tools",
            analyzer_version=ANALYZER_VERSION,
            kind="tool_telemetry",
            payload=tools,
        ),
        *flags,
    ]
    execution = AnalysisExecution(
        org_id=revision.org_id,
        call_id=revision.call_id,
        revision=revision.revision,
        analyzer_id="tier1",
        analyzer_version=ANALYZER_VERSION,
        content_hash=revision.processing_run_id,
        state=AnalysisExecutionState.COMPLETED,
    )
    return execution, results
