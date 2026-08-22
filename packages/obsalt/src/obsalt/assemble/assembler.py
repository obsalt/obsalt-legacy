"""Fold normalized events into a complete immutable candidate CallRevision.

Merging independent facts is associative, commutative, and idempotent given the
source-revision rules in §5.3. Conflicts block automatic promotion.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from obsalt.assemble.facts import ASSEMBLER_VERSION, fact_id_for
from obsalt.domain.coverage import architecture_of, derive_coverage, derive_fidelity
from obsalt.domain.enums import (
    CallDirection,
    CallStatus,
    HangupParty,
    HangupReason,
    MeasurementPlacement,
    Provenance,
    ToolStatus,
)
from obsalt.domain.events import (
    AggregateObserved,
    CallFinalized,
    CallObserved,
    EvidenceObserved,
    FactRetracted,
    GroundingObserved,
    NormalizedEvent,
    OutcomeObserved,
    SnapshotBoundaryObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import (
    AggregateMeasurement,
    CallRevision,
    EvidenceRef,
    FidelityDeclaration,
    GroundingRef,
    Hangup,
    ProvenanceStamp,
    StageMeasurement,
    ToolInvocation,
    Turn,
)
from obsalt.domain.redact_shape import payload_shape
from obsalt.util import canonical_json, duration_ms, new_id, sha256_text, utcnow

ROOT_OWNED_FIELDS = frozenset({"agent_id", "ended_at", "status", "hangup", "cost"})


class FactRecord:
    __slots__ = ("fact_id", "content_hash", "event", "source_revision")

    def __init__(self, event: NormalizedEvent) -> None:
        self.fact_id = event.fact_id or fact_id_for(event)
        self.event = event
        self.content_hash = sha256_text(canonical_json(_content(event)))
        self.source_revision = event.source_revision


def _content(event: NormalizedEvent) -> dict[str, Any]:
    data = event.model_dump(exclude={"envelope_id", "processing_run_id", "envelope_sequence", "event_occurred_at"})
    return data


def _revision_cmp(left: FactRecord, right: FactRecord) -> int | None:
    a, b = left.source_revision, right.source_revision
    if a is None or b is None:
        return None
    if a.kind != b.kind or a.scope != b.scope:
        return None
    if a.kind == "timestamp":
        try:
            return (float(a.value) > float(b.value)) - (float(a.value) < float(b.value))
        except ValueError:
            return None
    if a.value.isdigit() and b.value.isdigit():
        return (int(a.value) > int(b.value)) - (int(a.value) < int(b.value))
    if a.value == b.value:
        return 0
    return None


class Assembler:
    def __init__(self, declaration: FidelityDeclaration, *, decoder_version: str, processing_run_id: str) -> None:
        self.declaration = declaration
        self.decoder_version = decoder_version
        self.processing_run_id = processing_run_id

    def assemble(
        self,
        org_id: str,
        call_id: str,
        source: str,
        events: Sequence[NormalizedEvent],
        *,
        base_revision: str | None = None,
        rooted: bool = True,
    ) -> CallRevision:
        accepted, conflicts, retracted = fold_facts(events)
        remaining = [record.event for record in accepted.values() if record.fact_id not in retracted]
        remaining = [event for event in remaining if not isinstance(event, FactRetracted)]

        call_obs = _last_of(remaining, CallObserved)
        turns = [_turn(e) for e in remaining if isinstance(e, TurnObserved)]
        turns.sort(key=lambda t: t.index)
        tools = [_tool(e) for e in remaining if isinstance(e, ToolObserved)]
        _annotate_retries(tools)
        stages = [_stage(e) for e in remaining if isinstance(e, StageObserved)]
        aggregates = [_aggregate(e) for e in remaining if isinstance(e, AggregateObserved)]
        grounding = [_grounding(e) for e in remaining if isinstance(e, GroundingObserved)]
        evidence = [_evidence(e) for e in remaining if isinstance(e, EvidenceObserved)]
        outcome = _last_of(remaining, OutcomeObserved)
        finalized = any(isinstance(e, CallFinalized) for e in remaining)

        source_call_id = call_obs.source_call_id if call_obs else source
        hangup = None
        if outcome is not None:
            hangup = Hangup(
                reason=outcome.reason or HangupReason.UNKNOWN,
                party=outcome.party or HangupParty.UNKNOWN,
                provider_code=outcome.provider_code,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=(
                    outcome.provenance_by_field["provider_code"].source_path
                    if "provider_code" in outcome.provenance_by_field
                    else None
                ),
            )
            if turns:
                hangup.last_speaker = turns[-1].speaker

        started = call_obs.started_at if call_obs else None
        ended = call_obs.ended_at if call_obs else (outcome.ended_at if outcome else None)
        status = CallStatus.ENDED if finalized or hangup else CallStatus.ONGOING
        if not rooted:
            status = CallStatus.UNROOTED
        if hangup and hangup.reason.value.startswith("error_"):
            status = CallStatus.ERROR

        coverage = derive_coverage(remaining, self.declaration, self.decoder_version)
        fidelity = derive_fidelity(remaining)
        architecture = architecture_of(remaining, self.declaration)

        provenance: dict[str, ProvenanceStamp] = {}
        if call_obs:
            provenance.update(call_obs.provenance_by_field)

        return CallRevision(
            org_id=org_id,
            call_id=call_id,
            revision=new_id(),
            source=source,
            source_call_id=source_call_id,
            agent_id=(call_obs.agent_id if call_obs and call_obs.agent_id else "unknown"),
            agent_version=call_obs.agent_version if call_obs else None,
            direction=call_obs.direction if call_obs else CallDirection.UNKNOWN,
            from_number=call_obs.from_number if call_obs else None,
            to_number=call_obs.to_number if call_obs else None,
            started_at=started,
            ended_at=ended,
            duration_ms=duration_ms(started, ended) if started and ended else None,
            status=status,
            pipeline_architecture=architecture,
            timeline_fidelity=fidelity,
            cost=call_obs.cost if call_obs else (outcome.cost if outcome else None),
            hangup=hangup,
            turns=turns,
            stage_measurements=stages,
            aggregate_measurements=aggregates,
            tools=tools,
            grounding=grounding,
            evidence=evidence,
            coverage=coverage,
            provenance=provenance,
            decoder_version=self.decoder_version,
            assembler_version=ASSEMBLER_VERSION,
            processing_run_id=self.processing_run_id,
            conflicts=conflicts,
            rooted=rooted,
            created_at=utcnow(),
        )


def fold_facts(events: Iterable[NormalizedEvent]) -> tuple[dict[str, FactRecord], list[str], set[str]]:
    accepted: dict[str, FactRecord] = {}
    conflicts: list[str] = []
    retracted: set[str] = set()
    snapshot_domains: list[str] = []
    present_domains: set[str] = set()

    for event in events:
        if isinstance(event, SnapshotBoundaryObserved):
            snapshot_domains = list(event.authoritative_domains)
            continue
        if isinstance(event, FactRetracted) and event.retracted_fact_id:
            retracted.add(event.retracted_fact_id)
            continue
        record = FactRecord(event)
        present_domains.add(event.type)
        existing = accepted.get(record.fact_id)
        if existing is None:
            accepted[record.fact_id] = record
            continue
        if existing.content_hash == record.content_hash:
            continue
        cmp = _revision_cmp(record, existing)
        if cmp is None:
            conflicts.append(record.fact_id)
            continue
        if cmp > 0:
            accepted[record.fact_id] = record
        # cmp < 0: keep existing; cmp == 0 already handled by hash

    if snapshot_domains:
        for fact_id, record in list(accepted.items()):
            if record.event.type in snapshot_domains and record.event.type not in present_domains:
                retracted.add(fact_id)

    return accepted, conflicts, retracted


def _last_of(events: Sequence[NormalizedEvent], typ: type):
    found = None
    for event in events:
        if isinstance(event, typ):
            found = event
    return found


def _turn(event: TurnObserved) -> Turn:
    return Turn(
        index=event.turn_index,
        speaker=event.speaker,
        text=event.text,
        started_at=event.started_at,
        ended_at=event.ended_at,
        interrupted=event.interrupted,
        confidence=event.confidence,
        provenance_by_field=event.provenance_by_field,
    )


def _tool(event: ToolObserved) -> ToolInvocation:
    duration = duration_ms(event.started_at, event.ended_at)
    return ToolInvocation(
        id=event.tool_id,
        name=event.name,
        turn_index=event.turn_index,
        started_at=event.started_at,
        ended_at=event.ended_at,
        duration_ms=duration,
        status=event.status,
        payload_shape=payload_shape(event.args) if event.args is not None else None,
        argument_hash=sha256_text(canonical_json(event.args)) if event.args is not None else None,
        error=event.error,
        provenance_by_field=event.provenance_by_field,
    )


def _annotate_retries(tools: list[ToolInvocation]) -> None:
    last_fail: dict[str, ToolInvocation] = {}
    for tool in tools:
        key = f"{tool.name}:{tool.argument_hash}"
        prev = last_fail.get(key)
        if prev is not None and prev.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
            tool.retry_count = prev.retry_count + 1
        if tool.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
            last_fail[key] = tool
        else:
            last_fail.pop(key, None)


def _stage(event: StageObserved) -> StageMeasurement:
    placement = event.placement
    if placement is MeasurementPlacement.INTERVAL and not (event.started_at and event.ended_at):
        placement = MeasurementPlacement.UNPLACED
    return StageMeasurement(
        fact_id=event.fact_id or fact_id_for(event),
        stage=event.stage,
        metric=event.metric,
        value_ms=event.value_ms,
        turn_index=event.turn_index,
        placement=placement,
        started_at=event.started_at,
        ended_at=event.ended_at,
        resolution_ms=event.resolution_ms,
        provenance=event.provenance,
        source_path=event.source_path,
        derivation=event.derivation,
    )


def _aggregate(event: AggregateObserved) -> AggregateMeasurement:
    return AggregateMeasurement(
        fact_id=event.fact_id or fact_id_for(event),
        stage=event.stage,
        metric=event.metric,
        statistic=event.statistic,
        value_ms=event.value_ms,
        population=event.population,
        window=event.window,
        provenance=event.provenance,
        source_path=event.source_path,
    )


def _grounding(event: GroundingObserved) -> GroundingRef:
    return GroundingRef(
        kind=event.kind,
        content_ref=sha256_text(event.content),
        content=event.content,
        provenance=event.provenance,
        source_path=event.source_path,
    )


def _evidence(event: EvidenceObserved) -> EvidenceRef:
    return EvidenceRef(
        kind=event.kind,
        uri=event.uri,
        metadata=event.metadata,
        provenance=event.provenance,
        source_path=event.source_path,
    )
