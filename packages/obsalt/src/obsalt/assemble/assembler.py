"""Fold normalized events into a complete immutable candidate CallRevision.

Merging independent facts is associative, commutative, and idempotent given the
source-revision rules in §5.3. Conflicts block automatic promotion.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from obsalt.analysis.hangup import customer_loss_score
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
    InterruptionObserved,
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

# Processing metadata is stamped by core and is not fact content (§5.3).
BOOKKEEPING_FIELDS = frozenset(
    {
        "envelope_id",
        "processing_run_id",
        "envelope_sequence",
        "event_occurred_at",
        "decoder_version",
        "fact_id",
        "org_id",
        "call_key",
    }
)


class FactRecord:
    __slots__ = ("fact_id", "content_hash", "event", "source_revision")

    def __init__(self, event: NormalizedEvent) -> None:
        self.fact_id = event.fact_id or fact_id_for(event)
        self.event = event
        self.content_hash = sha256_text(canonical_json(_content(event)))
        self.source_revision = event.source_revision


def _content(event: NormalizedEvent) -> dict[str, Any]:
    return event.model_dump(exclude=set(BOOKKEEPING_FIELDS))


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
    def __init__(
        self, declaration: FidelityDeclaration, *, decoder_version: str, processing_run_id: str
    ) -> None:
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
        remaining = [
            record.event for record in accepted.values() if record.fact_id not in retracted
        ]
        remaining = [event for event in remaining if not isinstance(event, FactRetracted)]

        call_obs = _last_of(remaining, CallObserved)
        turns = [_turn(e) for e in remaining if isinstance(e, TurnObserved)]
        turns.sort(
            key=lambda t: (
                t.started_at or datetime.min.replace(tzinfo=UTC),
                t.index,
                t.speaker.value,
            )
        )
        _apply_interruptions(turns, remaining)
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

        started, ended, derived_provenance = _lifecycle_bounds(call_obs, outcome, turns)
        status = CallStatus.ENDED if finalized or hangup else CallStatus.ONGOING
        agent_id = call_obs.agent_id if call_obs and call_obs.agent_id else "unknown"
        cost = call_obs.cost if call_obs else (outcome.cost if outcome else None)
        if not rooted:
            # Non-root spans may not win root-owned fields (§6.3).
            status = CallStatus.UNROOTED
            ended = None
            hangup = None
            cost = None
            agent_id = "unknown"
        if hangup and hangup.reason.value.startswith("error_"):
            status = CallStatus.ERROR

        coverage = derive_coverage(remaining, self.declaration, self.decoder_version)
        fidelity = derive_fidelity(remaining)
        architecture = architecture_of(remaining, self.declaration)

        provenance: dict[str, ProvenanceStamp] = {}
        if call_obs:
            provenance.update(call_obs.provenance_by_field)
        provenance.update(derived_provenance)

        revision = CallRevision(
            org_id=org_id,
            call_id=call_id,
            revision=new_id(),
            source=source,
            source_call_id=source_call_id,
            agent_id=agent_id,
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
            cost=cost,
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
            accepted_fact_ids=sorted(fid for fid in accepted if fid not in retracted),
            created_at=utcnow(),
        )
        if revision.hangup is not None:
            score, reasons = customer_loss_score(revision)
            revision.hangup.loss_score = score
            revision.hangup.loss_reasons = reasons
        return revision


def fold_facts(
    events: Iterable[NormalizedEvent],
) -> tuple[dict[str, FactRecord], list[str], set[str]]:
    accepted: dict[str, FactRecord] = {}
    conflicts: list[str] = []
    retracted: set[str] = set()

    for event in events:
        if isinstance(event, SnapshotBoundaryObserved):
            # A snapshot is authoritative for the declared domains: drop prior
            # facts in those domains so omitted facts retract. Subsequent events
            # in this stream re-add what the snapshot still contains.
            for fact_id, record in list(accepted.items()):
                if record.event.type in event.authoritative_domains:
                    accepted.pop(fact_id, None)
            continue
        if isinstance(event, FactRetracted) and event.retracted_fact_id:
            retracted.add(event.retracted_fact_id)
            accepted.pop(event.retracted_fact_id, None)
            continue
        record = FactRecord(event)
        existing = accepted.get(record.fact_id)
        if existing is None:
            accepted[record.fact_id] = record
            continue
        merged = _merge_or_choose(existing, record)
        if merged is None:
            # Differing content without a comparable revision is a conflict.
            # Keep a deterministic winner so fold is commutative; promotion still blocks.
            if record.fact_id not in conflicts:
                conflicts.append(record.fact_id)
            if record.content_hash < existing.content_hash:
                accepted[record.fact_id] = record
            continue
        accepted[record.fact_id] = merged

    return accepted, conflicts, retracted


def _is_unset(value: object) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, str) and value in {"unknown", "pending"}:
        return True
    return False


def _merge_or_choose(existing: FactRecord, incoming: FactRecord) -> FactRecord | None:
    """Identical content dedupes; ordered revision wins; additive field fills merge.

    Differing non-empty values without a comparable source revision are a conflict.
    CallObserved / ToolObserved updates (started then ended, invocation then result)
    are additive fills, not arbitrary overwrites.
    """
    if existing.content_hash == incoming.content_hash:
        return existing
    cmp = _revision_cmp(incoming, existing)
    if cmp is not None:
        return incoming if cmp > 0 else existing
    if not isinstance(
        existing.event,
        (CallObserved, ToolObserved, TurnObserved, OutcomeObserved, GroundingObserved),
    ):
        return None
    if type(existing.event) is not type(incoming.event):
        return None
    left = existing.event.model_dump()
    right = incoming.event.model_dump()
    merged: dict[str, object] = {}
    for key in left:
        a, b = left.get(key), right.get(key)
        if key in BOOKKEEPING_FIELDS:
            merged[key] = b if not _is_unset(b) else a
            continue
        if a == b:
            merged[key] = a
        elif isinstance(a, dict) and isinstance(b, dict):
            combined = dict(a)
            combined.update(b)
            merged[key] = combined
        elif _is_unset(a) and not _is_unset(b):
            merged[key] = b
        elif _is_unset(b) and not _is_unset(a):
            merged[key] = a
        else:
            return None
    event = existing.event.__class__.model_validate(merged)
    record = FactRecord(event)
    record.fact_id = existing.fact_id
    return record


TEvent = TypeVar("TEvent", bound=NormalizedEvent)


def _lifecycle_bounds(
    call_obs: CallObserved | None,
    outcome: OutcomeObserved | None,
    turns: Sequence[Turn],
) -> tuple[datetime | None, datetime | None, dict[str, ProvenanceStamp]]:
    """Call clocks come from CallObserved / OutcomeObserved, then earliest turn.

    A missing call-level start is not unknown when turns already carry
    provider timestamps. Deriving it keeps list, search, and rollups on
    the same clock as ``in_range``. Call end is never inferred from the
    last turn — that would invent an end for an ongoing call.
    """
    started = call_obs.started_at if call_obs is not None else None
    ended = call_obs.ended_at if call_obs is not None else None
    if ended is None and outcome is not None:
        ended = outcome.ended_at
    extra: dict[str, ProvenanceStamp] = {}
    if started is None:
        turn_starts = [turn.started_at for turn in turns if turn.started_at is not None]
        if turn_starts:
            started = min(turn_starts)
            extra["started_at"] = ProvenanceStamp(
                provenance=Provenance.OBSALT_DERIVED,
                derivation="min(turn.started_at)",
            )
    return started, ended, extra


def _last_of(events: Sequence[NormalizedEvent], typ: type[TEvent]) -> TEvent | None:
    found: TEvent | None = None
    for event in events:
        if isinstance(event, typ):
            found = event
    return found


def _turn(event: TurnObserved) -> Turn:
    return Turn(
        index=event.turn_index,
        speaker=event.speaker,
        text=event.text,
        text_ref=sha256_text(event.text) if event.text else None,
        started_at=event.started_at,
        ended_at=event.ended_at,
        interrupted=event.interrupted,
        confidence=event.confidence,
        provenance_by_field=event.provenance_by_field,
    )


def _apply_interruptions(turns: list[Turn], events: Sequence[NormalizedEvent]) -> None:
    for event in events:
        if not isinstance(event, InterruptionObserved) or event.turn_index is None:
            continue
        for turn in turns:
            if turn.index == event.turn_index:
                turn.interrupted = True


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
        result_ref=sha256_text(canonical_json(event.result)) if event.result is not None else None,
        args=event.args,
        result=event.result,
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
