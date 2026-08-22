"""Backend timeline view-model. Never invents stage intervals (T1)."""

from __future__ import annotations

from typing import Any

from obsalt.assemble.fidelity import waterfall_allowed
from obsalt.domain.enums import MeasurementPlacement, TimelineFidelity
from obsalt.domain.models import CallRevision, StageMeasurement


def timeline_view(revision: CallRevision) -> dict[str, Any]:
    fidelity = revision.lifecycle.timeline_fidelity
    intervals = [
        m
        for m in revision.stage_measurements
        if m.placement is MeasurementPlacement.INTERVAL and m.started_at and m.ended_at
    ]
    unplaced = [m for m in revision.stage_measurements if m.placement is not MeasurementPlacement.INTERVAL]
    anchored = [
        m for m in revision.stage_measurements if m.placement is MeasurementPlacement.ANCHORED_DURATION
    ]
    draw_waterfall = fidelity is TimelineFidelity.STAGE_LEVEL and waterfall_allowed(intervals)
    return {
        "call_id": revision.call_id,
        "revision": revision.revision,
        "fidelity": fidelity.value,
        "architecture": (
            revision.lifecycle.pipeline_architecture.value
            if revision.lifecycle.pipeline_architecture
            else None
        ),
        "waterfall": draw_waterfall,
        "reason": _reason(fidelity, draw_waterfall),
        "turns": [
            {
                "index": turn.index,
                "speaker": turn.speaker.value,
                "started_at": turn.started_at.isoformat() if turn.started_at else None,
                "ended_at": turn.ended_at.isoformat() if turn.ended_at else None,
                "text": turn.text,
                "interrupted": turn.interrupted,
                "chips": _chips_for_turn(unplaced, turn.index),
            }
            for turn in revision.turns
        ],
        "intervals": [_measure(m) for m in intervals] if draw_waterfall else [],
        "anchored_durations": [_measure(m) for m in anchored],
        "unplaced": [_measure(m) for m in unplaced if m.placement is MeasurementPlacement.UNPLACED],
        "aggregates": [
            {
                "stage": item.stage.value,
                "metric": item.metric.value,
                "statistic": item.statistic.value,
                "value_ms": item.value_ms,
                "population": item.population,
                "source_path": item.source_path,
                "provenance": item.provenance.value,
            }
            for item in revision.aggregate_measurements
        ],
    }


def _reason(fidelity: TimelineFidelity, waterfall: bool) -> str:
    if waterfall:
        return "stage intervals were measured with real start and end timestamps"
    if fidelity is TimelineFidelity.TURN_LEVEL:
        return "real turn boundaries exist; stage durations are unplaced and shown as chips"
    if fidelity is TimelineFidelity.MESSAGE_LEVEL:
        return "only coarse message anchors were measured; no complete stage intervals"
    if fidelity is TimelineFidelity.CALL_LEVEL:
        return "provider published call-level distributions; they are not drawn as a waterfall"
    return "no measured timeline facts"


def _chips_for_turn(measurements: list[StageMeasurement], turn_index: int) -> list[dict[str, Any]]:
    chips = []
    for item in measurements:
        if item.turn_index != turn_index:
            continue
        chips.append(_measure(item))
    return chips


def _measure(item: StageMeasurement) -> dict[str, Any]:
    return {
        "fact_id": item.fact_id,
        "stage": item.stage.value,
        "metric": item.metric.value,
        "value_ms": item.value_ms,
        "placement": item.placement.value,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "ended_at": item.ended_at.isoformat() if item.ended_at else None,
        "provenance": item.provenance.value,
        "source_path": item.source_path,
        "derivation": item.derivation,
    }
