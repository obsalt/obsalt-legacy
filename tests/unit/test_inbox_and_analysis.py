"""Inbox DLQ, hangup rollup cache, pending hallucination, and plugin ops."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from obsalt.analysis.calibration import calibrate_rubric
from obsalt.analysis.cluster import MemoryHangupClusterStore
from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.judge import HeuristicJudge
from obsalt.domain.enums import AnalysisState, Capability, HangupReason, Speaker
from obsalt.domain.events import CallObserved, GroundingObserved, TurnObserved
from obsalt.domain.models import CallRevision, Hangup, Rubric
from obsalt.plugin.types import (
    BackfillCursor,
    BackfillItem,
    ConnectionConfig,
    RawEnvelope,
    TombstoneHints,
)
from obsalt.query import hangup_rollup
from obsalt.testing.fakes import MemoryInbox
from obsalt.util import utcnow
from obsalt.worker.process import process_normalized_events
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit import decode_raw_fixture
from obsalt_vapi.plugin import VapiPlugin
from tests.helpers import CARTESIA_FIXTURES, ELEVEN_FIXTURES, example_state, fidelity_declaration


def test_decode_dlq_after_eight_failures() -> None:
    inbox = MemoryInbox()
    envelope = RawEnvelope(
        envelope_id="e-fail",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        received_at=utcnow(),
    )
    inbox.accept(envelope, tombstone_hints=TombstoneHints())
    for _ in range(7):
        inbox.mark_failed(envelope.envelope_id, "decode failed")
        assert envelope.envelope_id not in {row["envelope_id"] for row in inbox.dlq}
        assert envelope.envelope_id in inbox.outbox
    inbox.mark_failed(envelope.envelope_id, "decode failed")
    assert inbox.dlq[-1]["envelope_id"] == envelope.envelope_id
    assert envelope.envelope_id not in inbox.outbox


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


def test_hallucination_stays_pending_until_tier2() -> None:
    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    rows = state.sink.analysis[(rev.org_id, rev.call_id, rev.revision)]
    hallo = next(row for row in rows if row.execution.analyzer_id == "hallucination")
    assert hallo.execution.state is AnalysisState.PENDING


def test_entailment_and_calibration() -> None:
    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    claims = asyncio.run(entail_claims(rev, judge=HeuristicJudge()))
    assert claims
    assert any(item["verdict"] != "grounded" for item in claims)
    rubric = Rubric(id="r1", org_id="acme", name="hallucination", description="Flag invented facts")
    result = asyncio.run(calibrate_rubric(rubric, [(rev, False)], judge=HeuristicJudge()))
    assert result["n"] == 1
    assert "agreement" in result


def test_vapi_backfill_without_key_is_truncated() -> None:
    plugin = VapiPlugin()
    page = plugin.scan(
        ConnectionConfig(org_id="o", provider="vapi", connection_id="c", ingest_key_hash="x"),
        BackfillCursor(),
    )
    assert page.items == []
    assert page.truncated_by_retention is True


def test_example_rest_backfill_and_stream_declared() -> None:
    plugin = ExamplePlugin()
    assert Capability.REST_BACKFILL in plugin.capabilities
    assert Capability.STREAM_SOURCE in plugin.capabilities
    page = plugin.scan(
        ConnectionConfig(org_id="o", provider="example", connection_id="c", ingest_key_hash="x"),
        BackfillCursor(),
    )
    assert page.items == []
    envelope = plugin.hydrate(
        ConnectionConfig(org_id="o", provider="example", connection_id="c", ingest_key_hash="x"),
        BackfillItem(upstream_entity_id="u1", content_hash="abc"),
    )
    assert envelope.delivery_key.startswith("c:u1:")


def test_elevenlabs_and_cartesia_emit_user_grounding() -> None:
    eleven = decode_raw_fixture(
        ElevenLabsPlugin(),
        ELEVEN_FIXTURES / "raw" / "post_call_transcription.json",
    )
    assert any(isinstance(event, GroundingObserved) and "refund" in event.content.lower() for event in eleven)
    cartesia = decode_raw_fixture(
        CartesiaPlugin(),
        CARTESIA_FIXTURES / "raw" / "call_ended.json",
    )
    assert any(isinstance(event, GroundingObserved) and event.content for event in cartesia)


def test_slo_breached_emitted_when_e2e_exceeds_threshold(monkeypatch) -> None:
    from obsalt.config import Settings
    from obsalt.domain.enums import MeasurementPlacement, Metric, Provenance, Stage
    from obsalt.domain.models import StageMeasurement
    from obsalt.runtime import in_memory_state
    from obsalt.webhooks.outbound import maybe_emit_slo, mint_whsec

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
    assert any(item.get("payload", {}).get("type") == "slo.breached" for item in state.webhook_outbox)
