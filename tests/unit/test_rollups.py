"""Small tests: fleet rollups. Aggregates never enter sample percentiles."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.cluster import MemoryHangupClusterStore, cluster_hangups
from obsalt.analysis.contributions import MemoryRollupStore
from obsalt.analysis.rollups import build_latency_rollup, build_quality_rollup
from obsalt.analysis.tier2 import decide_tier2
from obsalt.domain.enums import (
    AnalysisState,
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
        payload={"claims": [{"kind": "price_claim", "verdict": "contradicted"}]},
    )
    assert build_quality_rollup([call], [pending], "g1")["hallucinations"]["count"] == 0
    confirmed_only = build_quality_rollup([call], [confirmed], "g1")
    assert confirmed_only["hallucinations"]["count"] == 1
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
