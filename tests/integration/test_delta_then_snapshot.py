"""§5.3: delta events must not retract by omission; snapshots may."""

from __future__ import annotations

import json

from obsalt.domain.events import SnapshotBoundaryObserved, TurnObserved
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.query import active_calls
from obsalt.worker.drain import drain_once
from obsalt_vapi.plugin import VapiPlugin

from tests.helpers import VAPI_FIXTURES, vapi_headers, vapi_state


def _transcript_delta(*, call_id: str, text: str, seconds_from_start: float, time_ms: int) -> bytes:
    return json.dumps(
        {
            "message": {
                "type": "transcript",
                "timestamp": "2026-08-21T12:00:05.000Z",
                "startedAt": "2026-08-21T12:00:00.000Z",
                "call": {
                    "id": call_id,
                    "type": "inboundPhoneCall",
                    "assistantId": "support-agent",
                    "customer": {"number": "+15551230001"},
                },
                "messages": [
                    {
                        "role": "user",
                        "message": text,
                        "secondsFromStart": seconds_from_start,
                        "duration": 2.1,
                        "time": time_ms,
                    }
                ],
            }
        }
    ).encode()


def _ingest(state, raw: bytes) -> None:
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
    drain_once(state)


def test_vapi_transcript_delta_does_not_emit_snapshot() -> None:
    plugin = VapiPlugin()
    raw = _transcript_delta(
        call_id="vapi-call-refund-1",
        text="I want a refund for my order.",
        seconds_from_start=3.0,
        time_ms=1755777603000,
    )
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    events = list(
        plugin.decode(
            RawEnvelope(
                envelope_id=new_id(),
                org_id="acme",
                provider="vapi",
                connection_id="c1",
                object_key="k",
                delivery_key="d",
                content_sha256="x",
                body=raw,
                received_at=utcnow(),
            )
        )
    )
    assert not any(isinstance(event, SnapshotBoundaryObserved) for event in events)
    assert any(isinstance(event, TurnObserved) and event.text.startswith("I want a refund") for event in events)


def test_snapshot_then_delta_keeps_omitted_turns() -> None:
    state = vapi_state()
    eoc = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    _ingest(state, eoc)
    after_snapshot = active_calls(state, "acme")[0]
    snapshot_texts = [turn.text for turn in after_snapshot.turns]
    assert len(snapshot_texts) >= 3
    delta = _transcript_delta(
        call_id="vapi-call-refund-1",
        text="one more thing about the refund",
        seconds_from_start=12.0,
        time_ms=1755777612000,
    )
    _ingest(state, delta)
    after_delta = active_calls(state, "acme")[0]
    texts = [turn.text for turn in after_delta.turns]
    for previous in snapshot_texts:
        assert previous in texts
    assert "one more thing about the refund" in texts


def test_delta_then_snapshot_replaces_authoritative_turns() -> None:
    state = vapi_state()
    delta = _transcript_delta(
        call_id="vapi-call-refund-1",
        text="partial live transcript only",
        seconds_from_start=1.0,
        time_ms=1755777601000,
    )
    _ingest(state, delta)
    mid = active_calls(state, "acme")[0]
    assert any(turn.text == "partial live transcript only" for turn in mid.turns)
    eoc = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    _ingest(state, eoc)
    final = active_calls(state, "acme")[0]
    texts = [turn.text for turn in final.turns]
    assert "partial live transcript only" not in texts
    assert any("refund" in turn.text.lower() for turn in final.turns)
    assert len(final.turns) >= 3
