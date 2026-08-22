from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from obsalt.domain.enums import (
    CallDirection,
    EvidenceKind,
    GroundingKind,
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Speaker,
    Stage,
    Statistic,
    ToolStatus,
)
from obsalt.domain.models import ProvenanceStamp


class SourceRevision(BaseModel):
    """Ordered provider sequence. A content hash is not a source revision."""

    kind: Literal["sequence", "timestamp", "version"]
    value: str
    scope: str = "call"


class EventBase(BaseModel):
    org_id: str | None = None
    call_key: str | None = None
    fact_id: str | None = None
    envelope_id: str | None = None
    decoder_version: str | None = None
    processing_run_id: str | None = None
    event_occurred_at: datetime | None = None
    envelope_sequence: int | None = None
    source_revision: SourceRevision | None = None

    def identity_parts(self) -> dict[str, Any]:
        raise NotImplementedError


class CallObserved(EventBase):
    type: Literal["call_observed"] = "call_observed"
    source_call_id: str
    agent_id: str | None = None
    agent_version: str | None = None
    direction: CallDirection = CallDirection.UNKNOWN
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    architecture: PipelineArchitecture = PipelineArchitecture.CASCADE
    cost: float | None = None
    status: str | None = None
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "source_call_id": self.source_call_id}


class TurnObserved(EventBase):
    type: Literal["turn_observed"] = "turn_observed"
    turn_index: int
    speaker: Speaker
    text: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    confidence: float | None = None
    interrupted: bool = False
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "turn_index": self.turn_index, "speaker": self.speaker.value}


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

    def identity_parts(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "stage": self.stage.value,
            "metric": self.metric.value,
            "turn_index": self.turn_index,
            "source_path": self.source_path,
        }


class AggregateObserved(EventBase):
    type: Literal["aggregate_observed"] = "aggregate_observed"
    stage: Stage
    metric: Metric
    statistic: Statistic
    value_ms: float
    population: int | None = None
    window: str | None = None
    provenance: Provenance
    source_path: str | None = None

    def identity_parts(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "stage": self.stage.value,
            "metric": self.metric.value,
            "statistic": self.statistic.value,
            "source_path": self.source_path,
        }


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
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "tool_id": self.tool_id}


class OutcomeObserved(EventBase):
    type: Literal["outcome_observed"] = "outcome_observed"
    provider_code: str
    reason: HangupReason | None = None
    party: HangupParty | None = None
    ended_at: datetime | None = None
    cost: float | None = None
    provenance_by_field: dict[str, ProvenanceStamp] = Field(default_factory=dict)

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type}


class GroundingObserved(EventBase):
    type: Literal["grounding_observed"] = "grounding_observed"
    kind: GroundingKind
    content: str
    provenance: Provenance
    source_path: str | None = None

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "kind": self.kind.value, "source_path": self.source_path}


class EvidenceObserved(EventBase):
    type: Literal["evidence_observed"] = "evidence_observed"
    kind: EvidenceKind
    uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    source_path: str | None = None

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "kind": self.kind.value, "uri": self.uri}


class InterruptionObserved(EventBase):
    type: Literal["interruption_observed"] = "interruption_observed"
    turn_index: int | None = None
    count: int | None = None
    kind: str = "assistant"

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "kind": self.kind, "turn_index": self.turn_index}


class SnapshotBoundaryObserved(EventBase):
    type: Literal["snapshot_boundary"] = "snapshot_boundary"
    authoritative_domains: list[str] = Field(default_factory=list)
    source_revision: SourceRevision | None = None

    def identity_parts(self) -> dict[str, Any]:
        rev = self.source_revision.value if self.source_revision else None
        return {"type": self.type, "source_revision": rev}


class FactRetracted(EventBase):
    type: Literal["fact_retracted"] = "fact_retracted"
    retracted_fact_id: str
    source_revision: SourceRevision | None = None

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type, "retracted_fact_id": self.retracted_fact_id}


class CallFinalized(EventBase):
    type: Literal["call_finalized"] = "call_finalized"
    reason: str = "provider"

    def identity_parts(self) -> dict[str, Any]:
        return {"type": self.type}


NormalizedEvent = Annotated[
    CallObserved | TurnObserved | StageObserved | AggregateObserved | ToolObserved | OutcomeObserved | GroundingObserved | EvidenceObserved | InterruptionObserved | SnapshotBoundaryObserved | FactRetracted | CallFinalized,
    Field(discriminator="type"),
]

_EVENT_TYPES: dict[str, type[EventBase]] = {
    "call_observed": CallObserved,
    "turn_observed": TurnObserved,
    "stage_observed": StageObserved,
    "aggregate_observed": AggregateObserved,
    "tool_observed": ToolObserved,
    "outcome_observed": OutcomeObserved,
    "grounding_observed": GroundingObserved,
    "evidence_observed": EvidenceObserved,
    "interruption_observed": InterruptionObserved,
    "snapshot_boundary": SnapshotBoundaryObserved,
    "fact_retracted": FactRetracted,
    "call_finalized": CallFinalized,
}


def parse_normalized_event(data: dict[str, Any]) -> NormalizedEvent:
    kind = str(data.get("type") or "")
    cls = _EVENT_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"unknown normalized event type {kind!r}")
    return cls.model_validate(data)  # type: ignore[return-value]
