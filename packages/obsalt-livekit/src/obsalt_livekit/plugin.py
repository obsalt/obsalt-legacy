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
from obsalt.plugin.protocol import FidelityDeclaration, PluginManifest


class LiveKitPlugin:
    API_VERSION = 1
    name = "livekit"
    display_name = "LiveKit"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="livekit.otel",
        possible_architectures=frozenset(
            {PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}
        ),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVALS, Signal.E2E_DURATION}),
        structurally_absent={},
        schema_source="https://docs.livekit.io",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: Any) -> int:
        attrs = getattr(span, "attributes", {}) or {}
        if any(str(k).startswith("lk.") for k in attrs):
            return 50
        return 0

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        call_id = None
        for span in spans:
            attrs = getattr(span, "attributes", {}) or {}
            call_id = call_id or attrs.get("gen_ai.conversation.id") or attrs.get("lk.room.name")
            start, end = getattr(span, "start_time", None), getattr(span, "end_time", None)
            if start is not None and end is not None:
                from datetime import datetime

                def _as_dt(value: Any):
                    if value is None:
                        return None
                    if isinstance(value, datetime):
                        return value
                    ns = float(value)
                    if ns > 1e14:
                        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
                    return datetime.fromtimestamp(ns, tz=UTC)

                events.append(
                    StageObserved(
                        stage=Stage.E2E,
                        metric=Metric.DURATION,
                        value_ms=(float(end) - float(start)) / 1_000_000.0
                        if float(end) - float(start) > 10_000
                        else (float(end) - float(start)) * 1000.0,
                        placement=MeasurementPlacement.INTERVAL,
                        started_at=_as_dt(start),
                        ended_at=_as_dt(end),
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="lk.span",
                    )
                )
            # Accept both LiveKit and merged-spec audio token keys.
            _ = attrs.get("gen_ai.usage.input_audio_tokens") or attrs.get("gen_ai.usage.audio.input_tokens")
        if call_id:
            events.insert(
                0,
                CallObserved(
                    source_call_id=str(call_id),
                    architecture=PipelineArchitecture.CASCADE,
                    source_path="lk.room.name",
                ),
            )
        return events
