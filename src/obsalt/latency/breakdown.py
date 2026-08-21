from __future__ import annotations

from obsalt.domain.enums import LatencyComponent, Speaker
from obsalt.domain.models import CanonicalCall, LatencySample, Turn


def _gap_ms(prev: Turn, current: Turn) -> float | None:
    if prev.seconds_from_start is not None and current.seconds_from_start is not None:
        prev_end = prev.seconds_from_start + ((prev.duration_ms or 0) / 1000.0)
        gap = (current.seconds_from_start - prev_end) * 1000.0
        return max(0.0, gap)
    if prev.ended_at and current.started_at:
        return max(0.0, (current.started_at - prev.ended_at).total_seconds() * 1000.0)
    if prev.started_at and current.started_at:
        prev_end = prev.ended_at or prev.started_at
        return max(0.0, (current.started_at - prev_end).total_seconds() * 1000.0)
    return None


def derive_turn_gaps(call: CanonicalCall) -> None:
    """Fill time-to-first-audio from user→agent turn gaps when provider omitted it."""
    previous: Turn | None = None
    for turn in call.turns:
        if (
            previous
            and previous.speaker == Speaker.USER
            and turn.speaker == Speaker.AGENT
            and turn.time_to_first_audio_ms is None
        ):
            gap = _gap_ms(previous, turn)
            if gap is not None:
                turn.time_to_first_audio_ms = gap
        previous = turn


def samples_from_turns(call: CanonicalCall) -> list[LatencySample]:
    samples: list[LatencySample] = []
    for turn in call.turns:
        if turn.stt_ms is not None:
            samples.append(LatencySample(component=LatencyComponent.STT, duration_ms=turn.stt_ms, turn_index=turn.index))
        if turn.llm_ms is not None:
            samples.append(
                LatencySample(
                    component=LatencyComponent.LLM,
                    duration_ms=turn.llm_ms,
                    turn_index=turn.index,
                    ttft_ms=turn.llm_ttft_ms,
                )
            )
        if turn.tts_ms is not None:
            samples.append(
                LatencySample(
                    component=LatencyComponent.TTS,
                    duration_ms=turn.tts_ms,
                    turn_index=turn.index,
                    ttfb_ms=turn.tts_ttfb_ms,
                )
            )
        if turn.time_to_first_audio_ms is not None:
            samples.append(
                LatencySample(
                    component=LatencyComponent.TTFA,
                    duration_ms=turn.time_to_first_audio_ms,
                    turn_index=turn.index,
                )
            )
            samples.append(
                LatencySample(
                    component=LatencyComponent.E2E,
                    duration_ms=turn.time_to_first_audio_ms,
                    turn_index=turn.index,
                )
            )
    return samples


def merge_samples(existing: list[LatencySample], extra: list[LatencySample]) -> list[LatencySample]:
    seen: set[tuple[str, int | None, float]] = set()
    merged: list[LatencySample] = []
    for sample in existing + extra:
        key = (sample.component.value, sample.turn_index, round(sample.duration_ms, 2))
        if key in seen:
            continue
        seen.add(key)
        merged.append(sample)
    return merged


def enrich_latency(call: CanonicalCall) -> CanonicalCall:
    derive_turn_gaps(call)
    call.latency_samples = merge_samples(call.latency_samples, samples_from_turns(call))
    return call
