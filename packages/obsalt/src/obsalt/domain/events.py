"""Normalized events emitted by decoders. Small facts, not whole-call snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from obsalt.domain.enums import (
    CallDirection,
    EvidenceKind,
    GroundingKind,
    HangupParty,
    HangupReason,
    InterruptionKind,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Speaker,
    Stage,
    Statistic,
    ToolStatus,
)


class SourceRevision(BaseModel):
    """Ordered provider revision. A content hash is not a source revision."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sequence", "timestamp", "version"]
    value: str
    scope: str = "call"


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str | None = None
    call_key: str | None = None
    fact_id: str | None = None
    envelope_id: str | None = None
    decoder_version: str | None = None
    processing_run_id: str | None = None
    event_occurred_at: datetime | None = None
    envelope_sequence: int | None = None
    source_revision: SourceRevision | None = None
    provenance_by_field: dict[str, Provenance] = Field(default_factory=dict)


class CallObserved(EventBase):
    type: Literal["call_observed"] = "call_observed"
    source_call_id: str
    agent_id: str = "unknown"
    agent_version: str | None = None
    direction: CallDirection = CallDirection.UNKNOWN
    started_at: datetime | None = None
    architecture: PipelineArchitecture | None = None
    from_number: str | None = None
    to_number: str | None = None
    source_path: str | None = None


class TurnObserved(EventBase):
    type: Literal["turn_observed"] = "turn_observed"
    turn_index: int
    speaker: Speaker
    text: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    confidence: float | None = None
    interrupted: bool | None = None
    source_path: str | None = None


class StageObserved(EventBase):
    type: Literal["stage_observed"] = "stage_observed"
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


class AggregateObserved(EventBase):
    type: Literal["aggregate_observed"] = "aggregate_observed"
    stage: Stage
    metric: Metric
    statistic: Statistic
    value_ms: float
    population: int | None = None
    window: str | None = None
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None


class ToolObserved(EventBase):
    type: Literal["tool_observed"] = "tool_observed"
    tool_id: str
    name: str
    turn_index: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: ToolStatus = ToolStatus.PENDING
    args: Any = None
    result: Any = None
    error: str | None = None
    source_path: str | None = None


class OutcomeObserved(EventBase):
    type: Literal["outcome_observed"] = "outcome_observed"
    provider_code: str
    reason: HangupReason | None = None
    party: HangupParty | None = None
    ended_at: datetime | None = None
    cost: float | None = None
    source_path: str | None = None


class GroundingObserved(EventBase):
    type: Literal["grounding_observed"] = "grounding_observed"
    kind: GroundingKind
    content: str
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None


class EvidenceObserved(EventBase):
    type: Literal["evidence_observed"] = "evidence_observed"
    kind: EvidenceKind
    uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance = Provenance.PROVIDER_REPORTED
    source_path: str | None = None


class InterruptionObserved(EventBase):
    type: Literal["interruption_observed"] = "interruption_observed"
    turn_index: int | None = None
    count: int | None = None
    kind: InterruptionKind
    source_path: str | None = None


class SnapshotBoundaryObserved(EventBase):
    type: Literal["snapshot_boundary"] = "snapshot_boundary"
    authoritative_domains: list[str] = Field(default_factory=list)
    source_revision: SourceRevision | None = None


class FactRetracted(EventBase):
    type: Literal["fact_retracted"] = "fact_retracted"
    retracted_fact_id: str
    source_revision: SourceRevision | None = None


class CallFinalized(EventBase):
    type: Literal["call_finalized"] = "call_finalized"
    reason: str = "provider"


NormalizedEvent = Annotated[
    CallObserved
    | TurnObserved
    | StageObserved
    | AggregateObserved
    | ToolObserved
    | OutcomeObserved
    | GroundingObserved
    | EvidenceObserved
    | InterruptionObserved
    | SnapshotBoundaryObserved
    | FactRetracted
    | CallFinalized,
    Field(discriminator="type"),
]
