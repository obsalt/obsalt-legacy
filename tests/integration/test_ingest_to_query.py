"""Receive → drain → query for hosted providers. Units, provenance, T1."""

from __future__ import annotations

import gzip

from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import MeasurementPlacement, Provenance
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.plugin.types import RawEnvelope
from obsalt.query import active_calls
from obsalt.util import new_id, utcnow
from obsalt.worker.drain import drain_once
from obsalt.worker.process import MemoryRevisionSink, process_envelope
from obsalt_example.plugin import ExamplePlugin
from obsalt_retell.plugin import RetellPlugin
from obsalt_vapi.plugin import VapiPlugin

from tests.conftest import (
    EXAMPLE_FIXTURES,
    RETELL_FIXTURES,
    VAPI_FIXTURES,
    example_headers,
    example_state,
    fidelity_declaration,
    retell_headers,
    retell_state,
    vapi_headers,
    vapi_state,
)


def test_vapi_end_of_call_uses_published_keys_and_does_not_invent_intervals() -> None:
    state = vapi_state()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    result = receive_webhook(
        provider="vapi",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(vapi_headers()),
        resolver=state.resolver,
        plugin=VapiPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    assert result.envelope is not None
    assert result.envelope.body == raw
    drain_once(state)
    calls = active_calls(state, "acme")
    assert len(calls) == 1
    call = calls[0]
    assert call.source_call_id == "vapi-call-refund-1"
    paths = {m.source_path or "" for m in call.stage_measurements}
    assert any("transcriberLatency" in path for path in paths)
    assert any("modelLatency" in path for path in paths)
    assert any("voiceLatency" in path for path in paths)
    assert any("endpointingLatency" in path for path in paths)
    assert all(m.placement is not MeasurementPlacement.INTERVAL for m in call.stage_measurements)
    assert all(m.provenance is Provenance.PROVIDER_REPORTED for m in call.stage_measurements)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["unplaced_stage_chips"]
    assert call.turns
    assert all(turn.provenance_by_field for turn in call.turns)
    assert any(g.kind.value == "system_prompt" for g in call.grounding)


def test_retell_word_offsets_are_seconds_and_latency_values_are_milliseconds() -> None:
    state = retell_state()
    raw = (RETELL_FIXTURES / "raw" / "call_ended.json").read_bytes()
    result = receive_webhook(
        provider="retell",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(retell_headers(raw)),
        resolver=state.resolver,
        plugin=RetellPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    drain_once(state)
    call = active_calls(state, "acme")[0]
    assert call.source_call_id == "retell-call-happy-1"
    samples = [m.value_ms for m in call.stage_measurements if m.stage.value == "e2e"]
    assert 580 in samples
    assert 900 in samples
    assert all(value > 1 for value in samples)
    aggregates = [a.value_ms for a in call.aggregate_measurements if a.stage.value == "e2e"]
    assert 620 in aggregates or any(a.statistic.value == "p50" and a.value_ms == 620 for a in call.aggregate_measurements)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    first_user = next(t for t in call.turns if t.speaker.value == "user")
    assert first_user.started_at is not None
    assert call.started_at is not None
    offset_ms = (first_user.started_at - call.started_at).total_seconds() * 1000
    assert 2000 <= offset_ms <= 3000


def test_process_envelope_expands_gzip_before_decode() -> None:
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="gzip-1",
        content_sha256="x",
        body=gzip.compress(raw),
        headers={"content-encoding": "gzip"},
        received_at=utcnow(),
        source_call_id="ex-1",
    )
    revision = process_envelope(
        envelope,
        ExamplePlugin(),
        declaration=fidelity_declaration(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        decoder_version="example/1",
        source="example",
    )
    assert revision.turns
    assert revision.source_call_id


def test_gzip_receive_then_drain_assembles() -> None:
    state = example_state()
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    wire = gzip.compress(raw)
    headers = example_headers(wire)
    headers["content-encoding"] = "gzip"
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=wire,
        headers=RawHeaders.from_mapping(headers),
        resolver=state.resolver,
        plugin=ExamplePlugin(),
        objects=state.objects,
        inbox=state.inbox,
        content_encoding="gzip",
    )
    assert result.created is True
    assert result.envelope is not None
    assert result.envelope.body == wire
    drain_once(state)
    assert active_calls(state, "acme")
