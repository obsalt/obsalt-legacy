"""Assembler property tests — §5.3 / §13.4."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

from obsalt.analysis.tier1 import analyze_tier1
from obsalt.assemble.assembler import Assembler, FactRecord, fold_facts
from obsalt.assemble.facts import fact_id_for
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.assemble.rehydrate import events_from_revision
from obsalt.domain.enums import Speaker, ToolStatus
from obsalt.domain.events import (
    CallObserved,
    SnapshotBoundaryObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.worker.process import MemoryRevisionSink, process_normalized_events

from tests.helpers import fidelity_declaration


def test_fold_is_permutation_and_duplicate_idempotent() -> None:
    events = [
        CallObserved(source_call_id="c1", agent_id="a"),
        TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="hello"),
    ]
    baseline, conflicts, _ = fold_facts(events)
    assert conflicts == []
    for perm in permutations(events):
        accepted, more_conflicts, _ = fold_facts([*perm, *perm])
        assert more_conflicts == []
        assert set(accepted) == set(baseline)
        assert {r.content_hash for r in accepted.values()} == {r.content_hash for r in baseline.values()}


def test_conflicting_content_is_commutative() -> None:
    left = CallObserved(source_call_id="c1", agent_id="zeta")
    right = CallObserved(source_call_id="c1", agent_id="alpha")
    assert fact_id_for(left) == fact_id_for(right)
    first, conflicts_a, _ = fold_facts([left, right])
    second, conflicts_b, _ = fold_facts([right, left])
    fact_id = fact_id_for(left)
    assert fact_id in conflicts_a and fact_id in conflicts_b
    assert first[fact_id].content_hash == second[fact_id].content_hash
    winner = min(FactRecord(left).content_hash, FactRecord(right).content_hash)
    assert first[fact_id].content_hash == winner


def test_snapshot_retracts_only_declared_domains() -> None:
    accepted, conflicts, _ = fold_facts(
        [
            CallObserved(source_call_id="c1", agent_id="a"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="old"),
            TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="gone"),
            ToolObserved(tool_id="t1", name="lookup", status=ToolStatus.SUCCESS),
            SnapshotBoundaryObserved(authoritative_domains=["turn_observed"]),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="new"),
        ]
    )
    assert conflicts == []
    turns = [r.event for r in accepted.values() if isinstance(r.event, TurnObserved)]
    tools = [r.event for r in accepted.values() if isinstance(r.event, ToolObserved)]
    calls = [r.event for r in accepted.values() if isinstance(r.event, CallObserved)]
    assert [t.text for t in turns] == ["new"]
    assert len(tools) == 1
    assert calls[0].agent_id == "a"


def test_delta_without_snapshot_does_not_retract_by_omission() -> None:
    accepted, conflicts, _ = fold_facts(
        [
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="keep me"),
            TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="also keep"),
            TurnObserved(turn_index=2, speaker=Speaker.USER, text="late delta only"),
        ]
    )
    assert conflicts == []
    texts = {r.event.text for r in accepted.values() if isinstance(r.event, TurnObserved)}
    assert texts == {"keep me", "also keep", "late delta only"}


def test_same_turn_index_different_started_at_are_distinct_facts() -> None:
    early = datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC)
    later = datetime(2026, 8, 21, 12, 0, 8, tzinfo=UTC)
    accepted, conflicts, _ = fold_facts(
        [
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="first", started_at=early),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="second", started_at=later),
        ]
    )
    assert conflicts == []
    turns = [r.event for r in accepted.values() if isinstance(r.event, TurnObserved)]
    assert {t.text for t in turns} == {"first", "second"}


def test_assemble_sorts_turns_by_started_at_not_arrival() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    later = datetime(2026, 8, 21, 12, 0, 8, tzinfo=UTC)
    early = datetime(2026, 8, 21, 12, 0, 1, tzinfo=UTC)
    revision = assembler.assemble(
        "o",
        "cid",
        "test",
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="later", started_at=later),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="earlier", started_at=early),
        ],
    )
    assert [t.text for t in revision.turns] == ["earlier", "later"]


def test_rehydrate_keeps_redacted_tool_payloads_across_late_fold() -> None:
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    first = process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="a"),
            ToolObserved(
                tool_id="lookup-1",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                args={"order_id": "INV-1"},
                result={"amount": "$20.00"},
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    assert first.tools[0].args == {"order_id": "INV-1"}
    assert first.tools[0].result == {"amount": "$20.00"}
    rebuilt = events_from_revision(first)
    tools = [e for e in rebuilt if isinstance(e, ToolObserved)]
    assert tools[0].args == {"order_id": "INV-1"}
    assert tools[0].result == {"amount": "$20.00"}
    second = process_normalized_events(
        [CallObserved(source_call_id="c1", agent_id="a")],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e2",
        declaration=fidelity_declaration(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    assert second.tools[0].args == {"order_id": "INV-1"}
    assert second.tools[0].result == {"amount": "$20.00"}


def test_tier1_does_not_mutate_the_revision() -> None:
    assembler = Assembler(fidelity_declaration(), decoder_version="t/1", processing_run_id="r")
    revision = assembler.assemble(
        "o",
        "cid",
        "test",
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="bye"),
        ],
    )
    before = revision.model_dump()
    analyze_tier1(revision)
    assert revision.model_dump() == before


def test_analysis_is_written_for_the_promoted_revision() -> None:
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    promoted = process_normalized_events(
        [
            CallObserved(source_call_id="c1", agent_id="a"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="I want a refund"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=pointers,
        sink=sink,
        decoder_version="t/1",
    )
    rows = sink.list_analysis("acme", promoted.call_id, promoted.revision)
    assert rows
    assert all(row.execution.revision == promoted.revision for row in rows)
    assert pointers.get("acme", promoted.call_id) == promoted.revision
