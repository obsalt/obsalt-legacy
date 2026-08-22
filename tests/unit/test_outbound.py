"""Small tests: outbound SLO hooks and hangup rollup cache."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.analysis.cluster import MemoryHangupClusterStore
from obsalt.config import Settings
from obsalt.domain.enums import HangupReason, MeasurementPlacement, Metric, Provenance, Stage
from obsalt.domain.models import CallRevision, Hangup, StageMeasurement
from obsalt.query import hangup_rollup
from obsalt.runtime import in_memory_state
from obsalt.webhooks.outbound import maybe_emit_slo, mint_whsec


def test_hangup_rollup_serves_materialized_store() -> None:
    store = MemoryHangupClusterStore()
    call = CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="vapi",
        source_call_id="s1",
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
    )
    store.refresh("acme", [call], "g1")
    store.by_org["acme"]["clusters"][0]["reason"] = "cached-user-hangup"
    data = hangup_rollup([], as_of_generation="g1", store=store, org_id="acme")
    assert data["items"][0]["reason"] == "cached-user-hangup"


def test_slo_breached_emitted_when_e2e_exceeds_threshold(monkeypatch) -> None:
    monkeypatch.setattr("obsalt.webhooks.outbound.drain_outbound", lambda _state: 0)
    state = in_memory_state(Settings(environment="test", slo_e2e_ms=50))
    state.webhook_destinations.append(
        {
            "id": "d1",
            "org_id": "dev",
            "url": "http://127.0.0.1:1/hooks",
            "secret": mint_whsec(),
            "event_type": "slo.breached",
            "allow_http_localhost": "true",
        }
    )
    revision = CallRevision(
        org_id="dev",
        call_id="c-slo",
        revision="r1",
        source="example",
        source_call_id="slo",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        stage_measurements=[
            StageMeasurement(
                fact_id="e2e",
                stage=Stage.E2E,
                metric=Metric.DURATION,
                value_ms=500.0,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )
    maybe_emit_slo(state, revision)
    assert any(
        item.get("payload", {}).get("type") == "slo.breached" for item in state.webhook_outbox
    )
