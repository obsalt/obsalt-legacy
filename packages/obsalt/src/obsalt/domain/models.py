"""Immutable domain facts. Measurements never become invented span positions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from obsalt.domain.enums import (
    AnalysisExecutionState,
    CallDirection,
    CallStatus,
    EvidenceKind,
    GroundingKind,
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Signal,
    SignalCoverageStatus,
    Speaker,
    Stage,
    Statistic,
    TimelineFidelity,
    ToolStatus,
)


class ProvenanceStamp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: Provenance
    source_path: str | None = None
    derivation: str | None = None


class StageMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    stage: Stage
    metric: Metric
    value_ms: float
    turn_index: int | None = None
    placement: MeasurementPlacement
    started_at: datetime | None = None
    ended_at: datetime | None = None
    resolution_ms: float | None = None
    provenance: Provenance
    source_path: str | None = None
    derivation: str | None = None


class AggregateMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    stage: Stage
    metric: Metric
    statistic: Statistic
    value_ms: float
    population: int | None = None
    window: str | None = None
    provenance: Provenance
    source_path: str | None = None


class SignalCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: Signal
    status: SignalCoverageStatus
    reason: str | None = None
    source_path: str | None = None
    decoder_version: str


class Hangup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: HangupReason = HangupReason.UNKNOWN
    party: HangupParty = HangupParty.UNKNOWN
    provider_code: str = ""
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    index: int
    speaker: Speaker
    text_ref: str | None = None
    text: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    interrupted: bool | None = None
    confidence: float | None = None
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    tool_id: str
    name: str
    turn_index: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: ToolStatus = ToolStatus.PENDING
    retry_count: int = 0
    payload_shape: Any = None
    argument_hash: str | None = None
    result_ref: str | None = None
    error: str | None = None
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)


class GroundingRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GroundingKind
    content_ref: str
    provenance: Provenance
    source_path: str | None = None


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    uri: str
    content_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    source_path: str | None = None


class CallIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str
    call_id: str
    source: str
    source_call_id: str
    agent_id: str = "unknown"
    agent_version: str | None = None


class CallLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: CallStatus = CallStatus.UNKNOWN
    pipeline_architecture: PipelineArchitecture | None = None
    timeline_fidelity: TimelineFidelity = TimelineFidelity.NONE
    cost: float | None = None


class CallTelephony(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: CallDirection = CallDirection.UNKNOWN
    from_number: str | None = None
    to_number: str | None = None
    sip_id: str | None = None
    provider_ids: dict[str, str] = Field(default_factory=dict)


class FactConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    field: str | None = None
    left: Any = None
    right: Any = None
    reason: str


class CallRevision(BaseModel):
    """Complete immutable snapshot of a call. Never a shallow last-write-wins row."""

    model_config = ConfigDict(extra="forbid")

    org_id: str
    call_id: str
    revision: int
    identity: CallIdentity
    telephony: CallTelephony = Field(default_factory=CallTelephony)
    lifecycle: CallLifecycle = Field(default_factory=CallLifecycle)
    hangup: Hangup | None = None
    turns: list[Turn] = Field(default_factory=list)
    stage_measurements: list[StageMeasurement] = Field(default_factory=list)
    aggregate_measurements: list[AggregateMeasurement] = Field(default_factory=list)
    tools: list[ToolInvocation] = Field(default_factory=list)
    grounding: list[GroundingRef] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    coverage: list[SignalCoverage] = Field(default_factory=list)
    provenance: dict[str, ProvenanceStamp] = Field(default_factory=dict)
    conflicts: list[FactConflict] = Field(default_factory=list)
    decoder_versions: list[str] = Field(default_factory=list)
    assembler_version: str = "obsalt-assemble/1"
    processing_run_id: str
    envelope_ids: list[str] = Field(default_factory=list)
    rooted: bool = True
    finalized: bool = False


class AnalysisExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str
    call_id: str
    revision: int
    analyzer_id: str
    analyzer_version: str
    rubric_version: str | None = None
    prompt_version: str | None = None
    judge_version: str | None = None
    content_hash: str
    state: AnalysisExecutionState
    error: str | None = None


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str
    call_id: str
    revision: int
    analyzer_id: str
    analyzer_version: str
    kind: str
    passed: bool | None = None
    score: float | None = None
    label: str | None = None
    rationale: str | None = None
    evidence_quotes: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
