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
    Stage,
)
from obsalt.domain.events import CallObserved, NormalizedEvent, StageObserved
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.otel.conventions import CONVERSATION_ID, genai_audio_input_tokens
from obsalt.plugin.types import PluginManifest, ReadableSpan


class LiveKitPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "livekit"
    display_name = "LiveKit"
    decoder_version = "livekit/1"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="livekit.otlp",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVAL}),
        structurally_absent={},
        schema_source="LiveKit lk.* and gen_ai.usage.input_audio_tokens (accepted alongside merged spec)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: ReadableSpan) -> int:
        attrs = span.attributes or {}
        if any(str(k).startswith("lk.") for k in attrs):
            return 70
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        conv = None
        token_key = None
        for span in spans:
            attrs = span.attributes or {}
            conv = attrs.get(CONVERSATION_ID) or attrs.get("lk.room.name") or conv
            _count, key = genai_audio_input_tokens(attrs)
            if key:
                token_key = token_key or key
        provenance: dict[str, ProvenanceStamp] = {}
        if token_key:
            provenance["input_audio_tokens"] = ProvenanceStamp(
                provenance=Provenance.PROVIDER_REPORTED, source_path=token_key
            )
        if conv:
            yield CallObserved(
                source_call_id=str(conv),
                architecture=PipelineArchitecture.CASCADE,
                provenance_by_field=provenance,
            )
        for span in spans:
            if not span.name:
                continue
            attrs = span.attributes or {}
            _count, key = genai_audio_input_tokens(attrs)
            source_path = f"span:{span.name}"
            if key:
                source_path = f"span:{span.name}/{key}"
            yield StageObserved(
                stage=_stage_for_span(span.name, attrs),
                metric=Metric.DURATION,
                value_ms=(span.end_unix_nano - span.start_unix_nano) / 1e6,
                placement=MeasurementPlacement.INTERVAL,
                started_at=datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC),
                ended_at=datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=source_path,
            )


def _stage_for_span(name: str, attrs: dict[str, object]) -> Stage:
    blob = f"{name} {' '.join(str(k) for k in attrs)}".lower()
    if any(token in blob for token in ("stt", "transcri", "asr", "speech_to_text")):
        return Stage.STT
    if any(token in blob for token in ("tts", "synthe", "voice")):
        return Stage.TTS
    if any(token in blob for token in ("llm", "inference", "chat", "generate")):
        return Stage.LLM
    if any(token in blob for token in ("tool", "function")):
        return Stage.TOOL
    if any(token in blob for token in ("vad", "endpoint", "eou")):
        return Stage.ENDPOINTING
    return Stage.E2E
