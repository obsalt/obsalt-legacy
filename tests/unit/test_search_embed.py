"""Memory search must embed from sync and async call sites."""

from __future__ import annotations

import asyncio

from obsalt.domain.enums import Speaker
from obsalt.domain.models import CallRevision, Turn
from obsalt.search.index import MemorySearchIndex


def _call() -> CallRevision:
    from datetime import UTC, datetime

    return CallRevision(
        org_id="acme",
        call_id="search-1",
        revision="r1",
        source="example",
        source_call_id="ex-search",
        started_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="customer asking about refunds")],
    )


def test_memory_index_query_works_inside_a_running_event_loop() -> None:
    index = MemorySearchIndex()
    index.index(_call())

    async def _go() -> list[dict]:
        return list(index.query("refunds", filters={"org_id": "acme"}).get("items") or [])

    hits = asyncio.run(_go())
    assert any(item["call_id"] == "search-1" for item in hits)
