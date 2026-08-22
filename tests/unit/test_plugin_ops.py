"""Small tests: plugin capability declarations and hosted grounding."""

from __future__ import annotations

from obsalt.domain.enums import Capability
from obsalt.domain.events import GroundingObserved
from obsalt.plugin.types import BackfillCursor, BackfillItem, ConnectionConfig
from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit import decode_raw_fixture
from obsalt_vapi.plugin import VapiPlugin
from tests.helpers import CARTESIA_FIXTURES, ELEVEN_FIXTURES


def test_vapi_backfill_without_key_is_truncated() -> None:
    page = VapiPlugin().scan(
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
    assert any(
        isinstance(event, GroundingObserved) and "refund" in event.content.lower()
        for event in eleven
    )
    cartesia = decode_raw_fixture(
        CartesiaPlugin(),
        CARTESIA_FIXTURES / "raw" / "call_ended.json",
    )
    assert any(isinstance(event, GroundingObserved) and event.content for event in cartesia)
