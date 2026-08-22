from __future__ import annotations

from obsalt.domain.models import AnalysisResult, CallRevision

ANALYZER_VERSION = "tier1-flags/1"


def rule_flags(revision: CallRevision) -> list[AnalysisResult]:
    flags: list[AnalysisResult] = []

    def add(kind: str, rationale: str, **payload: object) -> None:
        flags.append(
            AnalysisResult(
                org_id=revision.org_id,
                call_id=revision.call_id,
                revision=revision.revision,
                analyzer_id="tier1.flags",
                analyzer_version=ANALYZER_VERSION,
                kind=kind,
                passed=False,
                rationale=rationale,
                payload=dict(payload),
            )
        )

    failed = [t for t in revision.tools if t.status.value in {"error", "timeout"}]
    if failed:
        add("tool_failure", "one or more tools failed", names=[t.name for t in failed])

    low_conf = [t for t in revision.turns if t.confidence is not None and t.confidence < 0.4]
    if low_conf:
        add("low_stt_confidence", "STT confidence below 0.4", turns=[t.index for t in low_conf])

    user_turns = [t for t in revision.turns if t.speaker.value == "user" and t.text]
    if revision.turns and not user_turns:
        add("dead_air", "no user speech was transcribed")

    duration = revision.lifecycle.duration_ms or 0
    if duration > 0 and len(revision.turns) <= 1 and duration > 20_000:
        add("silence", "long call with almost no turns")

    return flags
