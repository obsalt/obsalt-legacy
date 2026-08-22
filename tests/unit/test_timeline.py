"""T1: a waterfall is drawn only from INTERVAL measurements with real timestamps."""

from __future__ import annotations

from datetime import UTC, date, datetime

from obsalt.assemble.assembler import Assembler
from obsalt.assemble.facts import stamp_event
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import (
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Stage,
)
from obsalt.domain.events import CallObserved, StageObserved
from obsalt.domain.models import FidelityDeclaration


def _decl() -> FidelityDeclaration:
    return FidelityDeclaration(
        source_format="test",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(MeasurementPlacement),
        provides=frozenset({Signal.STT_DURATION}),
        structurally_absent={},
        schema_source="test",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )


def _stamp(event, seq: int):
    return stamp_event(
        event,
        org_id="o",
        call_key="k",
        envelope_id="e",
        decoder_version="t/1",
        processing_run_id="r",
        envelope_sequence=seq,
    )


def test_anchored_duration_is_not_a_waterfall_interval() -> None:
    start = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    call = Assembler(_decl(), decoder_version="t/1", processing_run_id="r").assemble(
        "o",
        "cid",
        "test",
        [
            _stamp(CallObserved(source_call_id="c1"), 0),
            _stamp(
                StageObserved(
                    stage=Stage.STT,
                    metric=Metric.DURATION,
                    value_ms=140,
                    turn_index=0,
                    placement=MeasurementPlacement.ANCHORED_DURATION,
                    started_at=start,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="turn.stt",
                ),
                1,
            ),
        ],
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert view["anchored_stage_chips"]
    assert view["anchored_stage_chips"][0]["placement"] == MeasurementPlacement.ANCHORED_DURATION.value


def test_interval_without_timestamps_is_downgraded_and_not_drawn() -> None:
    call = Assembler(_decl(), decoder_version="t/1", processing_run_id="r").assemble(
        "o",
        "cid",
        "test",
        [
            _stamp(CallObserved(source_call_id="c1"), 0),
            _stamp(
                StageObserved(
                    stage=Stage.STT,
                    metric=Metric.DURATION,
                    value_ms=12,
                    placement=MeasurementPlacement.INTERVAL,
                    started_at=None,
                    ended_at=None,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="broken",
                ),
                1,
            ),
        ],
    )
    view = timeline_view(call)
    assert view["draw_stage_waterfall"] is False
    assert view["stage_intervals"] == []
    assert call.stage_measurements[0].placement is MeasurementPlacement.UNPLACED
