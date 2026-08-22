from __future__ import annotations

from obsalt.domain.enums import (
    MeasurementPlacement,
    PipelineArchitecture,
    Signal,
    SignalCoverageStatus,
    TimelineFidelity,
)
from obsalt.domain.events import (
    AggregateObserved,
    CallObserved,
    EvidenceObserved,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, SignalCoverage


def derive_fidelity(events: list[NormalizedEvent]) -> TimelineFidelity:
    has_interval = False
    has_turn = False
    has_coarse = False
    has_call = False
    for event in events:
        if isinstance(event, StageObserved):
            if event.placement is MeasurementPlacement.INTERVAL and event.started_at and event.ended_at:
                has_interval = True
            elif event.placement is MeasurementPlacement.COARSE_ANCHOR:
                has_coarse = True
            elif event.turn_index is not None:
                has_turn = True
            else:
                has_call = True
        elif isinstance(event, TurnObserved):
            if event.started_at and event.ended_at:
                has_turn = True
            elif event.started_at or event.ended_at:
                # start XOR end at message resolution (ElevenLabs whole-second anchors)
                has_coarse = True
        elif isinstance(event, AggregateObserved):
            has_call = True
    if has_interval:
        return TimelineFidelity.STAGE_LEVEL
    if has_turn:
        return TimelineFidelity.TURN_LEVEL
    if has_coarse:
        return TimelineFidelity.MESSAGE_LEVEL
    if has_call:
        return TimelineFidelity.CALL_LEVEL
    return TimelineFidelity.NONE


def derive_coverage(
    events: list[NormalizedEvent],
    declaration: FidelityDeclaration,
    decoder_version: str,
) -> list[SignalCoverage]:
    present: dict[Signal, str | None] = {}
    for event in events:
        if isinstance(event, StageObserved):
            present[_stage_signal(event)] = event.source_path
        elif isinstance(event, TurnObserved):
            present[Signal.TRANSCRIPT] = "turn.text"
            if event.confidence is not None:
                present[Signal.STT_CONFIDENCE] = "turn.confidence"
            if event.started_at or event.ended_at:
                present[Signal.TURN_INTERVAL] = "turn.started_at"
        elif isinstance(event, ToolObserved):
            present[Signal.TOOL_RESULT] = "tool"
            if event.started_at and event.ended_at:
                present[Signal.TOOL_TIMING] = "tool.started_at"
        elif isinstance(event, GroundingObserved):
            present[_grounding_signal(event)] = event.source_path
        elif isinstance(event, EvidenceObserved) and event.kind.value == "recording":
            present[Signal.RECORDING] = event.source_path
        elif isinstance(event, OutcomeObserved):
            stamp = event.provenance_by_field.get("provider_code")
            present[Signal.HANGUP] = stamp.source_path if stamp is not None else None
        elif isinstance(event, InterruptionObserved):
            present[Signal.INTERRUPTION_COUNT] = "interruption"
            present[Signal.BARGE_IN] = "interruption"
        elif isinstance(event, CallObserved) and event.cost is not None:
            present[Signal.COST] = "call.cost"
        elif isinstance(event, AggregateObserved):
            present[_stage_signal_from_names(event.stage.value, event.metric.value)] = event.source_path

    rows: list[SignalCoverage] = []
    for signal in Signal:
        if signal in present:
            rows.append(
                SignalCoverage(
                    signal=signal,
                    status=SignalCoverageStatus.PRESENT,
                    source_path=present[signal],
                    decoder_version=decoder_version,
                )
            )
        elif signal in declaration.structurally_absent:
            rows.append(
                SignalCoverage(
                    signal=signal,
                    status=SignalCoverageStatus.UNSUPPORTED,
                    reason=declaration.structurally_absent[signal],
                    decoder_version=decoder_version,
                )
            )
        elif signal in declaration.provides:
            rows.append(
                SignalCoverage(
                    signal=signal,
                    status=SignalCoverageStatus.ABSENT,
                    reason="provider can supply this signal but it was not present on this payload",
                    decoder_version=decoder_version,
                )
            )
    return rows


def architecture_of(events: list[NormalizedEvent], declaration: FidelityDeclaration) -> PipelineArchitecture:
    for event in events:
        if isinstance(event, CallObserved):
            return event.architecture
    return next(iter(declaration.possible_architectures), PipelineArchitecture.CASCADE)


def _stage_signal(event: StageObserved) -> Signal:
    return _stage_signal_from_names(event.stage.value, event.metric.value)


def _stage_signal_from_names(stage: str, metric: str) -> Signal:
    mapping = {
        ("stt", "duration"): Signal.STT_DURATION,
        ("stt", "ttfb"): Signal.STT_DURATION,
        ("llm", "ttft"): Signal.LLM_TTFT,
        ("llm", "duration"): Signal.LLM_DURATION,
        ("tts", "duration"): Signal.TTS_DURATION,
        ("tts", "ttfb"): Signal.TTS_TTFB,
        ("user_input", "duration"): Signal.STAGE_INTERVAL,
        ("generation", "duration"): Signal.STAGE_INTERVAL,
        ("playout", "duration"): Signal.STAGE_INTERVAL,
        ("e2e", "duration"): Signal.E2E_DURATION,
        ("ttfa", "duration"): Signal.TTFA,
        ("ttfa", "first_audio"): Signal.TTFA,
        ("endpointing", "duration"): Signal.ENDPOINTING,
        ("vad", "duration"): Signal.VAD,
        ("transport", "duration"): Signal.TRANSPORT,
        ("tool", "duration"): Signal.TOOL_TIMING,
    }
    return mapping.get((stage, metric), Signal.E2E_DURATION)


def _grounding_signal(event: GroundingObserved) -> Signal:
    return {
        "system_prompt": Signal.GROUNDING_PROMPT,
        "knowledge": Signal.GROUNDING_KNOWLEDGE,
        "tool_result": Signal.GROUNDING_TOOLS,
        "user_text": Signal.GROUNDING_USER,
    }[event.kind.value]
