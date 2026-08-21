"""Hamming voice-agent OpenTelemetry conventions.

Span names and the Hamming 12 attributes follow
https://hamming.ai/resources/opentelemetry-voice-agents-tracing-guide
GenAI attributes are used only on LLM and tool spans.
"""

from __future__ import annotations

# --- Span names ---
SPAN_CALL = "call.lifecycle"
SPAN_TURN_PREFIX = "turn"
SPAN_STT = "stt.transcription"
SPAN_STT_PROVIDER_PREFIX = "stt.provider"
SPAN_STT_FALLBACK_PREFIX = "stt.provider.fallback"
SPAN_STT_SELECTION = "stt.provider_selection"
SPAN_VAD = "vad.end_of_utterance"
SPAN_LLM = "llm.inference"
SPAN_TOOL_PREFIX = "llm.tool_call"
SPAN_TTS = "tts.synthesis"
SPAN_PLAYOUT = "audio.playout"
SPAN_WEBHOOK = "webhook.dispatch"
SPAN_TRANSCRIPT_FINAL = "transcript.finalization"
SPAN_EVAL = "evaluation.assertion_check"

# --- Join keys (every span) ---
CALL_ID = "call.id"
WORKSPACE_ID = "workspace.id"
AGENT_ID = "agent.id"
ROOM_ID = "room.id"
TURN_INDEX = "turn.index"
CONVERSATION_ID = "gen_ai.conversation.id"
TEST_RUN_ID = "test_run.id"
SCENARIO_ID = "scenario.id"

# --- Hamming 12 ---
STT_PROVIDER = "stt.provider"
STT_CONFIDENCE = "stt.confidence"
STT_LATENCY_MS = "stt.latency_ms"
STT_MODEL = "stt.model"
LLM_MODEL = "llm.model"
LLM_TTFT_MS = "llm.ttft_ms"
LLM_TOKENS_IN = "llm.tokens.input"
LLM_TOKENS_OUT = "llm.tokens.output"
TTS_PROVIDER = "tts.provider"
TTS_SYNTHESIS_MS = "tts.synthesis_ms"
TOOL_NAME = "tool.name"
TOOL_EXECUTION_MS = "tool.execution_ms"
CALL_DURATION_MS = "call.duration_ms"

# --- Extra voice debugging (still low cardinality) ---
LLM_FINISH_REASON = "llm.finish_reason"
TTS_FIRST_AUDIO_MS = "tts.first_audio_ms"
TTS_VOICE_ID = "tts.voice_id"
TTS_CHAR_COUNT = "tts.character_count"
VAD_EOU_MS = "vad.end_of_utterance_ms"
CALL_STATUS = "call.status"
CALL_ERROR_TYPE = "call.error_type"
CALL_LANGUAGES = "call.languages"
TOOL_RETRY_COUNT = "tool.retry_count"
TOOL_STATUS_CODE = "tool.status_code"
TOOL_SIDE_EFFECT = "tool.side_effect_type"
WEBHOOK_STATUS = "webhook.status"
TRANSCRIPT_FINAL_STATUS = "transcript.finalization.status"
ASSERTION_ID = "assertion.id"
ASSERTION_RESULT = "assertion.result"
ASSERTION_SCORE = "assertion.score"

# --- Evidence pointers (not payloads) ---
EVIDENCE_TRANSCRIPT_ID = "evidence.transcript_id"
EVIDENCE_RECORDING_ID = "evidence.recording_id"
EVIDENCE_REDACTION = "evidence.redaction_state"

# --- GenAI ---
GENAI_OPERATION = "gen_ai.operation.name"
GENAI_PROVIDER = "gen_ai.provider.name"
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_USAGE_IN = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUT = "gen_ai.usage.output_tokens"
GENAI_TOOL_NAME = "gen_ai.tool.name"
GENAI_TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"

# --- Metrics (Prometheus-safe names; no call_id labels) ---
METRIC_CALLS = "voice_calls_total"
METRIC_LATENCY = "voice_response_latency_seconds"
METRIC_TOOL_FAIL = "voice_tool_failures_total"
METRIC_LOW_CONFIDENCE = "voice_low_confidence_turns_total"
METRIC_ASSERT_FAIL = "voice_assertion_failures_total"

LATENCY_BUCKETS = (0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)

HAMMING_12 = (
    STT_PROVIDER,
    STT_CONFIDENCE,
    STT_LATENCY_MS,
    LLM_MODEL,
    LLM_TTFT_MS,
    LLM_TOKENS_IN,
    LLM_TOKENS_OUT,
    TTS_PROVIDER,
    TTS_SYNTHESIS_MS,
    TOOL_NAME,
    TOOL_EXECUTION_MS,
    CALL_DURATION_MS,
)

JOIN_KEYS = (CALL_ID, WORKSPACE_ID, AGENT_ID, CONVERSATION_ID)

TURN_SPEAKER = "turn.speaker"
AUDIO_PLAYOUT_MS = "audio.playout_ms"

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


def turn_span_name(index: int) -> str:
    return f"{SPAN_TURN_PREFIX}.{int(index)}"


def stt_provider_span_name(provider: str, *, fallback: bool = False) -> str:
    slug = (provider or "unknown").lower().replace(" ", "_")
    prefix = SPAN_STT_FALLBACK_PREFIX if fallback else SPAN_STT_PROVIDER_PREFIX
    return f"{prefix}.{slug}"


def tool_span_name(name: str) -> str:
    slug = (name or "unknown").replace(" ", "_")
    return f"{SPAN_TOOL_PREFIX}.{slug}"


def join_attributes(
    call_id: str,
    workspace_id: str,
    agent_id: str,
    *,
    turn_index: int | None = None,
    conversation_id: str | None = None,
) -> dict[str, str | int]:
    """Identity keys copied onto every span so Tempo can join evidence."""
    attrs: dict[str, str | int] = {
        CALL_ID: call_id,
        WORKSPACE_ID: workspace_id,
        AGENT_ID: agent_id,
        CONVERSATION_ID: conversation_id or call_id,
    }
    if turn_index is not None:
        attrs[TURN_INDEX] = int(turn_index)
    return attrs


def known_provider(name: str | None) -> str | None:
    if not name:
        return None
    slug = str(name).strip()
    if not slug or slug.lower() in {"unknown", "none", "n/a"}:
        return None
    return slug
