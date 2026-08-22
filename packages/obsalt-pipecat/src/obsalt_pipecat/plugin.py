from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Stage,
)
from obsalt.domain.events import CallObserved, NormalizedEvent, StageObserved
from obsalt.plugin.protocol import FidelityDeclaration, PluginManifest


class PipecatPlugin:
    API_VERSION = 1
    name = "pipecat"
    display_name = "Pipecat"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="pipecat.otel",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset(
            {Signal.STAGE_INTERVALS, Signal.STT_DURATION, Signal.LLM_TTFT, Signal.TTS_TTFB, Signal.E2E_DURATION}
        ),
        structurally_absent={},
        schema_source="https://docs.pipecat.ai",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: Any) -> int:
        attrs = getattr(span, "attributes", {}) or {}
        name = getattr(span, "name", "") or ""
        if attrs.get("metrics.ttfb") is not None or str(name).startswith("turn"):
            return 50
        if attrs.get("gen_ai.provider.name") == "pipecat" or attrs.get("gen_ai.system") == "pipecat":
            return 50
        return 0

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        call_id = None
        for span in spans:
            attrs = getattr(span, "attributes", {}) or {}
            call_id = (
                call_id
                or attrs.get("gen_ai.conversation.id")
                or attrs.get("call.id")
            )
            start, end = getattr(span, "start_time", None), getattr(span, "end_time", None)
            name = getattr(span, "name", "") or ""
            stage = _stage_from(name, attrs)
            if stage and start is not None and end is not None:
                events.append(
                    StageObserved(
                        stage=stage,
                        metric=Metric.DURATION,
                        value_ms=(end - start) / 1_000_000.0,
                        placement=MeasurementPlacement.INTERVAL,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"span:{name}",
                    )
                )
        if call_id:
            events.insert(
                0,
                CallObserved(
                    source_call_id=str(call_id),
                    architecture=PipelineArchitecture.CASCADE,
                    source_path="gen_ai.conversation.id",
                ),
            )
        return events


def _stage_from(name: str, attrs: dict[str, Any]) -> Stage | None:
    lowered = name.lower()
    if "stt" in lowered or attrs.get("stt.provider"):
        return Stage.STT
    if "tts" in lowered:
        return Stage.TTS
    if "llm" in lowered or lowered.startswith("llm"):
        return Stage.LLM
    if "turn" in lowered:
        return Stage.E2E
    return None
