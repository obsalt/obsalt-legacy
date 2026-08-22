"""Small tests: inbox DLQ after repeated decode failures."""

from __future__ import annotations

from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.testing.fakes import MemoryInbox
from obsalt.util import utcnow


def test_decode_dlq_after_eight_failures() -> None:
    inbox = MemoryInbox()
    envelope = RawEnvelope(
        envelope_id="e-fail",
        org_id="acme",
        provider="example",
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        received_at=utcnow(),
    )
    inbox.accept(envelope, tombstone_hints=TombstoneHints())
    for _ in range(7):
        inbox.mark_failed(envelope.envelope_id, "decode failed")
        assert envelope.envelope_id not in {row["envelope_id"] for row in inbox.dlq}
        assert envelope.envelope_id in inbox.outbox
    inbox.mark_failed(envelope.envelope_id, "decode failed")
    assert inbox.dlq[-1]["envelope_id"] == envelope.envelope_id
    assert envelope.envelope_id not in inbox.outbox
