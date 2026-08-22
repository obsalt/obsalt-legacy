from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date

from obsalt.assemble.assembler import Assembler, fold_facts
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.promote import MemoryPointerStore, promote
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
)
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.domain.models import FidelityDeclaration


def _decl() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION, Signal.TURN_INTERVAL}),
        structurally_absent={Signal.VAD: "not in fixture"},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def _events(order: Sequence[str]) -> list:
    catalog = {
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
    return [catalog[name] for name in order]


def test_fold_is_idempotent_and_commutative() -> None:
    names = ["call", "turn", "stt"]
    base = _events(names)
    a, conflicts_a, _ = fold_facts(base)
    b, conflicts_b, _ = fold_facts(list(reversed(base)) + base)
    assert conflicts_a == []
    assert conflicts_b == []
    assert set(a) == set(b)
    assert {r.content_hash for r in a.values()} == {r.content_hash for r in b.values()}


def test_unplaced_stage_does_not_draw_waterfall() -> None:
    assembler = Assembler(_decl(), decoder_version="t/1", processing_run_id="r")
    events = []
    for i, event in enumerate(_events(["call", "turn", "stt"])):
        events.append(
            stamp_event(
                event,
                org_id="o",
                call_key="k",
                envelope_id="e",
                decoder_version="t/1",
                processing_run_id="r",
                envelope_sequence=i,
            )
        )
    call = assembler.assemble("o", "cid", "test", events)
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["unplaced_stage_chips"]
    assert view["stage_intervals"] == []
    assert call.timeline_fidelity.value == "turn_level" or call.timeline_fidelity.value in {
        "call_level",
        "turn_level",
    }


def test_interval_stage_draws_waterfall() -> None:
    from datetime import datetime

    start = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 22, 12, 0, 1, tzinfo=UTC)
    events = [
        stamp_event(
            CallObserved(source_call_id="c1"),
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=0,
        ),
        stamp_event(
            StageObserved(
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=1000,
                placement=MeasurementPlacement.INTERVAL,
                started_at=start,
                ended_at=end,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="span",
            ),
            org_id="o",
            call_key="k",
            envelope_id="e",
            decoder_version="t/1",
            processing_run_id="r",
            envelope_sequence=1,
        ),
    ]
    call = Assembler(_decl(), decoder_version="t/1", processing_run_id="r").assemble(
        "o", "cid", "test", events
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is True
    assert call.timeline_fidelity.value == "stage_level"


def test_promote_cas_and_retain_prior_revision() -> None:
    from obsalt.worker.process import MemoryRevisionSink

    sink = MemoryRevisionSink()
    pointers = MemoryPointerStore()
    assembler = Assembler(_decl(), decoder_version="t/1", processing_run_id="r")
    e1 = [
        stamp_event(
            CallObserved(source_call_id="c1", agent_id="a"),
            org_id="o",
            call_key="cid",
            envelope_id="e1",
            decoder_version="vapi/1",
            processing_run_id="r1",
            envelope_sequence=0,
        )
    ]
    first = assembler.assemble("o", "cid", "vapi", e1)
    sink.write(first)
    assert promote(
        pointers, first, expected=None, fact_frontier=frozenset({e1[0].fact_id or ""})
    ).promoted
    e2 = [
        stamp_event(
            CallObserved(source_call_id="c1", agent_id="a2"),
            org_id="o",
            call_key="cid",
            envelope_id="e2",
            decoder_version="vapi/2",
            processing_run_id="r2",
            envelope_sequence=0,
        )
    ]
    second = assembler.assemble("o", "cid", "vapi", e2)
    sink.write(second)
    assert promote(
        pointers, second, expected=first.revision, fact_frontier=frozenset({e2[0].fact_id or ""})
    ).promoted
    assert pointers.get("o", "cid") == second.revision
    assert sink.get("o", "cid", first.revision) is not None
    assert first.revision != second.revision
