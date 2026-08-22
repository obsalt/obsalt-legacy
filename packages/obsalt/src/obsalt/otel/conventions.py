"""Voice semantic conventions.

Adopt merged OTel GenAI attributes. Own only what the spec leaves open under
``obsalt.*``. Span names are low-cardinality — variables move to attributes.

Conversational content lives under ``obsalt.pii.*``. Default export strips it.
The denylist is a belt-and-braces test assertion, not the mechanism.
"""

from __future__ import annotations

# Span names (low cardinality)
SPAN_CALL = "call.lifecycle"
SPAN_TURN = "turn"
SPAN_STT = "stt.transcription"
SPAN_STT_PROVIDER_ATTEMPT = "stt.provider_attempt"
SPAN_VAD = "vad.end_of_utterance"
SPAN_LLM = "llm.inference"
SPAN_TOOL = "execute_tool"
SPAN_TTS = "tts.synthesis"
SPAN_PLAYOUT = "audio.playout"
SPAN_EVAL = "evaluation.assertion_check"

# Join keys
CALL_ID = "call.id"
PROVIDER_CALL_ID = "call.provider_id"
ORG_ID = "obsalt.org"
AGENT_ID = "agent.id"
TURN_INDEX = "turn.index"
CONVERSATION_ID = "gen_ai.conversation.id"

# GenAI — merged
GENAI_OPERATION = "gen_ai.operation.name"
GENAI_PROVIDER = "gen_ai.provider.name"
GENAI_SYSTEM = "gen_ai.system"
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_USAGE_IN = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUT = "gen_ai.usage.output_tokens"
GENAI_AUDIO_IN = "gen_ai.usage.audio.input_tokens"
GENAI_AUDIO_OUT = "gen_ai.usage.audio.output_tokens"
GENAI_AUDIO_IN_LIVEKIT = "gen_ai.usage.input_audio_tokens"
GENAI_OUTPUT_TYPE = "gen_ai.output.type"
GENAI_TOOL_NAME = "gen_ai.tool.name"
GENAI_TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"

# obsalt.* for what the spec leaves open
OBSALT_BARGE_IN = "obsalt.barge_in"
OBSALT_ENDPOINTING_MS = "obsalt.endpointing_ms"
OBSALT_STT_CONFIDENCE = "obsalt.stt.confidence"
OBSALT_TTFA_MS = "obsalt.ttfa_ms"
OBSALT_FIDELITY = "obsalt.timeline_fidelity"
OBSALT_PROVENANCE = "obsalt.provenance"
OBSALT_RECORDING_REF = "obsalt.recording.ref"
OBSALT_AS_ROOT = "obsalt.as_root"

# Marked PII namespace
PII_PREFIX = "obsalt.pii."
PII_USER_TRANSCRIPT = "obsalt.pii.user_transcript"
PII_AGENT_TRANSCRIPT = "obsalt.pii.agent_transcript"
PII_TOOL_ARGS = "obsalt.pii.tool.arguments"
PII_TOOL_RESULT = "obsalt.pii.tool.result"

# Belt-and-braces denylist for tests (not the export mechanism)
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

# Derived metrics (not spans)
METRIC_CALL_DURATION = "voice.call.duration"
METRIC_STAGE_DURATION = "voice.stage.duration"
METRIC_TURN_COUNT = "voice.turn.count"
METRIC_INTERRUPTION = "voice.interruption.count"
METRIC_TOOL_FAIL = "voice.tool.failures"
METRIC_EVAL_FAIL = "voice.eval.failures"

# Proposed GenAI attributes, off by default
PROPOSED_GENAI_FLAG = "obsalt.otel.proposed_genai"


def turn_span_name(_index: int | None = None) -> str:
    return SPAN_TURN


def stt_provider_span_name(_provider: str | None = None, *, fallback: bool = False) -> str:
    _ = fallback
    return SPAN_STT_PROVIDER_ATTEMPT


def tool_span_name(_name: str | None = None) -> str:
    return SPAN_TOOL


def is_pii_attr(key: str) -> bool:
    return key.startswith(PII_PREFIX) or key in PII_FORBIDDEN_ATTR_KEYS
