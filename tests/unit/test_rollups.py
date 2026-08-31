"""Small tests: fleet rollups. Aggregates never enter sample percentiles."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.cluster import MemoryHangupClusterStore, cluster_hangups
from obsalt.analysis.contributions import MemoryRollupStore
from obsalt.analysis.rollups import build_latency_rollup, build_quality_rollup
from obsalt.analysis.tier2 import decide_tier2
from obsalt.domain.enums import (
    AnalysisState,
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    Metric,
    Provenance,
    Speaker,
    Stage,
    Statistic,
)
from obsalt.domain.models import (
    AggregateMeasurement,
    AnalysisExecution,
    AnalysisResult,
    CallRevision,
    Hangup,
    StageMeasurement,
    Turn,
)
from obsalt.query import hangup_rollup, latency_rollup, sample_percentile


def _call() -> CallRevision:
    return CallRevision(
        org_id="o",
        call_id="c",
        revision="r",
        source="retell",
        source_call_id="s",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
        stage_measurements=[
            StageMeasurement(
                fact_id="a",
                stage=Stage.E2E,
                metric=Metric.DURATION,
                value_ms=580,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
        aggregate_measurements=[
            AggregateMeasurement(
                fact_id="b",
                stage=Stage.E2E,
                metric=Metric.DURATION,
                statistic=Statistic.P95,
                value_ms=10_000,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )


def test_aggregates_never_enter_sample_percentiles() -> None:
    call = _call()
    data = build_latency_rollup([call], "g1")
    assert data["as_of_generation"] == "g1"
    assert data["sample_percentiles"]["e2e"]["p50"] == 580
    assert data["sample_percentiles"]["e2e"]["p95"] == 580
    assert data["provider_aggregates"][0]["value_ms"] == 10_000
    assert data["aggregates_excluded"] == 1
    wrapped = latency_rollup([call], as_of_generation="g1")
    assert wrapped["aggregates_excluded"] == 1
    assert wrapped["items"][0]["p50"] == 580
    mixed = sample_percentile([580.0, 10_000.0], 50)
    assert mixed != 580
    assert mixed == 5290.0


def test_hangup_clusters_and_zero_baseline_samples_out() -> None:
    call = _call()
    clustered = cluster_hangups([call], "g1")
    assert clustered["as_of_generation"] == "g1"
    assert clustered["clusters"][0]["reason"] == HangupReason.USER_HANGUP.value


def test_missing_hangup_clusters_as_not_reported_not_unknown() -> None:
    from obsalt.analysis.hangup import ENDING_NOT_REPORTED

    call = _call().model_copy(update={"hangup": None})
    clustered = cluster_hangups([call], "g1")
    assert clustered["clusters"][0]["reason"] == ENDING_NOT_REPORTED
    unmapped = _call().model_copy(
        update={
            "hangup": Hangup(
                reason=HangupReason.UNKNOWN, party=HangupParty.UNKNOWN, provider_code="x-1"
            )
        }
    )
    other = cluster_hangups([unmapped], "g1")["clusters"][0]
    assert other["reason"] == HangupReason.UNKNOWN.value
    assert other["provider_code"] == "x-1"


def test_hangup_cluster_includes_last_speaker_and_closing_texts() -> None:
    call = _call().model_copy(
        update={
            "hangup": Hangup(reason=HangupReason.USER_HANGUP, last_speaker=Speaker.AGENT),
            "turns": [
                Turn(index=0, speaker=Speaker.USER, text="I want a refund."),
                Turn(index=1, speaker=Speaker.AGENT, text="I can help with that."),
            ],
        }
    )
    cluster = cluster_hangups([call], "g1")["clusters"][0]
    assert cluster["last_speaker"] == "agent"
    assert cluster["last_user_text"] == "I want a refund."
    assert cluster["last_agent_text"] == "I can help with that."
    execution = decide_tier2(
        call.model_copy(update={"hangup": Hangup(reason=HangupReason.COMPLETED)}),
        baseline_sample_rate=0.0,
    )
    assert execution.state is AnalysisState.SAMPLED_OUT


def test_hangup_rollup_ignores_store_cache_outside_the_window() -> None:
    store = MemoryHangupClusterStore()
    store.refresh("acme", [_call()], "g1")
    empty = hangup_rollup([], as_of_generation="g1", store=store, org_id="acme")
    assert empty["clusters"] == []
    assert empty["items"] == []


def test_latency_rollup_empty_window_does_not_read_store_samples() -> None:
    store = MemoryRollupStore()
    store.contribute(_call())
    empty = latency_rollup([], as_of_generation="g1", store=store, org_id="acme")
    assert empty["sample_percentiles"] == {}
    assert empty["items"] == []


def test_pending_hallucination_candidates_are_not_fleet_failures() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r2",
        source="example",
        source_call_id="s",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="refund")],
    )
    pending = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            state=AnalysisState.PENDING,
        ),
        payload={
            "candidates": [{"kind": "price_claim", "needs_llm": True}],
            "selection": "pending",
        },
    )
    confirmed = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            state=AnalysisState.COMPLETED,
        ),
        payload={
            "model": "detector/1",
            "claims": [{"kind": "price_claim", "verdict": "contradicted", "model": "detector/1"}],
        },
    )
    pending_roll = build_quality_rollup([call], [pending], "g1")
    assert pending_roll["hallucinations"]["count"] == 0
    assert pending_roll["hallucinations"]["candidate_count"] == 1
    confirmed_only = build_quality_rollup([call], [confirmed], "g1")
    assert confirmed_only["hallucinations"]["count"] == 1
    assert confirmed_only["hallucinations"]["candidate_count"] == 0
    stale = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r-old",
            analyzer_id="hallucination",
            analyzer_version="1",
            state=AnalysisState.COMPLETED,
        ),
        payload={"claims": [{"kind": "fabricated_id", "verdict": "unsupported"}]},
    )
    assert build_quality_rollup([call], [stale], "g1")["hallucinations"]["count"] == 0


def test_heuristic_completed_is_candidate_not_confirmed() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r2",
        source="example",
        source_call_id="s",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50")],
    )
    heuristic = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            judge_version="heuristic/1",
            state=AnalysisState.COMPLETED,
        ),
        payload={
            "model": "heuristic/1",
            "claims": [
                {"kind": "price_claim", "verdict": "contradicted", "model": "heuristic/1"},
                {"kind": "fabricated_id", "verdict": "unsupported", "model": "heuristic/1"},
                {"kind": "commitment", "verdict": "contradicted", "model": "heuristic/1"},
            ],
        },
    )
    roll = build_quality_rollup([call], [heuristic], "g1")
    assert roll["hallucinations"]["count"] == 0
    assert roll["hallucinations"]["candidate_count"] == 1
    assert roll["hallucinations"]["candidates_by_kind"] == {
        "commitment": 1,
        "fabricated_id": 1,
        "price_claim": 1,
    }
    assert roll["hallucinations"]["candidate_claim_count"] == 3


def test_quality_counts_calls_not_claims_and_collapses_pending_plus_completed() -> None:
    from obsalt.analysis.rollups import latest_analysis_results

    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r2",
        source="example",
        source_call_id="s",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50")],
    )
    pending = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            state=AnalysisState.PENDING,
        ),
        payload={"candidates": [{"kind": "price_claim"}, {"kind": "fabricated_id"}]},
    )
    judged = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            judge_version="openai-compatible/1",
            state=AnalysisState.COMPLETED,
        ),
        payload={
            "model": "gpt-4.1-mini",
            "claims": [
                {"kind": "price_claim", "verdict": "contradicted", "model": "gpt-4.1-mini"},
                {"kind": "fabricated_id", "verdict": "unsupported", "model": "gpt-4.1-mini"},
            ],
        },
    )
    collapsed = latest_analysis_results([pending, judged])
    assert len(collapsed) == 1
    assert collapsed[0].execution.state is AnalysisState.COMPLETED
    roll = build_quality_rollup([call], [pending, judged], "g1")
    assert roll["hallucinations"]["count"] == 0
    assert roll["hallucinations"]["candidate_count"] == 1
    assert roll["hallucinations"]["candidates_by_kind"] == {
        "fabricated_id": 1,
        "price_claim": 1,
    }


def test_detector_contradiction_is_confirmed_and_evidence_missing_is_not() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r2",
        source="example",
        source_call_id="s",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="I refunded $48.50")],
    )
    detector = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r2",
            analyzer_id="hallucination",
            analyzer_version="1",
            judge_version="detector/1",
            state=AnalysisState.COMPLETED,
        ),
        payload={
            "model": "detector/1",
            "claims": [
                {"kind": "price_claim", "verdict": "contradicted", "model": "detector/1"},
                {"kind": "commitment", "verdict": "evidence_missing", "model": "detector/1"},
            ],
        },
    )
    roll = build_quality_rollup([call], [detector], "g1")
    assert roll["hallucinations"]["count"] == 1
    assert roll["hallucinations"]["by_kind"] == {"price_claim": 1}
    assert roll["hallucinations"]["candidate_count"] == 0
    assert roll["hallucinations"]["evidence_missing"] == 1


def test_latest_analysis_prefers_completed_over_newer_pending() -> None:
    from obsalt.analysis.rollups import latest_analysis_results

    pending_v3 = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r",
            analyzer_id="quality_card",
            analyzer_version="3",
            state=AnalysisState.PENDING,
        ),
        payload={"schema": "obsalt.quality_card/3"},
    )
    completed_v2 = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c1",
            revision="r",
            analyzer_id="quality_card",
            analyzer_version="2",
            state=AnalysisState.COMPLETED,
        ),
        payload={"dimensions": {"faithfulness": {"status": "fail"}}},
    )
    chosen = latest_analysis_results([pending_v3, completed_v2])
    assert len(chosen) == 1
    assert chosen[0].execution.analyzer_version == "2"
    assert chosen[0].execution.state is AnalysisState.COMPLETED


def test_review_queue_orders_critical_before_high_and_skips_low() -> None:
    def _hallo(call_id: str, severity: str, kind: str = "price_claim") -> AnalysisResult:
        return AnalysisResult(
            execution=AnalysisExecution(
                call_id=call_id,
                revision="r",
                analyzer_id="hallucination",
                analyzer_version="1",
                state=AnalysisState.COMPLETED,
            ),
            payload={
                "model": "detector/1",
                "claims": [
                    {
                        "kind": kind,
                        "verdict": "contradicted",
                        "model": "detector/1",
                        "severity": severity,
                    }
                ],
            },
        )

    critical = _call().model_copy(update={"call_id": "c-crit"})
    high = _call().model_copy(update={"call_id": "c-high"})
    roll = build_quality_rollup(
        [critical, high],
        [_hallo("c-high", "high", "commitment"), _hallo("c-crit", "critical")],
        "g1",
    )
    queue = roll["review_queue"]
    assert [item["call_id"] for item in queue] == ["c-crit", "c-high"]
    assert queue[0]["severity"] == "critical"


def test_behavior_flags_rollup_counts_calls_by_kind() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="c9",
        revision="r9",
        source="example",
        source_call_id="s9",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="hello")],
    )
    flags_row = AnalysisResult(
        execution=AnalysisExecution(
            call_id="c9",
            revision="r9",
            analyzer_id="flags",
            analyzer_version="2",
            state=AnalysisState.COMPLETED,
        ),
        payload={"flags": [{"kind": "loop_detected"}, {"kind": "dead_air"}]},
    )
    rollup = build_quality_rollup([call], [flags_row], "gen-test")
    assert rollup["behavior_flags"]["calls"] == 1
    assert rollup["behavior_flags"]["by_kind"] == {
        "dead_air": 1,
        "loop_detected": 1,
    }
