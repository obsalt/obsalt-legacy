"""Remaining rewrite-plan contracts that prior PRs left incomplete."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import permutations
from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.analysis.calibration import calibrate_rubric
from obsalt.analysis.contributions import MemoryRollupStore
from obsalt.analysis.entailment import entail_claims
from obsalt.analysis.judge import HeuristicJudge
from obsalt.api import create_app
from obsalt.assemble.assembler import fold_facts
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.domain.enums import (
    KeyScope,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.domain.models import FidelityDeclaration, Rubric
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.ops.backfill import run_backfill
from obsalt.ops.parquet import export_revisions
from obsalt.ops.privacy import apply_deletion
from obsalt.ops.retention import replay_horizon, sweep_raw
from obsalt.ops.schema_drift import compare_vendored
from obsalt.otel.span_identity import SpanIdentityIndex, otlp_delivery_key, span_content_fingerprint
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import BackfillCursor, ConnectionConfig, ReadableSpan, TombstoneHints
from obsalt.privacy.caller import DEFAULT_PEPPER, caller_token
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.util import utcnow
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit.schema import FixtureSuite, blocking_errors, validate_raw_fixtures

EXAMPLE_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "obsalt-example"
    / "src"
    / "obsalt_example"
    / "fixtures"
)


def _decl() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION, Signal.TURN_INTERVAL}),
        structurally_absent={Signal.VAD: "not in fixture"},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def _state() -> AppState:
    plugin = ExamplePlugin()
    resolver = MemoryResolver()
    resolver.add(
        ConnectionConfig(
            org_id="acme",
            provider="example",
            connection_id="c1",
            ingest_key_hash="",
            secrets={"hmac_secret": "s"},
        ),
        "ik",
    )
    return AppState(
        settings=Settings(),
        plugins=[LoadedPlugin(plugin)],
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={"k": ("acme", frozenset(KeyScope)), "other": ("other", frozenset(KeyScope))},
        rollup_generation="g1",
        rollups=MemoryRollupStore(),
        traces=MemoryTraceAssembler(),
    )


def test_fold_facts_is_permutation_idempotent() -> None:
    events = [
        CallObserved(source_call_id="c1", agent_id="a"),
        TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="hello"),
        StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=40,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="stt",
        ),
    ]
    first_ids, first_conflicts, _ = fold_facts(events)
    assert first_conflicts == []
    for perm in permutations(events):
        ids, conflicts, _ = fold_facts(perm)
        assert conflicts == []
        assert set(ids) == set(first_ids)


def test_schema_fixture_harness_accepts_example() -> None:
    suite = FixtureSuite(EXAMPLE_FIXTURES)
    messages = validate_raw_fixtures(suite)
    assert blocking_errors(messages) == []


def test_schema_drift_offline_is_reproducible() -> None:
    report = compare_vendored(EXAMPLE_FIXTURES)
    assert report["status"] == "offline"
    assert report["diverged"] is False


def test_otlp_delivery_key_is_per_span_identity() -> None:
    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
        attributes={"k": "v"},
    )
    raw = b'{"resourceSpans":[]}'
    key = otlp_delivery_key("acme", raw, [span])
    again = otlp_delivery_key("acme", raw + b" ", [span])
    assert key == again
    other = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=99,
        attributes={"k": "v"},
    )
    assert otlp_delivery_key("acme", raw, [other]) != key
    index = SpanIdentityIndex()
    assert index.observe("acme", span) == "accepted"
    assert index.observe("acme", span) == "duplicate"
    assert index.observe("acme", other) == "conflict"


def test_late_spans_after_finalize_rebuild() -> None:
    assembler = MemoryTraceAssembler()
    span = ReadableSpan(
        name="turn",
        trace_id="t1",
        span_id="s1",
        start_unix_nano=1_000_000_000,
        end_unix_nano=2_000_000_000,
    )
    record = assembler.ingest("acme", [span], [CallObserved(source_call_id="c1")])
    assembler.mark_finalized(record)
    late = ReadableSpan(
        name="turn",
        trace_id="t1",
        span_id="s2",
        start_unix_nano=3_000_000_000,
        end_unix_nano=4_000_000_000,
        parent_span_id="",
        attributes={"obsalt.as_root": True},
    )
    again = assembler.ingest("acme", [late], [TurnObserved(turn_index=0, speaker=Speaker.USER, text="late")])
    assert again.late_after_finalize is True
    assert again.finalized is False
    assert assembler.ready(again) is True


def test_delete_by_caller_and_range() -> None:
    state = _state()
    process_normalized_events(
        [
            CallObserved(
                source_call_id="c1",
                from_number="+15551212",
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="hello"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    result = apply_deletion(state, org_id="acme", caller="+15551212")
    assert result["undoable"] is False
    assert result["deleted_calls"]
    client = TestClient(create_app(Settings(), state))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"] == []


def test_cross_tenant_call_is_404() -> None:
    state = _state()
    first = process_normalized_events(
        [CallObserved(source_call_id="c1")],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    client = TestClient(create_app(Settings(), state))
    res = client.get(f"/v1/calls/{first.call_id}", headers={"X-API-Key": "other"})
    assert res.status_code == 404


def test_replay_horizon_and_raw_sweep() -> None:
    state = _state()
    key = "org/acme/raw/example/old/abc"
    state.objects.put(key, b"{}", content_type="application/json")
    from obsalt.domain.enums import EnvelopeState
    from obsalt.plugin.types import RawEnvelope

    envelope = RawEnvelope(
        envelope_id="old",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key=key,
        delivery_key="old",
        content_sha256="x",
        state=EnvelopeState.ASSEMBLED,
        received_at=utcnow() - timedelta(days=40),
        body=b"{}",
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    report = sweep_raw(state, raw_retention_days=30)
    assert report["purged_blobs"] == 1
    assert key not in state.objects.blobs
    horizon = replay_horizon(raw_retention_days=30)
    assert "replayable_after" in horizon


def test_parquet_export_writes_manifest(tmp_path: Path) -> None:
    state = _state()
    rev = process_normalized_events(
        [CallObserved(source_call_id="c1")],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    dest = tmp_path / "export"
    manifest = export_revisions([rev], dest, as_of_generation="g1")
    assert (dest / "manifest.json").exists()
    assert manifest["call_count"] == 1
    assert "cannot revoke" in manifest["note"]


def test_backfill_runner_respects_tombstones() -> None:
    state = _state()
    for cfg in state.resolver.connections.values():
        if cfg.provider == "example":
            cfg.settings["backfill_items"] = [{"id": "u1", "hash": "abc"}]
    state.inbox.tombstone("acme", TombstoneHints(source_call_id="u1"))
    report = run_backfill(state, org_id="acme", provider="example")
    assert report["status"] == "queued"
    assert report["envelopes"] == 0


def test_entailment_and_calibration() -> None:
    state = _state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    import asyncio

    claims = asyncio.run(entail_claims(rev, judge=HeuristicJudge()))
    assert claims
    rubric = Rubric(id="r1", org_id="acme", name="hallucination", description="Flag invented facts")
    result = asyncio.run(calibrate_rubric(rubric, [(rev, False)], judge=HeuristicJudge()))
    assert result["n"] == 1
    assert "agreement" in result


def test_call_list_filters_and_retention_api() -> None:
    state = _state()
    process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="support"),
            StageObserved(
                stage=Stage.E2E,
                metric=Metric.DURATION,
                value_ms=900,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="e2e",
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    client = TestClient(create_app(Settings(), state))
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=support&latency_ms=100",
        headers={"X-API-Key": "k"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"]
    empty = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z&agent_id=other",
        headers={"X-API-Key": "k"},
    )
    assert empty.json()["items"] == []
    horizon = client.get("/v1/retention", headers={"X-API-Key": "k"})
    assert horizon.status_code == 200
    assert horizon.json()["raw_retention_days"] == 30


def test_stream_source_example_frame() -> None:
    import asyncio

    plugin = ExamplePlugin()
    cfg = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        settings={"emit_example_frame": True},
    )

    async def _collect() -> list:
        return [frame async for frame in plugin.frames(cfg)]

    frames = asyncio.run(_collect())
    assert len(frames) == 1
    assert frames[0].source_call_id is None or frames[0].delivery_key == "example-stream-1"


def test_vapi_backfill_without_key_is_truncated() -> None:
    from obsalt_vapi.plugin import VapiPlugin

    plugin = VapiPlugin()
    page = plugin.scan(
        ConnectionConfig(org_id="o", provider="vapi", connection_id="c", ingest_key_hash="x"),
        BackfillCursor(),
    )
    assert page.items == []
    assert page.truncated_by_retention is True


def test_review_and_webhook_outbox() -> None:
    state = _state()
    client = TestClient(create_app(Settings(), state))
    res = client.post(
        "/v1/quality/review",
        headers={"X-API-Key": "k"},
        json={"call_id": "missing-is-ok", "agree": False},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "recorded"
    token = caller_token("acme", "+1555", DEFAULT_PEPPER)
    assert len(token) == 64
    assert token == caller_token("acme", "+1555", DEFAULT_PEPPER)


def test_otlp_receive_records_span_identity() -> None:
    state = _state()
    span = ReadableSpan(
        name="turn",
        trace_id="aa" * 16,
        span_id="bb" * 8,
        start_unix_nano=1,
        end_unix_nano=2,
    )
    result = receive_otlp_batch(
        org_id="acme",
        raw=b'{"resourceSpans":[]}',
        content_type="application/json",
        objects=state.objects,
        inbox=state.inbox,
        spans=[span],
        span_index=SpanIdentityIndex(),
    )
    assert result.created is True
    assert span_content_fingerprint(span)
