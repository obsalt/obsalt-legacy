"""Retell tools get a coarse utterance anchor, not a fabricated duration."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.domain.enums import Provenance
from obsalt.domain.events import ToolObserved
from obsalt.plugin.types import RawEnvelope
from obsalt.util import new_id, utcnow
from obsalt_retell.plugin import RetellPlugin
from tests.helpers import RETELL_FIXTURES


def test_retell_tool_uses_coarse_utterance_anchor() -> None:
    plugin = RetellPlugin()
    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider="retell",
        connection_id="c1",
        object_key="k",
        delivery_key="fixture",
        content_sha256="x",
        body=(RETELL_FIXTURES / "raw" / "call_ended.json").read_bytes(),
        received_at=utcnow(),
    )
    events = list(plugin.decode(envelope))
    tool = next(
        event
        for event in events
        if isinstance(event, ToolObserved) and event.name == "lookup_invoice"
    )
    assert tool.started_at == datetime(2025, 8, 21, 12, 0, 2, 300000, tzinfo=UTC)
    assert tool.ended_at is None
    stamp = tool.provenance_by_field["started_at"]
    assert stamp.provenance is Provenance.OBSALT_DERIVED
    assert "duration not reported" in (stamp.derivation or "")
