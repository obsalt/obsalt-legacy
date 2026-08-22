"""OpenTelemetry conventions. Span names are low-cardinality; variables go on attributes."""

from __future__ import annotations

from collections.abc import Mapping

# Span names (v2) — never interpolate turn index or tool name into the name.
SPAN_CALL = "call.lifecycle"
SPAN_TURN = "turn"
SPAN_STT = "stt.transcription"
SPAN_STT_PROVIDER_ATTEMPT = "stt.provider_attempt"
SPAN_STT_SELECTION = "stt.provider_selection"
SPAN_VAD = "vad.end_of_utterance"
SPAN_LLM = "llm.inference"
SPAN_TOOL = "execute_tool"
SPAN_TTS = "tts.synthesis"
SPAN_PLAYOUT = "audio.playout"
SPAN_WEBHOOK = "webhook.dispatch"
SPAN_TRANSCRIPT_FINAL = "transcript.finalization"
SPAN_EVAL = "evaluation.assertion_check"
SPAN_USER_INPUT = "user_input"
SPAN_GENERATION = "generation"

# Join / identity
CALL_ID = "call.id"
PROVIDER_CALL_ID = "call.provider_id"
ORG_ID = "obsalt.org"
AGENT_ID = "agent.id"
TURN_INDEX = "turn.index"
CONVERSATION_ID = "gen_ai.conversation.id"

# Merged GenAI
GENAI_OPERATION = "gen_ai.operation.name"
GENAI_PROVIDER = "gen_ai.provider.name"
GENAI_SYSTEM = "gen_ai.system"  # accept both; Pipecat docs vs code
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_USAGE_IN = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUT = "gen_ai.usage.output_tokens"
GENAI_AUDIO_IN = "gen_ai.usage.audio.input_tokens"
GENAI_AUDIO_OUT = "gen_ai.usage.audio.output_tokens"
GENAI_AUDIO_IN_LIVEKIT = "gen_ai.usage.input_audio_tokens"  # accept both
GENAI_TOOL_NAME = "gen_ai.tool.name"
GENAI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GENAI_OUTPUT_TYPE = "gen_ai.output.type"
ERROR_TYPE = "error.type"

# obsalt.* for what the spec leaves open
OBSALT_BARGE_IN = "obsalt.barge_in"
OBSALT_ENDPOINTING_MS = "obsalt.endpointing_ms"
OBSALT_STT_CONFIDENCE = "obsalt.stt.confidence"
OBSALT_TTFA_MS = "obsalt.ttfa_ms"
OBSALT_FIDELITY = "obsalt.timeline_fidelity"
OBSALT_PROVENANCE = "obsalt.provenance"
OBSALT_AS_ROOT = "obsalt.as_root"

# PII lives only under this prefix. Default exporter strips it.
PII_PREFIX = "obsalt.pii."
PII_USER_TRANSCRIPT = "obsalt.pii.user_transcript"
PII_AGENT_TRANSCRIPT = "obsalt.pii.agent_transcript"
PII_TOOL_ARGUMENTS = "obsalt.pii.tool.arguments"

STT_PROVIDER = "stt.provider"
STT_FALLBACK = "stt.fallback"
TTS_PROVIDER = "tts.provider"
TURN_SPEAKER = "turn.speaker"

# Belt-and-braces denylist for tests — not the redaction mechanism.
PII_FORBIDDEN_ATTR_KEYS = frozenset(
    {
        "stt.transcript",
        "transcript",
        "transcript.text",
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "tool.arguments",
        "tool.result",
        "phone",
        "phone_number",
        "email",
        "customer.number",
    }
)

METRIC_CALL_DURATION = "voice.call.duration"
METRIC_STAGE_DURATION = "voice.stage.duration"
METRIC_TURN_COUNT = "voice.turn.count"
METRIC_INTERRUPTION = "voice.interruption.count"
METRIC_TOOL_FAILURES = "voice.tool.failures"
METRIC_EVAL_FAILURES = "voice.eval.failures"


def strip_pii_attributes(attrs: dict[str, object], *, emit_pii: bool = False) -> dict[str, object]:
    """Strip ``obsalt.pii.*`` from *our* derived export. Never used to mutate a raw OTLP batch."""

    if emit_pii:
        return dict(attrs)
    return {k: v for k, v in attrs.items() if not str(k).startswith(PII_PREFIX)}


def genai_provider_name(attrs: Mapping[str, object] | None) -> str | None:
    """Accept both ``gen_ai.provider.name`` (code) and ``gen_ai.system`` (docs / Azure)."""

    attrs = attrs or {}
    for key in (GENAI_PROVIDER, GENAI_SYSTEM):
        value = attrs.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def genai_provider_name_key(attrs: Mapping[str, object] | None) -> str | None:
    attrs = attrs or {}
    if attrs.get(GENAI_PROVIDER) not in (None, ""):
        return GENAI_PROVIDER
    if attrs.get(GENAI_SYSTEM) not in (None, ""):
        return GENAI_SYSTEM
    return None


def genai_audio_input_tokens(attrs: Mapping[str, object] | None) -> tuple[int | None, str | None]:
    """Accept merged ``gen_ai.usage.audio.input_tokens`` and LiveKit ``gen_ai.usage.input_audio_tokens``."""

    attrs = attrs or {}
    for key in (GENAI_AUDIO_IN, GENAI_AUDIO_IN_LIVEKIT):
        raw = attrs.get(key)
        if raw is None:
            continue
        try:
            return int(raw), key
        except (TypeError, ValueError):
            continue
    return None, None


def setup_tracing(*, otlp_endpoint: str | None = None, emit_pii: bool = False) -> None:
    """Configure the process tracer. Default exporter strips obsalt.pii.*."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": "voice-agent"}))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces")
    else:
        exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def assert_no_pii_in_name(name: str) -> None:
    lowered = name.lower()
    for token in ("@", "transcript", "email"):
        if token in lowered:
            raise AssertionError(f"span name {name!r} looks like it contains content")
