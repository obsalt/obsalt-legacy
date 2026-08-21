from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from obsalt.domain.enums import (
    CallDirection,
    CallStatus,
    HallucinationKind,
    HangupParty,
    HangupReason,
    LatencyComponent,
    Provider,
    Speaker,
    ToolStatus,
)
from obsalt.util import utcnow


class LatencySample(BaseModel):
    component: LatencyComponent
    duration_ms: float
    turn_index: int | None = None
    ttft_ms: float | None = None
    ttfb_ms: float | None = None
    source: str = "derived"


class LatencyPercentiles(BaseModel):
    component: LatencyComponent
    count: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    avg_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None


class Turn(BaseModel):
    index: int
    speaker: Speaker
    text: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    seconds_from_start: float | None = None
    duration_ms: float | None = None
    interrupted: bool = False
    stt_ms: float | None = None
    llm_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ms: float | None = None
    tts_ttfb_ms: float | None = None
    time_to_first_audio_ms: float | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    id: str
    name: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    time_to_tool_ms: float | None = None
    status: ToolStatus = ToolStatus.PENDING
    retry_count: int = 0
    payload_shape: Any = None
    argument_hash: str | None = None
    error: str | None = None
    result_preview: str | None = None
    turn_index: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Hangup(BaseModel):
    reason: HangupReason = HangupReason.UNKNOWN
    party: HangupParty = HangupParty.UNKNOWN
    provider_reason: str = ""
    signal: str | None = None
    last_speaker: Speaker | None = None
    last_user_text: str | None = None
    last_agent_text: str | None = None
    loss_score: float = 0.0
    loss_reasons: list[str] = Field(default_factory=list)


class HallucinationFlag(BaseModel):
    kind: HallucinationKind
    span_text: str
    turn_index: int
    confidence: float
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    rubric_id: str
    rubric_name: str
    score: float
    passed: bool
    rationale: str
    quotes: list[str] = Field(default_factory=list)


class Rubric(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    threshold: float = 0.7
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class GroundingContext(BaseModel):
    system_prompt: str = ""
    knowledge: list[str] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    user_text: str = ""


class CanonicalCall(BaseModel):
    id: str
    org_id: str
    provider: Provider
    provider_call_id: str
    agent_id: str = "unknown"
    agent_name: str | None = None
    direction: CallDirection = CallDirection.UNKNOWN
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: CallStatus = CallStatus.UNKNOWN
    recording_url: str | None = None
    transcript_text: str = ""
    turns: list[Turn] = Field(default_factory=list)
    tools: list[ToolInvocation] = Field(default_factory=list)
    latency_samples: list[LatencySample] = Field(default_factory=list)
    hangup: Hangup | None = None
    hallucinations: list[HallucinationFlag] = Field(default_factory=list)
    evals: list[EvalResult] = Field(default_factory=list)
    grounding: GroundingContext = Field(default_factory=GroundingContext)
    cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_event_type: str | None = None
    finalized: bool = False
    ingested_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.speaker == Speaker.USER]

    def agent_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.speaker == Speaker.AGENT]


class IngestResult(BaseModel):
    call_id: str
    provider_call_id: str
    status: Literal["accepted", "merged", "finalized"]
    created: bool
    finalized: bool = False


class NativeSnapshot(BaseModel):
    """JSON body for ``POST /v1/ingest/native``.

    This is obsalt's own envelope — the equivalent of a Vapi
    ``end-of-call-report`` when **you** own the audio loop. Hosted platforms
    never send this; their adapters translate vendor JSON into ``CanonicalCall``
    instead.

    Built by ``CallRecorder.snapshot()`` / ``VoiceCall.snapshot()``.
    """

    call_id: str
    provider: str = "native"
    agent_id: str = "unknown"
    agent_name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None
    hangup_reason: str | None = None
    final: bool = True
    transcript_text: str = ""
    grounding: dict[str, Any] = Field(default_factory=dict)
    turns: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    latency_samples: list[dict[str, Any]] = Field(default_factory=list)
    recording_url: str | None = None
    direction: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    spans_exported: bool = False
    traceparent: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
