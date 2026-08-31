"""Small tests: cheap deterministic analysis. Never mutates the revision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obsalt.analysis.tier1 import analyze_tier1
from obsalt.domain.enums import Speaker
from obsalt.domain.events import CallObserved, TurnObserved
from obsalt.domain.models import CallRevision, Turn
from obsalt.worker.process import process_normalized_events
from tests.helpers import example_state, fidelity_declaration


def test_tier1_does_not_mutate_the_revision() -> None:
    from obsalt.assemble.assembler import Assembler

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


def test_dead_air_and_truncated_llm_flags() -> None:
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    revision = CallRevision(
        org_id="o",
        call_id="c",
        revision="r",
        source="example",
        source_call_id="s",
        turns=[
            Turn(
                index=0,
                speaker=Speaker.USER,
                text="hello",
                started_at=t0,
                ended_at=t0 + timedelta(seconds=1),
            ),
            Turn(
                index=1,
                speaker=Speaker.AGENT,
                text="I can look that up...",
                started_at=t0 + timedelta(seconds=20),
                ended_at=t0 + timedelta(seconds=21),
            ),
        ],
    )
    flags = next(row for row in analyze_tier1(revision) if row.execution.analyzer_id == "flags")
    kinds = {item["kind"] for item in flags.payload["flags"]}
    assert "dead_air" in kinds
    assert "truncated_llm" in kinds
    length = CallRevision(
        org_id="o",
        call_id="c2",
        revision="r",
        source="example",
        source_call_id="s2",
        turns=[Turn(index=0, speaker=Speaker.AGENT, text="complete answer")],
        unmapped_attributes={"finish_reason": "length"},
    )
    kinds = {
        item["kind"]
        for item in next(
            row for row in analyze_tier1(length) if row.execution.analyzer_id == "flags"
        ).payload["flags"]
    }
    assert "truncated_llm" in kinds


def test_empty_grounding_hallucination_is_evidence_missing() -> None:
    from obsalt.domain.enums import AnalysisState

    state = example_state()
    rev = process_normalized_events(
        [
            CallObserved(source_call_id="c1"),
            TurnObserved(turn_index=0, speaker=Speaker.AGENT, text="Your order ORD-99 is $12"),
        ],
        org_id="acme",
        source="example",
        source_call_id="c1",
        envelope_id="e1",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    rows = state.sink.analysis[(rev.org_id, rev.call_id, rev.revision)]
    hallo = next(row for row in rows if row.execution.analyzer_id == "hallucination")
    assert hallo.execution.state is AnalysisState.COMPLETED
    assert hallo.payload.get("selection") == "detector"
    assert all(
        item.get("verdict") == "evidence_missing" for item in hallo.payload.get("claims") or []
    )
