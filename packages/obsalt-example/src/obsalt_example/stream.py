"""Example StreamSource so Deepgram remains additive — no core change required."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope


class ExampleStreamSource:
    async def frames(self, cfg: ConnectionConfig) -> AsyncIterator[RawEnvelope]:
        yield RawEnvelope(
            envelope_id=str(uuid4()),
            org_id=cfg.org_id,
            provider="example",
            connection_id=cfg.connection_id,
            object_key="stream/example",
            body=b'{"event":"frame","call_id":"stream-1"}',
            delivery_key="stream:stream-1",
            received_at=datetime.now(UTC).isoformat(),
        )
