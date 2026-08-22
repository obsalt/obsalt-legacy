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
from obsalt.domain.models import FidelityDeclaration
from obsalt.otel.conventions import SPAN_GENERATION, SPAN_PLAYOUT, SPAN_USER_INPUT
from obsalt.plugin.types import InstrumentedClient, PluginManifest, ReadableSpan, SdkConfig


class GeminiLivePlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "gemini_live"
    display_name = "Gemini Live"
    decoder_version = "gemini-live/1"
    capabilities = frozenset({Capability.SDK_INSTRUMENTATION, Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="obsalt.sdk.gemini_live",
        possible_architectures=frozenset({PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVAL, Signal.BARGE_IN}),
        structurally_absent={
            Signal.STT_DURATION: "speech-to-speech has no STT stage",
            Signal.LLM_TTFT: "speech-to-speech has no LLM stage",
            Signal.TTS_DURATION: "speech-to-speech has no TTS stage",
        },
        schema_source="obsalt SDK S2S span shape",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )

    def instrument(self, client: object, cfg: SdkConfig) -> InstrumentedClient:
        return InstrumentedClient(client=client, notes="wraps Gemini Live session; emit user_input/generation/playout")

    def claims(self, span: ReadableSpan) -> int:
        if span.name in {SPAN_USER_INPUT, SPAN_GENERATION, SPAN_PLAYOUT}:
            return 80
        if "gemini" in str((span.attributes or {}).get("gen_ai.provider.name", "")).lower():
            return 20
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        if not spans:
            return
        conv = None
        for span in spans:
            conv = (span.attributes or {}).get("gen_ai.conversation.id") or conv
        if conv:
            yield CallObserved(source_call_id=str(conv), architecture=PipelineArchitecture.SPEECH_TO_SPEECH)
        mapping = {SPAN_USER_INPUT: Stage.USER_INPUT, SPAN_GENERATION: Stage.GENERATION, SPAN_PLAYOUT: Stage.PLAYOUT}
        for span in spans:
            stage = mapping.get(span.name)
            if stage is None:
                continue
            yield StageObserved(
                stage=stage,
                metric=Metric.DURATION,
                value_ms=(span.end_unix_nano - span.start_unix_nano) / 1e6,
                placement=MeasurementPlacement.INTERVAL,
                started_at=datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC),
                ended_at=datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=f"span:{span.name}",
            )
