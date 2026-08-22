"""Property tests: any permutation or duplicate delivery folds to the same candidate."""

from __future__ import annotations

import itertools
from datetime import date

from obsalt.assemble.assembler import Assembler, fold_facts
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import MemoryPointerStore, promote
from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, SnapshotBoundaryObserved, StageObserved, TurnObserved
from obsalt.domain.models import FidelityDeclaration
from obsalt.worker.process import MemoryRevisionSink


def _decl() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION, Signal.TURN_INTERVAL}),
        structurally_absent={},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def _catalog() -> dict[str, object]:
    return {
        "call": CallObserved(source_call_id="c1", agent_id="a"),
        "turn": TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        "stt": StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=140,
            turn_index=0,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="latency.stt",
        ),
    }


def test_fold_facts_is_permutation_and_duplicate_idempotent() -> None:
    catalog = _catalog()
    names = ["call", "turn", "stt"]
    hashes = set()
    for order in itertools.permutations(names):
        events = [catalog[name] for name in order]
        accepted, conflicts, _ = fold_facts([*events, *events])
        assert conflicts == []
        hashes.add(frozenset(record.content_hash for record in accepted.values()))
    assert len(hashes) == 1


def test_snapshot_retracts_omitted_authoritative_facts() -> None:
    accepted, conflicts, _ = fold_facts(
        [
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="old"),
            TurnObserved(turn_index=1, speaker=Speaker.AGENT, text="gone"),
            SnapshotBoundaryObserved(authoritative_domains=["turn_observed"]),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="new"),
        ]
    )
    assert conflicts == []
    turns = [record.event for record in accepted.values() if isinstance(record.event, TurnObserved)]
    assert len(turns) == 1
    assert turns[0].text == "new"


def test_conflicting_agent_ids_block_promotion() -> None:
    assembler = Assembler(_decl(), decoder_version="t/1", processing_run_id="r")
    events = [
        stamp_event(
            CallObserved(source_call_id="c1", agent_id="a"),
            org_id="o",
            call_key="cid",
            envelope_id="e1",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=0,
        ),
        stamp_event(
            CallObserved(source_call_id="c1", agent_id="b"),
            org_id="o",
            call_key="cid",
            envelope_id="e2",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=1,
        ),
    ]
    candidate = assembler.assemble("o", "cid", "test", events)
    assert candidate.conflicts
    pointers = MemoryPointerStore()
    sink = MemoryRevisionSink()
    sink.write(candidate)
    result = promote(pointers, candidate, expected=None, fact_frontier=frozenset(candidate.accepted_fact_ids))
    assert result.promoted is False
    assert result.conflict is True
    assert pointers.get("o", "cid") is None
