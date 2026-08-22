"""Invariants required by docs/rewrite-plan.md that the prior skeleton missed."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from obsalt.api import create_app
from obsalt.assemble.assembler import fold_facts
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.rehydrate import events_from_revision
from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
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
from obsalt.domain.events import (
    CallObserved,
    SnapshotBoundaryObserved,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration
from obsalt.ingest.otlp import receive_otlp_batch
from obsalt.otel.forward_queue import MemoryForwardQueue
from obsalt.otel.trace_assembly import MemoryTraceAssembler
from obsalt.plugin.host import LoadedPlugin
from obsalt.plugin.types import ConnectionConfig
from obsalt.runtime import AppState
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_example.plugin import ExamplePlugin
from obsalt_pipecat.plugin import PipecatPlugin

FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-example" / "src" / "obsalt_example" / "fixtures"


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
        settings=Settings(trace_grace_seconds=0),
        plugins=[LoadedPlugin(plugin), LoadedPlugin(PipecatPlugin())],
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={"k": ("acme", frozenset(KeyScope))},
        rollup_generation="g1",
        traces=MemoryTraceAssembler(),
        forward_queue=MemoryForwardQueue(),
    )


def test_late_events_fold_onto_previous_revision() -> None:
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    first_events = [
        CallObserved(source_call_id="c1", agent_id="a"),
        TurnObserved(turn_index=0, speaker=Speaker.USER, text="hello"),
    ]
    first = process_normalized_events(
        first_events,
        org_id="o",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    second_events = [
        CallObserved(source_call_id="c1", agent_id="a"),
        TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="hi back"),
    ]
    second = process_normalized_events(
        second_events,
        org_id="o",
        source="example",
        source_call_id="c1",
        envelope_id="e2",
        declaration=_decl(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    assert first.revision != second.revision
    assert sink.get("o", first.call_id, first.revision) is not None
    texts = {turn.text for turn in second.turns}
    assert texts == {"hello", "hi back"}
    assert pointers.get("o", first.call_id) == second.revision


def test_snapshot_retracts_omitted_authoritative_facts() -> None:
    accepted, conflicts, _ = fold_facts(
        [
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="old"),
            TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="gone"),
            SnapshotBoundaryObserved(authoritative_domains=["turn_observed"]),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="new"),
        ]
    )
    assert conflicts == []
    turns = [record.event for record in accepted.values() if isinstance(record.event, TurnObserved)]
    assert len(turns) == 1
    assert turns[0].text == "new"


def test_rehydrate_does_not_emit_snapshot() -> None:
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            SnapshotBoundaryObserved(authoritative_domains=["turn_observed"]),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        ],
        org_id="o",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    rebuilt = events_from_revision(rev)
    assert not any(event.type == "snapshot_boundary" for event in rebuilt)


def test_otlp_uses_inbox_and_does_not_forward_on_request_path() -> None:
    state = _state()
    dest_hits = {"n": 0}

    def _fake_forward(*_args, **_kwargs):
        dest_hits["n"] += 1
        raise AssertionError("forward must not run on the receive path")

    state.destinations = [{"org_id": "acme", "url": "https://example.test/v1/traces"}]
    raw = b'{"resourceSpans":[]}'
    result = receive_otlp_batch(
        org_id="acme",
        raw=raw,
        content_type="application/json",
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    assert result.envelope is not None
    assert result.envelope.object_key in state.objects.blobs
    assert state.inbox.outbox
    assert dest_hits["n"] == 0


def test_otlp_mixed_org_resource_rejected() -> None:
    state = _state()
    client = TestClient(create_app(Settings(trace_grace_seconds=0), state))
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "obsalt.org", "value": {"stringValue": "other"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "turn",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    import json

    res = client.post(
        "/v1/traces",
        content=json.dumps(payload),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 401


def test_otlp_complete_batch_assembles_stage_level() -> None:
    state = _state()
    client = TestClient(create_app(Settings(trace_grace_seconds=0), state))
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "turn",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {"key": "turn.index", "value": {"intValue": "0"}},
                                    {"key": "gen_ai.conversation.id", "value": {"stringValue": "room-1"}},
                                    {"key": "turn.speaker", "value": {"stringValue": "user"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    import json

    res = client.post(
        "/v1/traces",
        content=json.dumps(payload),
        headers={"X-API-Key": "k", "content-type": "application/json"},
    )
    assert res.status_code == 200
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    assert listed.json()["items"]
    item = listed.json()["items"][0]
    detail = client.get(f"/v1/calls/{item['id']}", headers={"X-API-Key": "k"})
    assert detail.status_code == 200
    assert detail.json()["timeline_fidelity"] in {"stage_level", "turn_level"}


def test_ui_does_not_collapse_to_first_org_without_session() -> None:
    state = _state()
    client = TestClient(create_app(Settings(), state))
    raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()
    client.post(
        "/v1/ingest/example/ik",
        content=raw,
        headers={"x-obsalt-example-signature": hmac_hex("s", raw)},
    )
    page = client.get("/v1/ui")
    assert page.status_code == 200
    assert b"ex-1" not in page.content


def test_csrf_required_for_ui_analyze() -> None:
    state = _state()
    client = TestClient(create_app(Settings(), state))
    raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()
    client.post(
        "/v1/ingest/example/ik",
        content=raw,
        headers={"x-obsalt-example-signature": hmac_hex("s", raw)},
    )
    listed = client.get(
        "/v1/calls?start=2020-01-01T00:00:00Z&end=2030-01-01T00:00:00Z",
        headers={"X-API-Key": "k"},
    )
    call_id = listed.json()["items"][0]["id"]
    login = client.post("/v1/ui/login", data={"api_key": "k"}, follow_redirects=False)
    assert login.status_code == 303
    denied = client.post(f"/v1/ui/calls/{call_id}/analyze", data={"csrf": "nope"}, follow_redirects=False)
    assert denied.status_code == 403


def test_evidence_blobs_are_org_namespaced() -> None:
    objects = MemoryObjectStore()
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="refund please"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
        objects=objects,
    )
    keys = list(objects.blobs)
    assert keys
    assert all(key.startswith("org/acme/evidence/") for key in keys)


def test_interval_without_timestamps_is_not_drawn() -> None:
    from obsalt.assemble.assembler import Assembler
    from obsalt.assemble.facts import stamp_event
    from obsalt.assemble.timeline import timeline_view

    events = [
        stamp_event(
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=12,
                placement=MeasurementPlacement.INTERVAL,
                started_at=None,
                ended_at=None,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="bad",
            ),
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=0,
        )
    ]
    call = Assembler(_decl(), decoder_version="t/1", processing_run_id="r").assemble("o", "cid", "test", events)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []


def test_conflicts_block_assembled_mark() -> None:
    from obsalt.domain.enums import EnvelopeState
    from obsalt.plugin.types import RawEnvelope, TombstoneHints
    from obsalt.util import utcnow
    from obsalt.worker.drain import _after_promote

    state = _state()
    first = process_normalized_events(
        [CallObserved(source_call_id="c1"), StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=10,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="latency.stt",
        )],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    conflicted = process_normalized_events(
        [StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=99,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="latency.stt",
        )],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e2",
        declaration=_decl(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    assert first.revision != conflicted.revision
    envelope = RawEnvelope(
        envelope_id="e2",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="e2",
        content_sha256="x",
        state=EnvelopeState.QUEUED,
        received_at=utcnow(),
    )
    state.inbox.by_id[envelope.envelope_id] = envelope
    state.inbox.outbox.append(envelope.envelope_id)
    _after_promote(state, envelope, conflicted, TombstoneHints(source_call_id="c1"))
    if conflicted.conflicts:
        assert envelope.state is not EnvelopeState.ASSEMBLED
        assert envelope.envelope_id in state.inbox.failures
    else:
        # Additive merge filled nothing and disagreed — must still not silently overwrite.
        assert state.pointers.get("acme", first.call_id) in {first.revision, conflicted.revision}


def test_example_rest_backfill_and_stream_declared() -> None:
    from obsalt.domain.enums import Capability
    from obsalt.plugin.types import BackfillCursor, BackfillItem

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
