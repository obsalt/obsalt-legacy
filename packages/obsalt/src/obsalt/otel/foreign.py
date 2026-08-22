"""Foreign convention mappers: OpenInference and OpenLLMetry.

Priority is below first-party source mappers (obsalt.*, Pipecat, LiveKit, ElevenLabs)
so a more specific plugin always wins. Core ships conventions, not providers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
    ToolStatus,
)
from obsalt.domain.events import (
    CallObserved,
    NormalizedEvent,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.otel.conventions import CONVERSATION_ID, PROVIDER_CALL_ID
from obsalt.otel.span_time import valid_span_interval
from obsalt.plugin.types import PluginManifest, ReadableSpan

_OPENINFERENCE_KINDS = {
    "LLM": Stage.LLM,
    "CHAIN": Stage.LLM,
    "AGENT": Stage.LLM,
    "TOOL": Stage.TOOL,
    "EMBEDDING": Stage.LLM,
    "RETRIEVER": Stage.LLM,
    "RERANKER": Stage.LLM,
    "GUARDRAIL": Stage.LLM,
}

_OPENLLMETRY_HINTS = ("llm.", "traceloop.")


class ForeignConventionMapper:
    """Accept OpenInference and OpenLLMetry spans that no source plugin claimed."""

    API_VERSION = 1
    name = "foreign-conventions"
    display_name = "OpenInference / OpenLLMetry"
    decoder_version = "foreign/1"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="openinference+openllmetry",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVAL, Signal.TURN_INTERVAL, Signal.TOOL_TIMING}),
        structurally_absent={},
        schema_source="openinference.span.kind, input.value; llm.*, traceloop.*",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: ReadableSpan) -> int:
        attrs = span.attributes or {}
        if attrs.get("openinference.span.kind"):
            return 20
        if any(str(key).startswith(_OPENLLMETRY_HINTS) for key in attrs):
            return 15
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        conv = None
        for span in spans:
            attrs = span.attributes or {}
            conv = (
                attrs.get(CONVERSATION_ID)
                or attrs.get(PROVIDER_CALL_ID)
                or attrs.get("session.id")
                or attrs.get("traceloop.association.properties.conversation_id")
                or conv
            )
        if conv:
            yield CallObserved(
                source_call_id=str(conv),
                architecture=PipelineArchitecture.CASCADE,
                provenance_by_field={
                    "source_call_id": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="openinference/openllmetry",
                    )
                },
            )
        turn_index = 0
        for span in spans:
            attrs = span.attributes or {}
            if not valid_span_interval(span):
                continue
            started = datetime.fromtimestamp((span.start_unix_nano or 0) / 1e9, tz=UTC)
            ended = datetime.fromtimestamp((span.end_unix_nano or 0) / 1e9, tz=UTC)
            ms = (span.end_unix_nano - span.start_unix_nano) / 1e6
            kind = str(attrs.get("openinference.span.kind") or "")
            stage = _OPENINFERENCE_KINDS.get(kind.upper()) or _stage_from_llm_attrs(
                attrs, span.name
            )
            if kind.upper() == "TOOL" or stage is Stage.TOOL:
                yield ToolObserved(
                    tool_id=str(
                        attrs.get("gen_ai.tool.call.id") or attrs.get("tool.id") or span.span_id
                    ),
                    name=str(
                        attrs.get("gen_ai.tool.name")
                        or attrs.get("tool.name")
                        or span.name
                        or "tool"
                    ),
                    started_at=started,
                    ended_at=ended,
                    status=ToolStatus.SUCCESS,
                )
            input_value = attrs.get("input.value") or attrs.get("llm.prompts")
            if input_value and kind.upper() in {"LLM", "CHAIN", "AGENT"}:
                yield TurnObserved(
                    turn_index=turn_index,
                    speaker=Speaker.USER,
                    text=str(input_value)[:2000],
                    started_at=started,
                    ended_at=ended,
                )
                turn_index += 1
            yield StageObserved(
                stage=stage,
                metric=Metric.DURATION,
                value_ms=ms,
                placement=MeasurementPlacement.INTERVAL,
                started_at=started,
                ended_at=ended,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=f"span:{span.name}:{kind or 'llm'}",
            )


def _stage_from_llm_attrs(attrs: dict[str, object], name: str) -> Stage:
    blob = f"{name} {' '.join(str(k) for k in attrs)}".lower()
    if any(token in blob for token in ("tool", "function")):
        return Stage.TOOL
    if any(token in blob for token in ("stt", "transcri", "asr")):
        return Stage.STT
    if any(token in blob for token in ("tts", "synthe")):
        return Stage.TTS
    return Stage.LLM
