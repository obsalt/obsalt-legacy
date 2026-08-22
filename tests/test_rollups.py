from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.cluster import cluster_hangups
from obsalt.analysis.rollups import build_latency_rollup
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
from obsalt.domain.models import AggregateMeasurement, CallRevision, Hangup, StageMeasurement, Turn
from obsalt.query import latency_rollup, sample_percentile
from obsalt.search.hybrid import rrf
from obsalt.search.index import MemorySearchIndex


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
    samples = data["sample_percentiles"]["e2e"]
    assert samples["p50"] == 580
    assert data["provider_aggregates"][0]["value_ms"] == 10_000
    wrapped = latency_rollup([call], as_of_generation="g1")
    assert wrapped["aggregates_excluded"] == 1
    assert sample_percentile([580, 10_000], 50) != samples["p50"]


def test_hangup_clusters_and_tier2_sampled_out_at_zero_rate() -> None:
    call = _call()
    clustered = cluster_hangups([call], "g1")
    assert clustered["as_of_generation"] == "g1"
    assert clustered["clusters"][0]["reason"] == HangupReason.USER_HANGUP.value
    execution = decide_tier2(call, baseline_sample_rate=0.0)
    assert execution.state is AnalysisState.SAMPLED_OUT


def test_search_rrf() -> None:
    assert rrf(["a", "b"], ["b", "c"])[0] == "b"
    refund = _call()
    refund.call_id = "refund-call"
    refund.turns = [Turn(index=0, speaker=Speaker.USER, text="customer asking about refunds")]
    weather = CallRevision(
        org_id="o",
        call_id="weather-call",
        revision="r",
        source="retell",
        source_call_id="w",
        agent_id="other",
        turns=[Turn(index=0, speaker=Speaker.USER, text="hello how is the weather today")],
    )
    index = MemorySearchIndex()
    index.index(refund)
    index.index(weather)
    result = index.query("refunds")
    assert result["fused_ids"] == rrf(result["vector_ids"], result["lexical_ids"])
    assert result["fused_ids"][0] == "refund-call"
