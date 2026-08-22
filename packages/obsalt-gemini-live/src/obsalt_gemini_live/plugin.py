from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    PipelineArchitecture,
    Signal,
)
from obsalt.domain.events import NormalizedEvent
from obsalt.domain.models import FidelityDeclaration
from obsalt.otel.conventions import (
    SPAN_GENERATION,
    SPAN_PLAYOUT,
    SPAN_USER_INPUT,
    genai_provider_name,
)
from obsalt.otel.s2s import decode_s2s_spans, instrument_s2s
from obsalt.plugin import PLUGIN_API_VERSION
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
        provides=frozenset({Signal.STAGE_INTERVAL, Signal.TURN_INTERVAL, Signal.BARGE_IN}),
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
        return instrument_s2s(
            client,
            cfg,
            provider="gemini",
            notes="wraps Gemini Live session; emit user_input/generation/playout",
        )

    def claims(self, span: ReadableSpan) -> int:
        if span.name in {SPAN_USER_INPUT, SPAN_GENERATION, SPAN_PLAYOUT}:
            return 80
        provider = (genai_provider_name(span.attributes) or "").lower()
        if "gemini" in provider or "google" in provider:
            return 20
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        yield from decode_s2s_spans(spans, architecture=PipelineArchitecture.SPEECH_TO_SPEECH)
