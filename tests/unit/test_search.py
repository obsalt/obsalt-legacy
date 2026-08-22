"""Small tests: hybrid search over redacted content, with a required time range."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from obsalt.domain.enums import Speaker
from obsalt.domain.models import CallRevision, Turn
from obsalt.query import search_calls
from obsalt.search.hybrid import rrf
from obsalt.search.index import MemorySearchIndex


def _call(
    *,
    org_id: str = "acme",
    call_id: str,
    text: str,
    started_at: datetime,
    agent_id: str = "support",
) -> CallRevision:
    return CallRevision(
        org_id=org_id,
        call_id=call_id,
        revision="r1",
        source="example",
        source_call_id=f"src-{call_id}",
        agent_id=agent_id,
        started_at=started_at,
        turns=[Turn(index=0, speaker=Speaker.USER, text=text)],
    )


def test_search_is_org_scoped() -> None:
    acme = _call(
        org_id="acme",
        call_id="acme-refund",
        text="customer asking about refunds",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    other = _call(
        org_id="beta",
        call_id="beta-refund",
        text="customer asking about refunds",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    index = MemorySearchIndex()
    index.index(acme)
    index.index(other)
    hits = search_calls(None, "refunds", index=index, org_id="acme")
    ids = {item["call_id"] for item in hits}
    assert "acme-refund" in ids
    assert "beta-refund" not in ids


def test_search_restricts_to_the_caller_provided_calls() -> None:
    inside = _call(
        call_id="inside",
        text="customer asking about refunds",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )
    outside = _call(
        call_id="outside",
        text="customer asking about refunds",
        started_at=datetime(2019, 1, 1, tzinfo=UTC),
    )
    index = MemorySearchIndex()
    index.index(inside)
    index.index(outside)
    hits = search_calls([inside], "refunds", index=index, org_id="acme")
    ids = {item["call_id"] for item in hits}
    assert ids == {"inside"}
    empty = search_calls([], "refunds", index=index, org_id="acme")
    assert empty == []


def test_index_time_filters_fall_back_to_created_at() -> None:
    call = CallRevision(
        org_id="acme",
        call_id="ingest-only",
        revision="r1",
        source="example",
        source_call_id="src-ingest-only",
        agent_id="support",
        started_at=None,
        created_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text="customer asking about refunds")],
    )
    index = MemorySearchIndex()
    index.index(call)
    inside = index.query(
        "refunds",
        filters={
            "org_id": "acme",
            "start": datetime(2026, 1, 1, tzinfo=UTC),
            "end": datetime(2026, 12, 31, tzinfo=UTC),
        },
    )
    assert [item["call_id"] for item in inside["items"]] == ["ingest-only"]
    outside = index.query(
        "refunds",
        filters={
            "org_id": "acme",
            "start": datetime(2019, 1, 1, tzinfo=UTC),
            "end": datetime(2019, 12, 31, tzinfo=UTC),
        },
    )
    assert outside["items"] == []


def test_index_time_filters_drop_calls_outside_the_window() -> None:
    early = _call(
        call_id="early",
        text="customer asking about refunds",
        started_at=datetime(2019, 6, 1, tzinfo=UTC),
    )
    late = _call(
        call_id="late",
        text="customer asking about refunds",
        started_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    index = MemorySearchIndex()
    index.index(early)
    index.index(late)
    result = index.query(
        "refunds",
        filters={
            "org_id": "acme",
            "start": datetime(2026, 1, 1, tzinfo=UTC),
            "end": datetime(2026, 12, 31, tzinfo=UTC),
        },
    )
    assert [item["call_id"] for item in result["items"]] == ["late"]


def test_rrf_promotes_agreement_and_refund_ranks_first() -> None:
    assert rrf(["a", "b"], ["b", "c"])[0] == "b"
    refund = _call(
        call_id="refund-call",
        text="customer asking about refunds",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    weather = _call(
        call_id="weather-call",
        text="hello how is the weather today",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        agent_id="other",
    )
    index = MemorySearchIndex()
    index.index(refund)
    index.index(weather)
    result = index.query("refunds")
    assert result["fused_ids"] == rrf(result["vector_ids"], result["lexical_ids"])
    assert result["fused_ids"][0] == "refund-call"


def test_memory_index_query_works_inside_a_running_event_loop() -> None:
    index = MemorySearchIndex()
    index.index(
        _call(
            call_id="search-1",
            text="customer asking about refunds",
            started_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
    )

    async def _go() -> list[dict]:
        return list(index.query("refunds", filters={"org_id": "acme"}).get("items") or [])

    hits = asyncio.run(_go())
    assert any(item["call_id"] == "search-1" for item in hits)
