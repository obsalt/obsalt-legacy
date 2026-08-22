from __future__ import annotations

from enum import StrEnum


class TimelineFidelity(StrEnum):
    STAGE_LEVEL = "stage_level"
    TURN_LEVEL = "turn_level"
    MESSAGE_LEVEL = "message_level"
    CALL_LEVEL = "call_level"
    NONE = "none"


class MeasurementPlacement(StrEnum):
    INTERVAL = "interval"
    ANCHORED_DURATION = "anchored_duration"
    UNPLACED = "unplaced"
    COARSE_ANCHOR = "coarse_anchor"


class PipelineArchitecture(StrEnum):
    CASCADE = "cascade"
    SPEECH_TO_SPEECH = "speech_to_speech"
    HYBRID = "hybrid"


class Provenance(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    OBSALT_DERIVED = "obsalt_derived"


class SignalCoverageStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    REDACTED = "redacted"
    DECODE_FAILED = "decode_failed"
    UNSUPPORTED = "unsupported"


class Stage(StrEnum):
    VAD = "vad"
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    PLAYOUT = "playout"
    TOOL = "tool"
    E2E = "e2e"
    TTFA = "ttfa"
    TRANSPORT = "transport"
    ENDPOINTING = "endpointing"
    USER_INPUT = "user_input"
    GENERATION = "generation"


class Metric(StrEnum):
    DURATION = "duration"
    TTFT = "ttft"
    TTFB = "ttfb"
    FIRST_AUDIO = "first_audio"


class Statistic(StrEnum):
    MEAN = "mean"
    P50 = "p50"
    P90 = "p90"
    P95 = "p95"
    P99 = "p99"
    MIN = "min"
    MAX = "max"


class Signal(StrEnum):
    STT_DURATION = "stt_duration"
    LLM_TTFT = "llm_ttft"
    LLM_DURATION = "llm_duration"
    TTS_DURATION = "tts_duration"
    TTS_TTFB = "tts_ttfb"
    E2E_DURATION = "e2e_duration"
    TTFA = "ttfa"
    ENDPOINTING = "endpointing"
    VAD = "vad"
    STT_CONFIDENCE = "stt_confidence"
    TOOL_TIMING = "tool_timing"
    TOOL_RESULT = "tool_result"
    BARGE_IN = "barge_in"
    TURN_INTERVAL = "turn_interval"
    STAGE_INTERVAL = "stage_interval"
    RECORDING = "recording"
    COST = "cost"
    TRANSCRIPT = "transcript"
    GROUNDING_PROMPT = "grounding_prompt"
    GROUNDING_TOOLS = "grounding_tools"
    GROUNDING_USER = "grounding_user"
    GROUNDING_KNOWLEDGE = "grounding_knowledge"
    INTERRUPTION_COUNT = "interruption_count"
    TRANSPORT = "transport"
    WORD_TIMING = "word_timing"
    HANGUP = "hangup"


class Speaker(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"
    UNKNOWN = "unknown"


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class CallStatus(StrEnum):
    ONGOING = "ongoing"
    ENDED = "ended"
    ERROR = "error"
    UNROOTED = "unrooted"
    UNKNOWN = "unknown"


class HangupParty(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class HangupReason(StrEnum):
    """Provider-agnostic hangup taxonomy. Keep this list stable."""

    USER_HANGUP = "user_hangup"
    AGENT_HANGUP = "agent_hangup"
    TRANSFER = "transfer"
    VOICEMAIL = "voicemail"
    INACTIVITY = "inactivity"
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_DURATION = "max_duration"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    DIAL_FAILED = "dial_failed"
    ERROR_STT = "error_stt"
    ERROR_LLM = "error_llm"
    ERROR_TTS = "error_tts"
    ERROR_TOOL = "error_tool"
    ERROR_TELEPHONY = "error_telephony"
    ERROR_UNKNOWN = "error_unknown"
    SPAM = "spam"
    CONCURRENCY = "concurrency"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    PENDING = "pending"


class HallucinationKind(StrEnum):
    UNGROUNDED_FACT = "ungrounded_fact"
    PHANTOM_TOOL_SUCCESS = "phantom_tool_success"
    FABRICATED_ID = "fabricated_id"
    PRICE_CLAIM = "price_claim"
    POLICY_CLAIM = "policy_claim"
    COMMITMENT = "commitment"
    PRIVATE_KNOWLEDGE = "private_knowledge"


class AnalysisState(StrEnum):
    PENDING = "pending"
    SAMPLED_OUT = "sampled_out"
    BUDGET_BLOCKED = "budget_blocked"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class EnvelopeState(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    DECODING = "decoding"
    ASSEMBLED = "assembled"
    FAILED = "failed"
    TOMBSTONED = "tombstoned"


class KeyScope(StrEnum):
    INGEST = "ingest"
    READ = "read"
    ANALYZE = "analyze"
    ADMIN = "admin"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    REVIEWER = "reviewer"


class Capability(StrEnum):
    WEBHOOK_SOURCE = "webhook_source"
    OTLP_MAPPER = "otlp_mapper"
    REST_BACKFILL = "rest_backfill"
    STREAM_SOURCE = "stream_source"
    SDK_INSTRUMENTATION = "sdk_instrumentation"
    AUTHENTICATION = "authentication"
    JUDGE = "judge"
    EMBEDDER = "embedder"
    REDACTOR = "redactor"


class ObservationalEventKind(StrEnum):
    CALL_STARTED = "call_started"
    CALL_ENDED = "call_ended"
    CALL_ANALYZED = "call_analyzed"
    TRANSCRIPT = "transcript"
    TOOL_OBSERVATION = "tool_observation"
    STATUS = "status"
    SNAPSHOT = "snapshot"
    INTERRUPTION = "interruption"
    UNKNOWN_OBSERVATIONAL = "unknown_observational"
    REJECTED_SYNCHRONOUS = "rejected_synchronous"
    OTLP_BATCH = "otlp_batch"


class VerifyOutcome(StrEnum):
    OK = "ok"
    MALFORMED = "malformed"
    MISSING_CREDENTIAL = "missing_credential"
    BAD_SIGNATURE = "bad_signature"
    STALE = "stale"
    REPLAYED = "replayed"


class GroundingKind(StrEnum):
    SYSTEM_PROMPT = "system_prompt"
    KNOWLEDGE = "knowledge"
    TOOL_RESULT = "tool_result"
    USER_TEXT = "user_text"


class EvidenceKind(StrEnum):
    TRANSCRIPT = "transcript"
    RECORDING = "recording"
    TOOL_PAYLOAD = "tool_payload"
    OTLP_BATCH = "otlp_batch"


def speaker_from(role: str | None) -> Speaker:
    if not role:
        return Speaker.UNKNOWN
    value = role.strip().lower()
    if value in {"user", "customer", "human", "caller"}:
        return Speaker.USER
    if value in {"agent", "assistant", "bot", "ai", "model"}:
        return Speaker.AGENT
    if value in {"system"}:
        return Speaker.SYSTEM
    if value in {
        "tool",
        "function",
        "tool_call_result",
        "tool_call_invocation",
        "agent-action",
    }:
        return Speaker.TOOL
    return Speaker.UNKNOWN
