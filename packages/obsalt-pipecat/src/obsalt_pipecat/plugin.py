from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from obsalt._version import PLUGIN_API_VERSION
from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
)
from obsalt.domain.events import (
    CallObserved,
    NormalizedEvent,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.otel.conventions import (
    CONVERSATION_ID,
    PROVIDER_CALL_ID,
    SPAN_LLM,
    SPAN_STT,
    SPAN_TOOL,
    SPAN_TTS,
    SPAN_TURN,
    genai_provider_name,
    genai_provider_name_key,
)
from obsalt.plugin.types import PluginManifest, ReadableSpan


class PipecatPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "pipecat"
    display_name = "Pipecat"
    decoder_version = "pipecat/1"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="pipecat.otlp",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVAL, Signal.TURN_INTERVAL, Signal.TTFA}),
        structurally_absent={},
        schema_source="Pipecat tracing (metrics.ttfb, turn.*, gen_ai.provider.name in code; gen_ai.system in docs)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: ReadableSpan) -> int:
        attrs = span.attributes or {}
        if "metrics.ttfb" in attrs or str(attrs.get("turn.index", "")).isdigit():
            return 70
        if span.name in {SPAN_TURN, SPAN_STT, SPAN_LLM, SPAN_TTS, SPAN_TOOL}:
            return 30
        if genai_provider_name(attrs):
            return 25
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        conv = None
        provider_key = None
        for span in spans:
            attrs = span.attributes or {}
            conv = attrs.get(CONVERSATION_ID) or attrs.get(PROVIDER_CALL_ID) or conv
            provider_key = provider_key or genai_provider_name_key(attrs)
        provenance: dict[str, ProvenanceStamp] = {}
        if provider_key:
            provenance["agent_id"] = ProvenanceStamp(
                provenance=Provenance.PROVIDER_REPORTED, source_path=provider_key
            )
        if conv:
            yield CallObserved(
                source_call_id=str(conv),
                architecture=PipelineArchitecture.CASCADE,
                provenance_by_field=provenance,
            )
        for span in spans:
            attrs = span.attributes or {}
            started = datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC)
            ended = datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC)
            ms = (span.end_unix_nano - span.start_unix_nano) / 1e6
            turn_index = attrs.get("turn.index")
            turn_i = int(turn_index) if turn_index is not None else None
            if span.name == SPAN_TURN:
                speaker = Speaker.AGENT if attrs.get("turn.speaker") == "agent" else Speaker.USER
                yield TurnObserved(turn_index=turn_i or 0, speaker=speaker, started_at=started, ended_at=ended)
                continue
            if span.name == SPAN_TOOL:
                yield ToolObserved(
                    tool_id=str(attrs.get("tool.id") or attrs.get("gen_ai.tool.call.id") or span.span_id),
                    name=str(attrs.get("tool.name") or attrs.get("gen_ai.tool.name") or "tool"),
                    turn_index=turn_i,
                    started_at=started,
                    ended_at=ended,
                )
                yield StageObserved(
                    stage=Stage.TOOL,
                    metric=Metric.DURATION,
                    value_ms=ms,
                    turn_index=turn_i,
                    placement=MeasurementPlacement.INTERVAL,
                    started_at=started,
                    ended_at=ended,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"span:{span.name}",
                )
                continue
            stage = {SPAN_STT: Stage.STT, SPAN_LLM: Stage.LLM, SPAN_TTS: Stage.TTS}.get(span.name)
            if stage is None:
                continue
            yield StageObserved(
                stage=stage,
                metric=Metric.DURATION,
                value_ms=ms,
                turn_index=turn_i,
                placement=MeasurementPlacement.INTERVAL,
                started_at=started,
                ended_at=ended,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=f"span:{span.name}",
            )
