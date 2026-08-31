from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from obsalt.domain.enums import (
    Capability,
    GroundingKind,
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
    GroundingObserved,
    NormalizedEvent,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.otel.conventions import (
    AGENT_ID,
    CONVERSATION_ID,
    PROVIDER_CALL_ID,
    SPAN_CALL,
    SPAN_LLM,
    SPAN_STT,
    SPAN_STT_PROVIDER_ATTEMPT,
    SPAN_TOOL,
    SPAN_TTS,
    SPAN_TURN,
    agent_id_from_attrs,
    genai_provider_name,
    genai_provider_name_key,
)
from obsalt.otel.s2s import outcome_from_span_attrs
from obsalt.otel.span_time import valid_span_interval
from obsalt.plugin import PLUGIN_API_VERSION
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
        possible_architectures=frozenset(
            {PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}
        ),
        possible_placements=frozenset(
            {MeasurementPlacement.INTERVAL, MeasurementPlacement.ANCHORED_DURATION}
        ),
        provides=frozenset(
            {
                Signal.STAGE_INTERVAL,
                Signal.TURN_INTERVAL,
                Signal.TTFA,
                Signal.GROUNDING_USER,
                Signal.HANGUP,
            }
        ),
        structurally_absent={},
        schema_source="Pipecat tracing (metrics.ttfb, turn.*, gen_ai.provider.name in code; gen_ai.system in docs)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: ReadableSpan) -> int:
        attrs = span.attributes or {}
        if "metrics.ttfb" in attrs or str(attrs.get("turn.index", "")).isdigit():
            return 70
        if span.name in {
            SPAN_TURN,
            SPAN_STT,
            SPAN_STT_PROVIDER_ATTEMPT,
            SPAN_LLM,
            SPAN_TTS,
            SPAN_TOOL,
        }:
            return 30
        if span.name == SPAN_CALL:
            return 40
        if genai_provider_name(attrs):
            return 25
        return 0

    def decode(self, spans: Sequence[ReadableSpan]) -> Iterable[NormalizedEvent]:
        conv = None
        provider_key = None
        agent_id = None
        agent_key = None
        outcome = None
        for span in spans:
            attrs = span.attributes or {}
            conv = attrs.get(CONVERSATION_ID) or attrs.get(PROVIDER_CALL_ID) or conv
            provider_key = provider_key or genai_provider_name_key(attrs)
            if agent_id is None:
                found = agent_id_from_attrs(attrs)
                if found:
                    agent_id = found
                    agent_key = (
                        AGENT_ID if attrs.get(AGENT_ID) not in (None, "") else "obsalt.agent.id"
                    )
            found_outcome = outcome_from_span_attrs(attrs)
            if found_outcome is not None:
                outcome = found_outcome
        provenance: dict[str, ProvenanceStamp] = {}
        if agent_id and agent_key:
            provenance["agent_id"] = ProvenanceStamp(
                provenance=Provenance.PROVIDER_REPORTED, source_path=agent_key
            )
        elif provider_key:
            provenance["agent_id"] = ProvenanceStamp(
                provenance=Provenance.PROVIDER_REPORTED, source_path=provider_key
            )
        if conv:
            yield CallObserved(
                source_call_id=str(conv),
                agent_id=agent_id,
                architecture=PipelineArchitecture.CASCADE,
                provenance_by_field=provenance,
            )
        if outcome is not None:
            yield outcome
        for span in spans:
            attrs = span.attributes or {}
            start_ns = span.start_unix_nano or 0
            end_ns = span.end_unix_nano or 0
            interval_ok = valid_span_interval(span)
            started = datetime.fromtimestamp(start_ns / 1e9, tz=UTC) if start_ns > 0 else None
            ended = datetime.fromtimestamp(end_ns / 1e9, tz=UTC) if interval_ok else None
            ms = (end_ns - start_ns) / 1e6 if interval_ok else None
            turn_index = attrs.get("turn.index")
            turn_i = int(turn_index) if turn_index is not None else None
            if span.name == SPAN_TURN:
                speaker = Speaker.AGENT if attrs.get("turn.speaker") == "agent" else Speaker.USER
                text = str(
                    attrs.get("obsalt.pii.user_transcript")
                    or attrs.get("obsalt.pii.agent_transcript")
                    or attrs.get("input.value")
                    or ""
                )
                yield TurnObserved(
                    turn_index=turn_i or 0,
                    speaker=speaker,
                    text=text,
                    started_at=started,
                    ended_at=ended,
                )
                if speaker is Speaker.USER and text:
                    yield GroundingObserved(
                        kind=GroundingKind.USER_TEXT,
                        content=text,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="span.attributes.obsalt.pii.user_transcript",
                    )
                continue
            if span.name == SPAN_TOOL:
                result = attrs.get("obsalt.pii.tool.result") or attrs.get("output.value")
                args = attrs.get("obsalt.pii.tool.arguments") or attrs.get("input.value")
                yield ToolObserved(
                    tool_id=str(
                        attrs.get("tool.id") or attrs.get("gen_ai.tool.call.id") or span.span_id
                    ),
                    name=str(attrs.get("tool.name") or attrs.get("gen_ai.tool.name") or "tool"),
                    turn_index=turn_i,
                    started_at=started,
                    ended_at=ended,
                    args=args
                    if isinstance(args, dict)
                    else ({"value": args} if args is not None else None),
                    result=result,
                )
                if result not in (None, ""):
                    yield GroundingObserved(
                        kind=GroundingKind.TOOL_RESULT,
                        content=str(result),
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="span.attributes.output.value",
                    )
                if interval_ok and ms is not None:
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
            stage = {
                SPAN_STT: Stage.STT,
                SPAN_STT_PROVIDER_ATTEMPT: Stage.STT,
                SPAN_LLM: Stage.LLM,
                SPAN_TTS: Stage.TTS,
            }.get(span.name)
            if stage is None and attrs.get("metrics.ttfb") is not None:
                stage = Stage.TTS
            if stage is None:
                continue
            ttfb = attrs.get("metrics.ttfb")
            if ttfb is not None:
                try:
                    ttfb_ms = float(ttfb)
                except (TypeError, ValueError):
                    ttfb_ms = None
                if ttfb_ms is not None:
                    # TTFB is a measured duration from the span start, not the
                    # full span width. Drawing it as INTERVAL invents a waterfall.
                    yield StageObserved(
                        stage=stage,
                        metric=Metric.TTFB,
                        value_ms=ttfb_ms,
                        turn_index=turn_i,
                        placement=MeasurementPlacement.ANCHORED_DURATION,
                        started_at=started,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="span.attributes.metrics.ttfb",
                    )
            if not interval_ok or ms is None:
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
