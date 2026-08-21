from __future__ import annotations

from enum import Enum


class Provider(str, Enum):
    VAPI = "vapi"
    RETELL = "retell"
    BLAND = "bland"
    OPENAI_REALTIME = "openai_realtime"
    NATIVE = "native"


class Speaker(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"
    UNKNOWN = "unknown"


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class CallStatus(str, Enum):
    ONGOING = "ongoing"
    ENDED = "ended"
    ERROR = "error"
    UNKNOWN = "unknown"


class HangupParty(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class HangupReason(str, Enum):
    """Provider-agnostic hangup taxonomy.

    Live voice platforms each invent their own disconnection codes. obsalt
    maps them here so hangup clustering and loss-scoring share one vocabulary.
    """

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


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    PENDING = "pending"


class LatencyComponent(str, Enum):
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    E2E = "e2e"
    TTFA = "ttfa"
    TOOL = "tool"
    S2S = "s2s"
    ENDPOINTING = "endpointing"
    KNOWLEDGE_BASE = "knowledge_base"


class HallucinationKind(str, Enum):
    UNGROUNDED_FACT = "ungrounded_fact"
    PHANTOM_TOOL_SUCCESS = "phantom_tool_success"
    FABRICATED_ID = "fabricated_id"
    PRICE_CLAIM = "price_claim"
    POLICY_CLAIM = "policy_claim"
    COMMITMENT = "commitment"
    PRIVATE_KNOWLEDGE = "private_knowledge"
