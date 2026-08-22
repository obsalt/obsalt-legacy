"""Phase 6 hosted plugins: receive → drain → query. Units, T1, delivery identity."""

from __future__ import annotations

import json

from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import MeasurementPlacement, TimelineFidelity
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.query import active_calls
from obsalt.worker.drain import drain_once
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_example.plugin import ExamplePlugin

from tests.helpers import (
    CARTESIA_FIXTURES,
    ELEVEN_FIXTURES,
    EXAMPLE_FIXTURES,
    cartesia_headers,
    cartesia_state,
    elevenlabs_headers,
    elevenlabs_state,
    example_headers,
    example_state,
)


def test_example_stt_duration_is_an_unplaced_chip_after_drain() -> None:
    state = example_state()
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(example_headers(raw)),
        resolver=state.resolver,
        plugin=ExamplePlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    drain_once(state)
    call = active_calls(state, "acme")[0]
    assert call.timeline_fidelity is TimelineFidelity.TURN_LEVEL
    assert all(m.placement is MeasurementPlacement.UNPLACED for m in call.stage_measurements)
    assert all(m.started_at is None and m.ended_at is None for m in call.stage_measurements)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["unplaced_stage_chips"]
    assert any(g.kind.value == "user_text" for g in call.grounding)


def test_elevenlabs_post_call_is_message_level_without_a_waterfall() -> None:
    state = elevenlabs_state()
    raw = (ELEVEN_FIXTURES / "raw" / "post_call_transcription.json").read_bytes()
    result = receive_webhook(
        provider="elevenlabs",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(elevenlabs_headers(raw)),
        resolver=state.resolver,
        plugin=ElevenLabsPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    drain_once(state)
    call = active_calls(state, "acme")[0]
    assert call.source_call_id == "el-conv-refund-1"
    assert call.timeline_fidelity is TimelineFidelity.MESSAGE_LEVEL
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert all(turn.ended_at is None for turn in call.turns)
    assert any(g.kind.value == "user_text" for g in call.grounding)


def test_elevenlabs_transcription_and_audio_do_not_dedupe() -> None:
    state = elevenlabs_state()
    plugin = ElevenLabsPlugin()
    transcription = (ELEVEN_FIXTURES / "raw" / "post_call_transcription.json").read_bytes()
    audio = json.dumps(
        {
            "type": "post_call_audio",
            "data": {"conversation_id": "el-conv-refund-1", "agent_id": "support-el"},
        }
    ).encode()
    first = receive_webhook(
        provider="elevenlabs",
        ingest_key="ik",
        raw=transcription,
        headers=RawHeaders.from_mapping(elevenlabs_headers(transcription)),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    second = receive_webhook(
        provider="elevenlabs",
        ingest_key="ik",
        raw=audio,
        headers=RawHeaders.from_mapping(elevenlabs_headers(audio)),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    assert first.created is True and second.created is True
    assert first.envelope is not None and second.envelope is not None
    assert first.envelope.delivery_key != second.envelope.delivery_key
    assert first.envelope.delivery_key.startswith("post_call_transcription:")
    assert second.envelope.delivery_key.startswith("post_call_audio:")


def test_cartesia_turn_intervals_stay_off_the_stage_waterfall() -> None:
    state = cartesia_state()
    raw = (CARTESIA_FIXTURES / "raw" / "call_ended.json").read_bytes()
    result = receive_webhook(
        provider="cartesia",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(cartesia_headers()),
        resolver=state.resolver,
        plugin=CartesiaPlugin(),
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.created is True
    drain_once(state)
    call = active_calls(state, "acme")[0]
    assert call.source_call_id == "line-call-1"
    assert call.timeline_fidelity is TimelineFidelity.TURN_LEVEL
    assert all(m.placement is MeasurementPlacement.UNPLACED for m in call.stage_measurements)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert all(turn.started_at and turn.ended_at for turn in call.turns)
