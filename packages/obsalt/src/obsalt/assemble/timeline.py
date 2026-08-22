from __future__ import annotations

from obsalt.domain.enums import MeasurementPlacement, TimelineFidelity
from obsalt.domain.models import CallRevision, StageMeasurement


def timeline_view(call: CallRevision) -> dict[str, object]:
    """Backend view-model: stage waterfalls only from INTERVAL measurements."""

    intervals = [m for m in call.stage_measurements if _is_interval(m)]
    anchored = [m for m in call.stage_measurements if _is_anchored(m)]
    unplaced = [m for m in call.stage_measurements if not _is_interval(m) and not _is_anchored(m)]
    return {
        "call_id": call.call_id,
        "revision": call.revision,
        "timeline_fidelity": call.timeline_fidelity.value,
        "pipeline_architecture": call.pipeline_architecture.value,
        "turns": [
            {
                "index": t.index,
                "speaker": t.speaker.value,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "ended_at": t.ended_at.isoformat() if t.ended_at else None,
                "interrupted": t.interrupted,
            }
            for t in call.turns
        ],
        "stage_intervals": [_measure(m) for m in intervals],
        "anchored_stage_chips": [_measure(m) for m in anchored],
        "unplaced_stage_chips": [_measure(m) for m in unplaced],
        "aggregates": [
            {
                "stage": a.stage.value,
                "metric": a.metric.value,
                "statistic": a.statistic.value,
                "value_ms": a.value_ms,
                "population": a.population,
                "provenance": a.provenance.value,
                "source_path": a.source_path,
            }
            for a in call.aggregate_measurements
        ],
        "draw_stage_waterfall": call.timeline_fidelity is TimelineFidelity.STAGE_LEVEL
        and bool(intervals),
        "reason": _reason(call),
    }


def _is_interval(measurement: StageMeasurement) -> bool:
    return (
        measurement.placement is MeasurementPlacement.INTERVAL
        and measurement.started_at is not None
        and measurement.ended_at is not None
    )


def _is_anchored(measurement: StageMeasurement) -> bool:
    return (
        measurement.placement is MeasurementPlacement.ANCHORED_DURATION
        and measurement.started_at is not None
    )


def _measure(measurement: StageMeasurement) -> dict[str, object]:
    return {
        "fact_id": measurement.fact_id,
        "stage": measurement.stage.value,
        "metric": measurement.metric.value,
        "value_ms": measurement.value_ms,
        "turn_index": measurement.turn_index,
        "placement": measurement.placement.value,
        "started_at": measurement.started_at.isoformat() if measurement.started_at else None,
        "ended_at": measurement.ended_at.isoformat() if measurement.ended_at else None,
        "provenance": measurement.provenance.value,
        "source_path": measurement.source_path,
        "derivation": measurement.derivation,
    }


def _reason(call: CallRevision) -> str:
    if call.timeline_fidelity is TimelineFidelity.STAGE_LEVEL:
        return "real stage intervals are present"
    if call.timeline_fidelity is TimelineFidelity.TURN_LEVEL:
        return "turn bars with unplaced stage chips; no invented stage intervals"
    if call.timeline_fidelity is TimelineFidelity.MESSAGE_LEVEL:
        return "coarse message anchors only"
    if call.timeline_fidelity is TimelineFidelity.CALL_LEVEL:
        return "provider published call-level distributions only"
    return "no timing measurements on this call"
