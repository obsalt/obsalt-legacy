from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from obsalt.domain.enums import (
    AnalysisState,
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
from obsalt.util import utcnow


class ProvenanceStamp(BaseModel):
    provenance: Provenance
    source_path: str | None = None
    derivation: str | None = None


class StageMeasurement(BaseModel):
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
    signal: Signal
    status: SignalCoverageStatus
    reason: str | None = None
    source_path: str | None = None
    decoder_version: str


class FidelityDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_format: str
    possible_architectures: frozenset[PipelineArchitecture]
    possible_placements: frozenset[MeasurementPlacement]
    provides: frozenset[Signal]
    structurally_absent: dict[Signal, str] = Field(default_factory=dict)
    schema_source: str
    schema_revision: str
    verified_at: date


class Hangup(BaseModel):
    reason: HangupReason = HangupReason.UNKNOWN
    party: HangupParty = HangupParty.UNKNOWN
    provider_code: str = ""
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None
    last_speaker: Speaker | None = None
    last_user_text_ref: str | None = None
    last_agent_text_ref: str | None = None
    loss_score: float = 0.0
    loss_reasons: list[str] = Field(default_factory=list)


class Turn(BaseModel):
    index: int
    speaker: Speaker
    text_ref: str | None = None
    text: str = ""  # hydrated for analysis; not stored in the hot table
    started_at: datetime | None = None
    ended_at: datetime | None = None
    interrupted: bool = False
    confidence: float | None = None
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    id: str
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
    kind: GroundingKind
    content_ref: str
    provenance: Provenance
    source_path: str | None = None


class EvidenceRef(BaseModel):
    kind: EvidenceKind
    uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None


class AnalysisExecution(BaseModel):
    call_id: str
    revision: str
    analyzer_id: str
    analyzer_version: str
    rubric_version: str | None = None
    prompt_version: str | None = None
    judge_version: str | None = None
    state: AnalysisState = AnalysisState.PENDING
    error: str | None = None
    content_hash: str | None = None


class AnalysisResult(BaseModel):
    execution: AnalysisExecution
    payload: dict[str, Any] = Field(default_factory=dict)


class CallRevision(BaseModel):
    """Complete immutable snapshot. Never a shallow 'latest' row."""

    org_id: str
    call_id: str
    revision: str
    source: str
    source_call_id: str
    agent_id: str = "unknown"
    agent_version: str | None = None
    direction: CallDirection = CallDirection.UNKNOWN
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: CallStatus = CallStatus.UNKNOWN
    pipeline_architecture: PipelineArchitecture = PipelineArchitecture.CASCADE
    timeline_fidelity: TimelineFidelity = TimelineFidelity.NONE
    cost: float | None = None
    hangup: Hangup | None = None
    turns: list[Turn] = Field(default_factory=list)
    stage_measurements: list[StageMeasurement] = Field(default_factory=list)
    aggregate_measurements: list[AggregateMeasurement] = Field(default_factory=list)
    tools: list[ToolInvocation] = Field(default_factory=list)
    grounding: list[GroundingRef] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    coverage: list[SignalCoverage] = Field(default_factory=list)
    provenance: dict[str, ProvenanceStamp] = Field(default_factory=dict)
    decoder_version: str = ""
    assembler_version: str = "1"
    processing_run_id: str = ""
    conflicts: list[str] = Field(default_factory=list)
    rooted: bool = True
    created_at: datetime = Field(default_factory=utcnow)

    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.speaker == Speaker.USER]

    def agent_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.speaker == Speaker.AGENT]


class Rubric(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    version: int = 1
    threshold: float = 0.7
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
