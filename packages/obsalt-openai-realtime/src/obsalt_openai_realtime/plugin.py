"""OpenAI Realtime: SDK instrumentation + S2S OTLP mapper.

No STT/LLM/TTS split exists. Barge-in is emitted only from a real interruption
signal, never from "agent turn followed by user speech".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date
from typing import Any

from obsalt.domain.enums import (
    Capability,
    InterruptionKind,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Stage,
)
from obsalt.domain.events import CallObserved, InterruptionObserved, NormalizedEvent, StageObserved
from obsalt.plugin.protocol import FidelityDeclaration, PluginManifest, SdkConfig


class OpenAIRealtimePlugin:
    API_VERSION = 1
    name = "openai-realtime"
    display_name = "OpenAI Realtime"
    capabilities = frozenset({Capability.SDK_INSTRUMENTATION, Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="obsalt.sdk.s2s.openai_realtime",
        possible_architectures=frozenset({PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset(
            {Signal.USER_INPUT, Signal.GENERATION, Signal.PLAYOUT, Signal.STAGE_INTERVALS, Signal.BARGE_IN}
        ),
        structurally_absent={
            Signal.STT_DURATION: "speech-to-speech: no STT stage exists",
            Signal.LLM_DURATION: "speech-to-speech: no LLM stage exists",
            Signal.TTS_DURATION: "speech-to-speech: no TTS stage exists",
        },
        schema_source="https://platform.openai.com/docs/guides/realtime",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def instrument(self, client: object, cfg: SdkConfig) -> object:
        client._obsalt_sdk = {"source": self.name, "cfg": cfg.model_dump()}
        return client

    def claims(self, span: Any) -> int:
        attrs = getattr(span, "attributes", {}) or {}
        if attrs.get("obsalt.source") == "openai-realtime":
            return 80
        name = getattr(span, "name", "")
        if name in {"user_input", "generation", "playout"}:
            return 20
        return 0

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        call_id = None
        for span in spans:
            attrs = getattr(span, "attributes", {}) or {}
            call_id = call_id or attrs.get("gen_ai.conversation.id") or attrs.get("call.id")
            name = getattr(span, "name", "")
            start = getattr(span, "start_time", None)
            end = getattr(span, "end_time", None)
            stage = {
                "user_input": Stage.USER_INPUT,
                "generation": Stage.GENERATION,
                "playout": Stage.PLAYOUT,
            }.get(name)
            if stage and start is not None and end is not None:
                events.append(
                    StageObserved(
                        stage=stage,
                        metric=Metric.DURATION,
                        value_ms=_span_ms(start, end),
                        placement=MeasurementPlacement.INTERVAL,
                        started_at=_as_dt(start),
                        ended_at=_as_dt(end),
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"span:{name}",
                    )
                )
            if attrs.get("obsalt.barge_in") is True:
                events.append(
                    InterruptionObserved(
                        kind=InterruptionKind.BARGE_IN, count=1, source_path="obsalt.barge_in"
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


def _span_ms(start: float, end: float) -> float:
    delta = end - start
    return delta / 1_000_000.0 if delta > 10_000 else delta * 1000.0


def _as_dt(value: Any) -> Any:
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    ns = float(value)
    if ns > 1e14:
        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
    return datetime.fromtimestamp(ns, tz=UTC)
