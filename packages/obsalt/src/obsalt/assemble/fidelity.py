"""Derive timeline fidelity and SignalCoverage from facts actually present.

A declaration states what a plugin can produce. This module states what a
specific call actually has. That distinction is the product's trust surface.
"""

from __future__ import annotations

from collections.abc import Iterable

from obsalt.domain.enums import (
    MeasurementPlacement,
    Provenance,
    Signal,
    SignalCoverageStatus,
    TimelineFidelity,
)
from obsalt.domain.events import (
    AggregateObserved,
    EvidenceObserved,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import SignalCoverage
from obsalt.plugin.protocol import FidelityDeclaration

ASSEMBLER_VERSION = "obsalt-assemble/1"

_STAGE_TO_SIGNAL = {
    "stt": Signal.STT_DURATION,
    "llm": Signal.LLM_DURATION,
    "tts": Signal.TTS_DURATION,
    "e2e": Signal.E2E_DURATION,
    "ttfa": Signal.TTFA,
    "endpointing": Signal.ENDPOINTING,
    "vad": Signal.VAD,
    "transport": Signal.TRANSPORT,
    "user_input": Signal.USER_INPUT,
    "generation": Signal.GENERATION,
    "playout": Signal.PLAYOUT,
}

_METRIC_TO_SIGNAL = {
    ("llm", "ttft"): Signal.LLM_TTFT,
    ("tts", "ttfb"): Signal.TTS_TTFB,
    ("ttfa", "duration"): Signal.TTFA,
    ("ttfa", "first_audio"): Signal.TTFA,
}


def derive_fidelity(events: Iterable[NormalizedEvent]) -> TimelineFidelity:
    events = list(events)
    has_interval = False
    has_turn = False
    has_coarse = False
    has_unplaced_or_agg = False
    for event in events:
        if isinstance(event, StageObserved):
            if event.placement is MeasurementPlacement.INTERVAL and event.started_at and event.ended_at:
                has_interval = True
            elif event.placement is MeasurementPlacement.COARSE_ANCHOR:
                has_coarse = True
            elif event.placement in {MeasurementPlacement.UNPLACED, MeasurementPlacement.ANCHORED_DURATION}:
                has_unplaced_or_agg = True
        elif isinstance(event, TurnObserved) and event.started_at and event.ended_at:
            has_turn = True
        elif isinstance(event, TurnObserved) and (event.started_at or event.ended_at):
            has_coarse = True
        elif isinstance(event, AggregateObserved):
            has_unplaced_or_agg = True
    if has_interval:
        return TimelineFidelity.STAGE_LEVEL
    if has_turn:
        return TimelineFidelity.TURN_LEVEL
    if has_coarse:
        return TimelineFidelity.MESSAGE_LEVEL
    if has_unplaced_or_agg:
        return TimelineFidelity.CALL_LEVEL
    return TimelineFidelity.NONE


def derive_coverage(
    events: Iterable[NormalizedEvent],
    *,
    decoder_version: str,
    declaration: FidelityDeclaration | None = None,
) -> list[SignalCoverage]:
    present: dict[Signal, SignalCoverage] = {}
    for event in events:
        for signal, path in _signals_in(event):
            present[signal] = SignalCoverage(
                signal=signal,
                status=SignalCoverageStatus.PRESENT,
                source_path=path,
                decoder_version=decoder_version,
            )
    rows = list(present.values())
    if declaration is None:
        return rows
    seen = {row.signal for row in rows}
    for signal, reason in declaration.structurally_absent.items():
        if signal in seen:
            continue
        rows.append(
            SignalCoverage(
                signal=signal,
                status=SignalCoverageStatus.UNSUPPORTED,
                reason=reason,
                decoder_version=decoder_version,
            )
        )
    for signal in declaration.provides:
        if signal in seen:
            continue
        if signal in declaration.structurally_absent:
            continue
        rows.append(
            SignalCoverage(
                signal=signal,
                status=SignalCoverageStatus.ABSENT,
                reason="provider can supply this signal but this payload did not",
                decoder_version=decoder_version,
            )
        )
    return rows


def _signals_in(event: NormalizedEvent) -> list[tuple[Signal, str | None]]:
    if isinstance(event, StageObserved):
        mapped = _METRIC_TO_SIGNAL.get((event.stage.value, event.metric.value))
        if mapped:
            hits = [(mapped, event.source_path)]
        else:
            hits = [(_STAGE_TO_SIGNAL.get(event.stage.value, Signal.E2E_DURATION), event.source_path)]
        if event.placement is MeasurementPlacement.INTERVAL:
            hits.append((Signal.STAGE_INTERVALS, event.source_path))
        return hits
    if isinstance(event, AggregateObserved):
        mapped = _METRIC_TO_SIGNAL.get((event.stage.value, event.metric.value))
        if mapped:
            return [(mapped, event.source_path)]
        return [(_STAGE_TO_SIGNAL.get(event.stage.value, Signal.E2E_DURATION), event.source_path)]
    if isinstance(event, TurnObserved):
        hits = [(Signal.TRANSCRIPT, event.source_path)]
        if event.started_at and event.ended_at:
            hits.append((Signal.TURN_INTERVALS, event.source_path))
        if event.confidence is not None:
            hits.append((Signal.STT_CONFIDENCE, event.source_path))
        return hits
    if isinstance(event, ToolObserved):
        hits = [(Signal.TOOL_PAYLOAD, event.source_path)]
        if event.started_at and event.ended_at:
            hits.append((Signal.TOOL_TIMING, event.source_path))
        return hits
    if isinstance(event, OutcomeObserved):
        hits = [(Signal.HANGUP_REASON, event.source_path)]
        if event.cost is not None:
            hits.append((Signal.COST, event.source_path))
        return hits
    if isinstance(event, GroundingObserved):
        kind_map = {
            "system_prompt": Signal.GROUNDING_SYSTEM_PROMPT,
            "knowledge": Signal.GROUNDING_KNOWLEDGE,
            "tool_result": Signal.GROUNDING_TOOL_RESULTS,
            "user_text": Signal.GROUNDING_USER_TEXT,
        }
        return [(kind_map[event.kind.value], event.source_path)]
    if isinstance(event, EvidenceObserved) and event.kind.value == "recording":
        return [(Signal.RECORDING, event.source_path)]
    if isinstance(event, InterruptionObserved):
        hits = [(Signal.INTERRUPTION_COUNT, event.source_path)]
        if event.kind.value == "barge_in":
            hits.append((Signal.BARGE_IN, event.source_path))
        return hits
    return []


def waterfall_allowed(measurements: Iterable[object]) -> bool:
    """A stage waterfall renders only INTERVAL measurements (T1)."""
    for item in measurements:
        placement = getattr(item, "placement", None)
        if placement is MeasurementPlacement.INTERVAL:
            started = getattr(item, "started_at", None)
            ended = getattr(item, "ended_at", None)
            if started and ended:
                return True
    return False
