"""Quality rollups, search tenancy, and deletion tombstones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obsalt.analysis.rollups import build_quality_rollup
from obsalt.domain.enums import AnalysisState
from obsalt.domain.models import AnalysisExecution, AnalysisResult, CallRevision
from obsalt.ingest.headers import RawHeaders
from obsalt.ingest.receive import receive_webhook
from obsalt.ops.privacy import apply_deletion
from obsalt.query import quality_rollup, search_calls
from obsalt.search.index import MemorySearchIndex
from obsalt.util import utcnow
from obsalt.worker.drain import drain_once
from obsalt_example.plugin import ExamplePlugin

from tests.conftest import EXAMPLE_FIXTURES, example_headers, example_state


def _call(*, org_id: str, call_id: str, revision: str, text: str) -> CallRevision:
    from obsalt.domain.enums import Speaker
    from obsalt.domain.models import Turn

    return CallRevision(
        org_id=org_id,
        call_id=call_id,
        revision=revision,
        source="example",
        source_call_id=f"src-{call_id}",
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        turns=[Turn(index=0, speaker=Speaker.USER, text=text)],
    )


def _result(
    call_id: str,
    revision: str,
    *,
    analyzer: str,
    state: AnalysisState,
    payload: dict,
) -> AnalysisResult:
    return AnalysisResult(
        execution=AnalysisExecution(
            call_id=call_id,
            revision=revision,
            analyzer_id=analyzer,
            analyzer_version="1",
            state=state,
        ),
        payload=payload,
    )


def test_pending_hallucination_candidates_are_not_fleet_failures() -> None:
    call = _call(org_id="acme", call_id="c1", revision="r2", text="refund")
    pending = _result(
        "c1",
        "r2",
        analyzer="hallucination",
        state=AnalysisState.PENDING,
        payload={"candidates": [{"kind": "price_claim", "needs_llm": True}], "selection": "pending"},
    )
    confirmed = _result(
        "c1",
        "r2",
        analyzer="hallucination",
        state=AnalysisState.COMPLETED,
        payload={"claims": [{"kind": "price_claim", "verdict": "contradicted"}]},
    )
    pending_only = build_quality_rollup([call], [pending], "g1")
    assert pending_only["hallucinations"]["count"] == 0
    confirmed_only = build_quality_rollup([call], [confirmed], "g1")
    assert confirmed_only["hallucinations"]["count"] == 1
    assert confirmed_only["hallucinations"]["by_kind"]["price_claim"] == 1


def test_quality_ignores_analysis_from_non_active_revisions() -> None:
    call = _call(org_id="acme", call_id="c1", revision="r-active", text="refund")
    stale = _result(
        "c1",
        "r-old",
        analyzer="hallucination",
        state=AnalysisState.COMPLETED,
        payload={"claims": [{"kind": "fabricated_id", "verdict": "unsupported"}]},
    )
    data = quality_rollup([call], [stale], as_of_generation="g1")
    assert data["hallucinations"]["count"] == 0
    assert data["flag_count"] == 0


def test_search_uses_populated_index_and_org_filter() -> None:
    acme = _call(org_id="acme", call_id="acme-refund", revision="r1", text="customer asking about refunds")
    other = _call(org_id="beta", call_id="beta-refund", revision="r1", text="customer asking about refunds")
    index = MemorySearchIndex()
    index.index(acme)
    index.index(other)
    hits = search_calls([], "refunds", index=index, org_id="acme")
    ids = {item["call_id"] for item in hits}
    assert "acme-refund" in ids
    assert "beta-refund" not in ids


def test_range_deletion_tombstones_later_ingest() -> None:
    state = example_state()
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    plugin = ExamplePlugin()
    now = utcnow()
    apply_deletion(state, org_id="acme", start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    result = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(example_headers(raw)),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    assert result.rejected == "tombstoned"
    drain_once(state)
    assert list(state.sink.revisions.values()) == []


def test_delete_by_call_id_tombs_provider_source_id_not_obsalt_uuid() -> None:
    state = example_state()
    raw = (EXAMPLE_FIXTURES / "raw" / "call_ended.json").read_bytes()
    plugin = ExamplePlugin()
    receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(example_headers(raw)),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    drain_once(state)
    stored = next(iter(state.sink.revisions.values()))
    apply_deletion(state, org_id="acme", call_id=stored.call_id)
    tombstoned_sources = {hints.source_call_id for _org, hints in state.inbox.tombstones}
    assert stored.source_call_id in tombstoned_sources
    assert stored.call_id not in tombstoned_sources or stored.call_id == stored.source_call_id
    replay = receive_webhook(
        provider="example",
        ingest_key="ik",
        raw=raw,
        headers=RawHeaders.from_mapping(example_headers(raw)),
        resolver=state.resolver,
        plugin=plugin,
        objects=state.objects,
        inbox=state.inbox,
    )
    assert replay.rejected == "tombstoned"
    drain_once(state)
    assert list(state.sink.revisions.values()) == []
