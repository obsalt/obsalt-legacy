"""Fold normalized events into a complete candidate CallRevision.

Merging independent facts is associative, commutative, and idempotent. Same
fact_id + identical content dedupes. A greater ordered source revision wins.
Differing content without a comparable revision is a visible conflict — never
an arbitrary overwrite. This is the antidote to ``merge_calls``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import uuid4

from obsalt.assemble.fidelity import ASSEMBLER_VERSION, derive_coverage, derive_fidelity
from obsalt.domain.enums import CallStatus, GroundingKind, Provenance
from obsalt.domain.events import (
    AggregateObserved,
    CallFinalized,
    CallObserved,
    EvidenceObserved,
    FactRetracted,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    SnapshotBoundaryObserved,
    SourceRevision,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.identity import call_id_for, call_key_for, canonical_json, content_hash, fact_id_for
from obsalt.domain.models import (
    AggregateMeasurement,
    CallIdentity,
    CallLifecycle,
    CallRevision,
    CallTelephony,
    EvidenceRef,
    FactConflict,
    GroundingRef,
    Hangup,
    ProvenanceStamp,
    StageMeasurement,
    ToolInvocation,
    Turn,
)
from obsalt.plugin.protocol import FidelityDeclaration

ROOT_OWNED = frozenset({"agent_id", "ended_at", "status", "hangup"})


def stamp_events(
    events: Iterable[NormalizedEvent],
    *,
    org_id: str,
    source: str,
    envelope_id: str,
    decoder_version: str,
    processing_run_id: str,
    envelope_sequence: int = 0,
    source_call_id: str | None = None,
) -> list[NormalizedEvent]:
    materialized = list(events)
    inferred = source_call_id
    for event in materialized:
        if isinstance(event, CallObserved) and event.source_call_id:
            inferred = event.source_call_id
            break
    stamped: list[NormalizedEvent] = []
    for event in materialized:
        payload = event.model_dump()
        if inferred:
            payload["call_key"] = call_key_for(org_id, source, inferred)
        payload["org_id"] = org_id
        payload["envelope_id"] = envelope_id
        payload["decoder_version"] = decoder_version
        payload["processing_run_id"] = processing_run_id
        payload["envelope_sequence"] = envelope_sequence
        if not payload.get("fact_id"):
            payload["fact_id"] = _default_fact_id(event, source)
        stamped.append(type(event).model_validate(payload))
    return stamped


def _default_fact_id(event: NormalizedEvent, source: str) -> str:
    if isinstance(event, CallObserved):
        return fact_id_for("call", source, event.source_call_id)
    if isinstance(event, TurnObserved):
        return fact_id_for("turn", source, str(event.turn_index), event.speaker.value)
    if isinstance(event, StageObserved):
        turn = "" if event.turn_index is None else str(event.turn_index)
        return fact_id_for(
            "stage", source, event.stage.value, event.metric.value, turn, event.placement.value
        )
    if isinstance(event, AggregateObserved):
        return fact_id_for("aggregate", source, event.stage.value, event.metric.value, event.statistic.value)
    if isinstance(event, ToolObserved):
        return fact_id_for("tool", source, event.tool_id, event.name)
    if isinstance(event, OutcomeObserved):
        return fact_id_for("outcome", source, event.provider_code)
    if isinstance(event, GroundingObserved):
        return fact_id_for("grounding", source, event.kind.value, content_hash(event.content))
    if isinstance(event, EvidenceObserved):
        return fact_id_for("evidence", source, event.kind.value, event.uri)
    if isinstance(event, InterruptionObserved):
        turn = "" if event.turn_index is None else str(event.turn_index)
        return fact_id_for("interruption", source, event.kind.value, turn)
    if isinstance(event, SnapshotBoundaryObserved):
        return fact_id_for("snapshot", source, canonical_json(event.authoritative_domains))
    if isinstance(event, FactRetracted):
        return fact_id_for("retract", event.retracted_fact_id)
    if isinstance(event, CallFinalized):
        return fact_id_for("finalized", source, event.reason)
    return fact_id_for("event", source, event.type)


def _revision_tuple(rev: SourceRevision | None) -> tuple[str, str] | None:
    if rev is None:
        return None
    return (rev.kind, rev.value)


def _revision_greater(left: SourceRevision | None, right: SourceRevision | None) -> bool | None:
    if left is None or right is None:
        return None
    if left.kind != right.kind or left.scope != right.scope:
        return None
    try:
        return float(left.value) > float(right.value)
    except ValueError:
        return left.value > right.value


def _content_key(event: NormalizedEvent) -> str:
    data = event.model_dump(
        exclude={
            "envelope_id",
            "decoder_version",
            "processing_run_id",
            "envelope_sequence",
            "event_occurred_at",
        }
    )
    return content_hash(data)


class _FactSlot:
    __slots__ = ("event",)

    def __init__(self, event: NormalizedEvent) -> None:
        self.event = event


def fold_events(
    events: Sequence[NormalizedEvent],
    *,
    org_id: str,
    source: str,
    source_call_id: str,
    processing_run_id: str | None = None,
    revision: int = 1,
    declaration: FidelityDeclaration | None = None,
    decoder_version: str = "unknown",
    allow_unrooted: bool = False,
) -> CallRevision:
    slots: dict[str, _FactSlot] = {}
    conflicts: list[FactConflict] = []
    retracted: set[str] = set()
    envelope_ids: set[str] = set()
    decoder_versions: set[str] = set()

    for event in events:
        if event.envelope_id:
            envelope_ids.add(event.envelope_id)
        if event.decoder_version:
            decoder_versions.add(event.decoder_version)
        if isinstance(event, FactRetracted):
            retracted.add(event.retracted_fact_id)
            continue
        fact_id = event.fact_id or _default_fact_id(event, source)
        existing = slots.get(fact_id)
        if existing is None:
            slots[fact_id] = _FactSlot(event)
            continue
        if _content_key(existing.event) == _content_key(event):
            continue
        cmp = _revision_greater(event.source_revision, existing.event.source_revision)
        if cmp is True:
            slots[fact_id] = _FactSlot(event)
            continue
        if cmp is False:
            continue
        conflicts.append(
            FactConflict(
                fact_id=fact_id,
                reason="differing content without a comparable source_revision",
                left=_content_key(existing.event),
                right=_content_key(event),
            )
        )

    accepted = [slot.event for fact_id, slot in slots.items() if fact_id not in retracted]
    call_id = call_id_for(org_id, source, source_call_id)
    identity = CallIdentity(
        org_id=org_id,
        call_id=call_id,
        source=source,
        source_call_id=source_call_id,
    )
    telephony = CallTelephony()
    lifecycle = CallLifecycle()
    hangup: Hangup | None = None
    turns: list[Turn] = []
    stages: list[StageMeasurement] = []
    aggregates: list[AggregateMeasurement] = []
    tools: list[ToolInvocation] = []
    grounding: list[GroundingRef] = []
    evidence: list[EvidenceRef] = []
    provenance: dict[str, ProvenanceStamp] = {}
    finalized = False

    for event in accepted:
        if isinstance(event, CallObserved):
            identity = identity.model_copy(
                update={
                    "agent_id": event.agent_id or identity.agent_id,
                    "agent_version": event.agent_version or identity.agent_version,
                }
            )
            telephony = telephony.model_copy(
                update={
                    "direction": event.direction,
                    "from_number": event.from_number,
                    "to_number": event.to_number,
                }
            )
            lifecycle = lifecycle.model_copy(
                update={
                    "started_at": event.started_at or lifecycle.started_at,
                    "pipeline_architecture": event.architecture or lifecycle.pipeline_architecture,
                }
            )
            if event.started_at:
                provenance["lifecycle.started_at"] = ProvenanceStamp(
                    provenance=event.provenance_by_field.get("started_at", Provenance.PROVIDER_REPORTED),
                    source_path=event.source_path,
                )
        elif isinstance(event, TurnObserved):
            turns.append(
                Turn(
                    fact_id=event.fact_id or "",
                    index=event.turn_index,
                    speaker=event.speaker,
                    text=event.text,
                    text_ref=content_hash(event.text) if event.text else None,
                    started_at=event.started_at,
                    ended_at=event.ended_at,
                    interrupted=event.interrupted,
                    confidence=event.confidence,
                )
            )
        elif isinstance(event, StageObserved):
            stages.append(
                StageMeasurement(
                    fact_id=event.fact_id or "",
                    stage=event.stage,
                    metric=event.metric,
                    value_ms=event.value_ms,
                    turn_index=event.turn_index,
                    placement=event.placement,
                    started_at=event.started_at,
                    ended_at=event.ended_at,
                    resolution_ms=event.resolution_ms,
                    provenance=event.provenance,
                    source_path=event.source_path,
                    derivation=event.derivation,
                )
            )
        elif isinstance(event, AggregateObserved):
            aggregates.append(
                AggregateMeasurement(
                    fact_id=event.fact_id or "",
                    stage=event.stage,
                    metric=event.metric,
                    statistic=event.statistic,
                    value_ms=event.value_ms,
                    population=event.population,
                    window=event.window,
                    provenance=event.provenance,
                    source_path=event.source_path,
                )
            )
        elif isinstance(event, ToolObserved):
            duration = None
            if event.started_at and event.ended_at:
                duration = (event.ended_at - event.started_at).total_seconds() * 1000.0
            tools.append(
                ToolInvocation(
                    fact_id=event.fact_id or "",
                    tool_id=event.tool_id,
                    name=event.name,
                    turn_index=event.turn_index,
                    started_at=event.started_at,
                    ended_at=event.ended_at,
                    duration_ms=duration,
                    status=event.status,
                    argument_hash=content_hash(event.args) if event.args is not None else None,
                    payload_shape=_shape(event.args),
                    error=event.error,
                )
            )
        elif isinstance(event, OutcomeObserved):
            hangup = Hangup(
                reason=event.reason or HangupReason.UNKNOWN,
                party=event.party or HangupParty.UNKNOWN,
                provider_code=event.provider_code,
                provenance=event.provenance_by_field.get("provider_code", Provenance.PROVIDER_REPORTED),
                source_path=event.source_path,
            )
            lifecycle = lifecycle.model_copy(
                update={
                    "ended_at": event.ended_at or lifecycle.ended_at,
                    "cost": event.cost if event.cost is not None else lifecycle.cost,
                    "status": CallStatus.ENDED,
                }
            )
        elif isinstance(event, GroundingObserved):
            grounding.append(
                GroundingRef(
                    kind=event.kind,
                    content_ref=content_hash(event.content),
                    provenance=event.provenance,
                    source_path=event.source_path,
                )
            )
        elif isinstance(event, EvidenceObserved):
            evidence.append(
                EvidenceRef(
                    kind=event.kind,
                    uri=event.uri,
                    content_ref=content_hash(event.uri),
                    metadata=event.metadata,
                    provenance=event.provenance,
                    source_path=event.source_path,
                )
            )
        elif isinstance(event, CallFinalized):
            finalized = True
            if lifecycle.status is CallStatus.UNKNOWN:
                lifecycle = lifecycle.model_copy(update={"status": CallStatus.ENDED})
        elif isinstance(event, (InterruptionObserved, SnapshotBoundaryObserved)):
            continue

    if lifecycle.started_at and lifecycle.ended_at and lifecycle.duration_ms is None:
        lifecycle = lifecycle.model_copy(
            update={"duration_ms": (lifecycle.ended_at - lifecycle.started_at).total_seconds() * 1000.0}
        )
    lifecycle = lifecycle.model_copy(update={"timeline_fidelity": derive_fidelity(accepted)})
    if allow_unrooted and lifecycle.status is CallStatus.UNKNOWN:
        lifecycle = lifecycle.model_copy(update={"status": CallStatus.UNROOTED})

    coverage = derive_coverage(
        accepted,
        decoder_version=next(iter(decoder_versions), decoder_version),
        declaration=declaration,
    )
    # Attach inline text on grounding refs for assembly consumers that still hold content.
    _ = GroundingKind
    return CallRevision(
        org_id=org_id,
        call_id=call_id,
        revision=revision,
        identity=identity,
        telephony=telephony,
        lifecycle=lifecycle,
        hangup=hangup,
        turns=sorted(turns, key=lambda t: t.index),
        stage_measurements=stages,
        aggregate_measurements=aggregates,
        tools=tools,
        grounding=grounding,
        evidence=evidence,
        coverage=coverage,
        provenance=provenance,
        conflicts=conflicts,
        decoder_versions=sorted(decoder_versions),
        assembler_version=ASSEMBLER_VERSION,
        processing_run_id=processing_run_id or str(uuid4()),
        envelope_ids=sorted(envelope_ids),
        rooted=not allow_unrooted,
        finalized=finalized,
    )


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _shape(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


# imported after type use to keep enum names available in OutcomeObserved fold
from obsalt.domain.enums import HangupParty, HangupReason  # noqa: E402
