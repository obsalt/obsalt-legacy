"""Black-box decode and auth tests for Vapi against published field names."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from obsalt.assemble.promote import MemoryPointerStore
from obsalt.crypto.primitives import hmac_hex, jwt_hs256_sign
from obsalt.domain.enums import VerifyOutcome
from obsalt.domain.events import CallObserved, TurnObserved
from obsalt.ingest.headers import RawHeaders
from obsalt.plugin.types import BackfillItem, ConnectionConfig, RawEnvelope
from obsalt.util import new_id, utcnow
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events
from obsalt_vapi.plugin import VapiPlugin


def _decode_fold(plugin: VapiPlugin, bodies: list[dict]) -> tuple[object, object]:
    """Fold a sequence of Vapi server messages into one call, like the worker does."""
    decl = plugin.fidelity
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    call_id = None
    last = None
    for idx, body in enumerate(bodies):
        b = json.dumps(body).encode()
        env = RawEnvelope(
            envelope_id=f"env-{idx}",
            org_id="acme",
            provider="vapi",
            connection_id="c1",
            object_key="k",
            delivery_key=f"del-{idx}",
            content_sha256="x",
            body=b,
            received_at=utcnow(),
        )
        events = list(plugin.decode(env))
        source = body["message"]["call"]["id"]
        call_id = call_id or source
        last = process_normalized_events(
            events,
            org_id="acme",
            source="vapi",
            source_call_id=source,
            envelope_id=f"env-{idx}",
            declaration=decl,
            pointers=pointers,
            sink=sink,
            decoder_version=plugin.decoder_version,
        )
    active = pointers.get("acme", last.call_id)
    return sink.get("acme", last.call_id, active), last


VAPI_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "obsalt-vapi"
    / "src"
    / "obsalt_vapi"
    / "fixtures"
)


def _env(body: bytes) -> RawEnvelope:
    return RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        body=body,
        received_at=utcnow(),
    )


def test_vapi_prefers_seconds_from_start_over_epoch_time() -> None:
    plugin = VapiPlugin()
    raw = json.loads((VAPI_FIXTURES / "raw" / "end_of_call.json").read_text())
    events = list(plugin.decode(_env((VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes())))
    first = next(event for event in events if isinstance(event, TurnObserved))
    assert first.started_at == datetime(2026, 8, 21, 12, 0, 0, 400000, tzinfo=UTC)
    assert (
        first.provenance_by_field["started_at"].source_path
        == "artifact.messages[].secondsFromStart"
    )
    call = next(event for event in events if isinstance(event, CallObserved))
    assert call.from_number == raw["message"]["call"]["customer"]["number"]


def test_vapi_falls_back_to_epoch_time_without_seconds_from_start() -> None:
    plugin = VapiPlugin()
    payload = json.loads((VAPI_FIXTURES / "raw" / "end_of_call.json").read_text())
    for message in payload["message"]["artifact"]["messages"]:
        message.pop("secondsFromStart", None)
    events = list(plugin.decode(_env(json.dumps(payload).encode())))
    first = next(event for event in events if isinstance(event, TurnObserved))
    assert first.started_at == datetime(2025, 8, 21, 12, 0, 0, 400000, tzinfo=UTC)
    assert first.provenance_by_field["started_at"].source_path == "artifact.messages[].time"


def test_vapi_bearer_hmac_and_oauth2() -> None:
    plugin = VapiPlugin()
    raw = (VAPI_FIXTURES / "raw" / "end_of_call.json").read_bytes()
    bearer = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"bearer_token": "tok"},
        settings={"auth_mode": "bearer"},
    )
    assert plugin.authenticate(
        raw, RawHeaders.from_mapping({"authorization": "Bearer tok"}).as_list(), bearer
    ).ok
    missing = plugin.authenticate(
        raw,
        RawHeaders.from_mapping({"authorization": "Bearer tok"}).as_list(),
        bearer.model_copy(update={"secrets": {}}),
    )
    assert missing.outcome is VerifyOutcome.MISSING_CREDENTIAL

    hmac_cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"hmac_secret": "hs"},
        settings={"auth_mode": "hmac", "timestamp_header": "x-vapi-timestamp"},
    )
    ts = str(int(time.time()))
    sig = hmac_hex("hs", ts.encode() + raw)
    assert plugin.authenticate(
        raw,
        RawHeaders.from_mapping({"x-vapi-signature": sig, "x-vapi-timestamp": ts}).as_list(),
        hmac_cfg,
    ).ok
    stale_ts = str(int(time.time()) - 20 * 60)
    stale_sig = hmac_hex("hs", stale_ts.encode() + raw)
    stale = plugin.authenticate(
        raw,
        RawHeaders.from_mapping(
            {"x-vapi-signature": stale_sig, "x-vapi-timestamp": stale_ts}
        ).as_list(),
        hmac_cfg,
    )
    assert not stale.ok

    secret = "oauth-secret"
    token = jwt_hs256_sign({"sub": "vapi", "exp": time.time() + 60}, secret)
    oauth = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={"oauth_token": secret},
        settings={"auth_mode": "oauth2"},
    )
    assert plugin.authenticate(b"{}", [(b"authorization", f"Bearer {token}".encode())], oauth).ok
    expired = jwt_hs256_sign({"sub": "vapi", "exp": time.time() - 10}, secret)
    bad = plugin.authenticate(b"{}", [(b"authorization", f"Bearer {expired}".encode())], oauth)
    assert bad.outcome is VerifyOutcome.BAD_SIGNATURE
    assert not plugin.authenticate(b"{}", [(b"authorization", b"Bearer oauth-secret")], oauth).ok


def test_vapi_legacy_secret_accepts_bearer_and_ignores_empty_x_vapi_secret() -> None:
    plugin = VapiPlugin()
    raw = b'{"message":{"type":"end-of-call-report","call":{"id":"x"}}}'
    cfg = ConnectionConfig(
        org_id="acme",
        provider="vapi",
        connection_id="c",
        ingest_key_hash="x",
        secrets={"legacy_secret": "vapi-secret"},
        settings={"auth_mode": "legacy_secret"},
    )
    bearer = plugin.authenticate(
        raw, RawHeaders.from_mapping({"authorization": "Bearer vapi-secret"}).as_list(), cfg
    )
    assert bearer.ok
    leftover = plugin.authenticate(
        raw,
        RawHeaders.from_mapping(
            {"x-vapi-secret": "", "authorization": "Bearer vapi-secret"}
        ).as_list(),
        cfg,
    )
    assert leftover.ok
    empty_only = plugin.authenticate(
        raw, RawHeaders.from_mapping({"x-vapi-secret": ""}).as_list(), cfg
    )
    assert empty_only.outcome is VerifyOutcome.MALFORMED


def test_vapi_hydrate_wraps_list_call_object() -> None:
    plugin = VapiPlugin()
    cfg = ConnectionConfig(org_id="acme", provider="vapi", connection_id="c1", ingest_key_hash="x")
    envelope = plugin.hydrate(
        cfg,
        BackfillItem(
            upstream_entity_id="vapi-call-1",
            payload={
                "id": "vapi-call-1",
                "endedReason": "hangup",
                "artifact": {"transcript": "hi"},
            },
        ),
    )
    body = json.loads(envelope.body or b"{}")
    assert body["message"]["type"] == "end-of-call-report"
    assert body["message"]["call"]["id"] == "vapi-call-1"
    assert body["message"]["endedReason"] == "hangup"
    events = list(plugin.decode(envelope))
    assert any(
        isinstance(event, CallObserved) and event.source_call_id == "vapi-call-1"
        for event in events
    )


def test_live_events_then_end_of_call_report_promotes_rich_revision() -> None:
    """Regression: a flood of live conversation-update events must not block the
    end-of-call-report from promoting. The assembler derives a lifecycle
    ``started_at`` from the live turns; if that derived value is round-tripped
    back onto the CallObserved fact it hard-conflicts with the provider-reported
    ``startedAt`` on the report, leaving a sparse live revision as active.

    See obsalt_vapi trace: live speech/conversation updates + end-of-call-report.
    """
    plugin = VapiPlugin()
    cid = "call-regression-1"
    live = {
        "message": {
            "type": "conversation-update",
            "call": {"id": cid},
            "artifact": {
                "messages": [
                    {"role": "bot", "message": "Hi there.", "time": 1000000},
                    {"role": "user", "message": "Hello.", "time": 1001000},
                ]
            },
        }
    }
    eoc = {
        "message": {
            "type": "end-of-call-report",
            "startedAt": "1970-01-01T00:00:02.000Z",
            "endedAt": "1970-01-01T00:00:30.000Z",
            "endedReason": "customer-ended-call",
            "call": {"id": cid, "type": "webCall"},
            "artifact": {
                "messages": [
                    {"role": "bot", "message": "Hi there.", "time": 1000000},
                    {"role": "user", "message": "Hello.", "time": 1001000},
                    {"role": "bot", "message": "Bye.", "time": 1002000},
                ],
                "performanceMetrics": {
                    "turnLatencies": [
                        {
                            "modelLatency": 100,
                            "voiceLatency": 50,
                            "transcriberLatency": 20,
                            "endpointingLatency": 5,
                            "turnLatency": 175,
                        }
                    ],
                    "modelLatencyAverage": 100,
                },
            },
        }
    }
    active, _last = _decode_fold(plugin, [live, live, eoc])
    assert active is not None
    assert len(active.turns) == 3
    assert active.hangup is not None and active.hangup.reason.value == "user_hangup"
    assert len(active.stage_measurements) >= 1
    assert len(active.aggregate_measurements) >= 1


def test_live_transcript_evidence_then_end_of_call_report_promotes() -> None:
    """Regression: Vapi live events also carry an ``artifact.transcript`` string,
    which the decoder emits as ``EvidenceObserved``. That fact shares an identity
    with the end-of-call report's full ``EvidenceObserved`` (same kind + uri,
    different text) and was not in the snapshot's retracted domains, so it
    hard-conflicted and blocked the report. The report must win.
    """
    plugin = VapiPlugin()
    cid = "call-evidence-1"
    live = {
        "message": {
            "type": "conversation-update",
            "call": {"id": cid},
            "artifact": {
                "transcript": "AI: Hi\nUser: Hello",
                "messages": [
                    {"role": "bot", "message": "Hi there.", "time": 1000000},
                    {"role": "user", "message": "Hello.", "time": 1001000},
                ],
            },
        }
    }
    eoc = {
        "message": {
            "type": "end-of-call-report",
            "startedAt": "1970-01-01T00:00:02.000Z",
            "endedAt": "1970-01-01T00:00:30.000Z",
            "endedReason": "customer-ended-call",
            "call": {"id": cid, "type": "webCall"},
            "artifact": {
                "transcript": "AI: Hi there.\nUser: Hello.\nAI: Bye.",
                "messages": [
                    {"role": "bot", "message": "Hi there.", "time": 1000000},
                    {"role": "user", "message": "Hello.", "time": 1001000},
                    {"role": "bot", "message": "Bye.", "time": 1002000},
                ],
            },
        }
    }
    active, _last = _decode_fold(plugin, [live, live, eoc])
    assert active is not None
    assert len(active.turns) == 3
    assert len(active.evidence) >= 1
    assert active.hangup is not None and active.hangup.reason.value == "user_hangup"
