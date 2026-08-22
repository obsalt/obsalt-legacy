from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date
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
from obsalt.plugin.protocol import FidelityDeclaration, PluginManifest, SdkConfig


class GeminiLivePlugin:
    API_VERSION = 1
    name = "gemini-live"
    display_name = "Gemini Live"
    capabilities = frozenset({Capability.SDK_INSTRUMENTATION, Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="obsalt.sdk.s2s.gemini_live",
        possible_architectures=frozenset({PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.USER_INPUT, Signal.GENERATION, Signal.PLAYOUT, Signal.STAGE_INTERVALS}),
        structurally_absent={
            Signal.STT_DURATION: "speech-to-speech: no STT stage exists",
            Signal.LLM_DURATION: "speech-to-speech: no LLM stage exists",
            Signal.TTS_DURATION: "speech-to-speech: no TTS stage exists",
        },
        schema_source="https://ai.google.dev/gemini-api/docs/live",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def instrument(self, client: object, cfg: SdkConfig) -> object:
        client._obsalt_sdk = {"source": self.name, "cfg": cfg.model_dump()}
        return client

    def claims(self, span: Any) -> int:
        attrs = getattr(span, "attributes", {}) or {}
        if attrs.get("obsalt.source") == "gemini-live":
            return 80
        return 0

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        call_id = None
        for span in spans:
            attrs = getattr(span, "attributes", {}) or {}
            call_id = call_id or attrs.get("gen_ai.conversation.id")
            name = getattr(span, "name", "")
            stage = {
                "user_input": Stage.USER_INPUT,
                "generation": Stage.GENERATION,
                "playout": Stage.PLAYOUT,
            }.get(name)
            start, end = getattr(span, "start_time", None), getattr(span, "end_time", None)
            if stage and start is not None and end is not None:
                from datetime import datetime

                def _as_dt(value: Any):
                    if isinstance(value, datetime):
                        return value
                    ns = float(value)
                    if ns > 1e14:
                        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
                    return datetime.fromtimestamp(ns, tz=UTC)

                events.append(
                    StageObserved(
                        stage=stage,
                        metric=Metric.DURATION,
                        value_ms=(float(end) - float(start)) / 1_000_000.0
                        if float(end) - float(start) > 10_000
                        else (float(end) - float(start)) * 1000.0,
                        placement=MeasurementPlacement.INTERVAL,
                        started_at=_as_dt(start),
                        ended_at=_as_dt(end),
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"span:{name}",
                    )
                )
        if call_id:
            events.insert(
                0,
                CallObserved(
                    source_call_id=str(call_id),
                    architecture=PipelineArchitecture.SPEECH_TO_SPEECH,
                    source_path="gen_ai.conversation.id",
                ),
            )
        return events
