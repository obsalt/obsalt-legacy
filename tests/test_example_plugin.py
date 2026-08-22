from __future__ import annotations

from pathlib import Path

from obsalt.crypto.primitives import hmac_hex
from obsalt.domain.enums import Capability, ObservationalEventKind, VerifyOutcome
from obsalt.domain.events import CallObserved, GroundingObserved, StageObserved, TurnObserved
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.plugin.host import discover_plugins
from obsalt.plugin.types import ConnectionConfig
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit import (
    AuthenticationConformanceTests,
    DecoderConformanceTests,
    RestBackfillConformanceTests,
    SchemaFixtureTests,
    StreamSourceConformanceTests,
    WebhookConformanceTests,
)

FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "obsalt-example" / "src" / "obsalt_example" / "fixtures"


class TestExampleDecoder(DecoderConformanceTests):
    plugin = ExamplePlugin()
    fixtures_dir = FIXTURES


class TestExampleSchema(SchemaFixtureTests):
    plugin = ExamplePlugin()
    fixtures_dir = FIXTURES


class TestExampleStream(StreamSourceConformanceTests):
    plugin = ExamplePlugin()
    connection = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={},
        settings={"emit_example_frame": True},
    )


class TestExampleBackfill(RestBackfillConformanceTests):
    plugin = ExamplePlugin()
    connection = ConnectionConfig(
        org_id="acme",
        provider="example",
        connection_id="c1",
        ingest_key_hash="x",
        secrets={},
        settings={"backfill_items": [{"id": "bf-1", "hash": "h1", "ended_reason": "completed"}]},
    )


class TestExampleWebhook(WebhookConformanceTests):
    plugin = ExamplePlugin()
    valid_raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()


class TestExampleAuth(AuthenticationConformanceTests):
    plugin = ExamplePlugin()
    connection = ConnectionConfig(
        org_id="acme", provider="example", connection_id="c1", ingest_key_hash="x", secrets={"hmac_secret": "example-secret"}
    )
    valid_raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()

    @property
    def valid_headers(self) -> dict[str, str]:
        return {"x-obsalt-example-signature": hmac_hex("example-secret", self.valid_raw)}


def test_example_plugin_discovered_via_entry_points() -> None:
    plugins = discover_plugins()
    names = {p.name for p in plugins}
    assert "example" in names
    loaded = next(p for p in plugins if p.name == "example")
    assert Capability.WEBHOOK_SOURCE in loaded.capabilities
    assert loaded.api_version == 1


def test_example_decode_has_interval_stt_and_grounding() -> None:
    plugin = ExamplePlugin()
    events = list(plugin.decode(_envelope(FIXTURES / "raw" / "call_ended.json")))
    assert any(isinstance(e, CallObserved) and e.source_call_id == "ex-1" for e in events)
    assert any(isinstance(e, GroundingObserved) for e in events)
    assert any(isinstance(e, TurnObserved) for e in events)
    stt = next(e for e in events if isinstance(e, StageObserved))
    assert stt.started_at is not None and stt.ended_at is not None
    assert stt.value_ms == 180


def test_example_signed_webhook_accepts_and_dedupes() -> None:
    plugin = ExamplePlugin()
    raw = (FIXTURES / "raw" / "call_ended.json").read_bytes()
    secret = "example-secret"
    sig = hmac_hex(secret, raw)
    cfg = ConnectionConfig(org_id="acme", provider="example", connection_id="c1", ingest_key_hash="x", secrets={"hmac_secret": secret})
    resolver = MemoryResolver()
    resolver.add(cfg, "ik")
    objects = MemoryObjectStore()
    inbox = MemoryInbox()
    headers = RawHeaders.from_mapping({"x-obsalt-example-signature": sig, "content-type": "application/json"})
    first = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=headers,
        resolver=resolver,
        plugin=plugin,
        objects=objects,
        inbox=inbox,
    )
    assert first.response.status_code == 200
    assert first.created is True
    assert first.envelope is not None
    second = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=headers,
        resolver=resolver,
        plugin=plugin,
        objects=objects,
        inbox=inbox,
    )
    assert second.created is False
    assert second.envelope is not None
    assert second.envelope.envelope_id == first.envelope.envelope_id


def test_example_missing_secret_fail_closed() -> None:
    plugin = ExamplePlugin()
    raw = b'{"call_id":"x"}'
    cfg = ConnectionConfig(org_id="acme", provider="example", connection_id="c1", ingest_key_hash="x", secrets={})
    result = plugin.authenticate(raw, RawHeaders.from_mapping({"x-obsalt-example-signature": "ab"}).as_list(), cfg)
    assert result.outcome is VerifyOutcome.MISSING_CREDENTIAL


def test_example_rejects_synchronous_callbacks() -> None:
    plugin = ExamplePlugin()
    assert plugin.classify(b'{"type":"assistant-request","call_id":"x"}') is ObservationalEventKind.REJECTED_SYNCHRONOUS


def _envelope(path: Path):
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    return RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        body=path.read_bytes(),
        received_at=utcnow(),
    )
