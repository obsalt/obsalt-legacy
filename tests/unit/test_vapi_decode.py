"""Black-box decode and auth tests for Vapi against published field names."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from obsalt.crypto.primitives import hmac_hex, jwt_hs256_sign
from obsalt.domain.enums import VerifyOutcome
from obsalt.domain.events import CallObserved, TurnObserved
from obsalt.ingest.headers import RawHeaders
from obsalt.plugin.types import BackfillItem, ConnectionConfig, RawEnvelope
from obsalt.util import new_id, utcnow
from obsalt_vapi.plugin import VapiPlugin

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
