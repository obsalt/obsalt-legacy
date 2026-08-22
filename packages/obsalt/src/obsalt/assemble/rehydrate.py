"""Rebuild NormalizedEvent[] from an immutable CallRevision.

Used to fold late envelopes onto the previously promoted revision. The
reconstructed stream never includes SnapshotBoundaryObserved — a new snapshot
in the incoming envelope is what retracts authoritative domains.
"""

from __future__ import annotations

from obsalt.assemble.facts import fact_id_for
from obsalt.domain.enums import CallStatus
from obsalt.domain.events import (
    AggregateObserved,
    CallFinalized,
    CallObserved,
    EvidenceObserved,
    GroundingObserved,
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import CallRevision


def events_from_revision(revision: CallRevision) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    call = CallObserved(
        source_call_id=revision.source_call_id,
        agent_id=revision.agent_id,
        agent_version=revision.agent_version,
        direction=revision.direction,
        from_number=revision.from_number,
        to_number=revision.to_number,
        started_at=revision.started_at,
        ended_at=revision.ended_at,
        architecture=revision.pipeline_architecture,
        cost=revision.cost,
        status=revision.status.value,
        provenance_by_field=dict(revision.provenance),
        org_id=revision.org_id,
        call_key=revision.call_id,
        decoder_version=revision.decoder_version,
        processing_run_id=revision.processing_run_id,
    )
    call.fact_id = fact_id_for(call)
    events.append(call)

    for turn in revision.turns:
        turn_event = TurnObserved(
            turn_index=turn.index,
            speaker=turn.speaker,
            text=turn.text,
            started_at=turn.started_at,
            ended_at=turn.ended_at,
            confidence=turn.confidence,
            interrupted=turn.interrupted,
            provenance_by_field=dict(turn.provenance_by_field),
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        turn_event.fact_id = fact_id_for(turn_event)
        events.append(turn_event)

    for measurement in revision.stage_measurements:
        stage_event = StageObserved(
            stage=measurement.stage,
            metric=measurement.metric,
            value_ms=measurement.value_ms,
            turn_index=measurement.turn_index,
            placement=measurement.placement,
            started_at=measurement.started_at,
            ended_at=measurement.ended_at,
            resolution_ms=measurement.resolution_ms,
            provenance=measurement.provenance,
            source_path=measurement.source_path,
            derivation=measurement.derivation,
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        stage_event.fact_id = measurement.fact_id or fact_id_for(stage_event)
        events.append(stage_event)

    for measurement in revision.aggregate_measurements:
        aggregate_event = AggregateObserved(
            stage=measurement.stage,
            metric=measurement.metric,
            statistic=measurement.statistic,
            value_ms=measurement.value_ms,
            population=measurement.population,
            window=measurement.window,
            provenance=measurement.provenance,
            source_path=measurement.source_path,
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        aggregate_event.fact_id = measurement.fact_id or fact_id_for(aggregate_event)
        events.append(aggregate_event)

    for tool in revision.tools:
        tool_event = ToolObserved(
            tool_id=tool.id,
            name=tool.name,
            turn_index=tool.turn_index,
            started_at=tool.started_at,
            ended_at=tool.ended_at,
            status=tool.status,
            args=tool.args,
            result=tool.result,
            error=tool.error,
            provenance_by_field=dict(tool.provenance_by_field),
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        tool_event.fact_id = fact_id_for(tool_event)
        events.append(tool_event)

    if revision.hangup is not None:
        from obsalt.domain.models import ProvenanceStamp

        outcome_event = OutcomeObserved(
            provider_code=revision.hangup.provider_code,
            reason=revision.hangup.reason,
            party=revision.hangup.party,
            ended_at=revision.ended_at,
            cost=revision.cost,
            provenance_by_field={
                "provider_code": ProvenanceStamp(
                    provenance=revision.hangup.provenance,
                    source_path=revision.hangup.source_path,
                )
            },
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        outcome_event.fact_id = fact_id_for(outcome_event)
        events.append(outcome_event)

    for item in revision.grounding:
        grounding_event = GroundingObserved(
            kind=item.kind,
            content=item.content,
            provenance=item.provenance,
            source_path=item.source_path,
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        grounding_event.fact_id = fact_id_for(grounding_event)
        events.append(grounding_event)

    for item in revision.evidence:
        evidence_event = EvidenceObserved(
            kind=item.kind,
            uri=item.uri,
            metadata=dict(item.metadata),
            provenance=item.provenance,
            source_path=item.source_path,
            org_id=revision.org_id,
            call_key=revision.call_id,
            decoder_version=revision.decoder_version,
        )
        evidence_event.fact_id = fact_id_for(evidence_event)
        events.append(evidence_event)

    if revision.status in {CallStatus.ENDED, CallStatus.ERROR} or revision.hangup is not None:
        finalized = CallFinalized(
            reason="provider", org_id=revision.org_id, call_key=revision.call_id
        )
        finalized.fact_id = fact_id_for(finalized)
        events.append(finalized)

    return events
