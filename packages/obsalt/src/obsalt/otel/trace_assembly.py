"""Trace assembly — never wait for an unsolvable completion signal (§6.3).

On the first span of an unseen trace, create an assembly record. Fold facts.
When the root arrives, mark rooted. Finalize at
min(root_ended_at + grace, first_seen_at + max_call_duration). A still-rootless
trace at max_call_duration finalizes as unrooted without inventing name/outcome/end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from obsalt.domain.events import NormalizedEvent
from obsalt.otel.conventions import OBSALT_AS_ROOT
from obsalt.plugin.types import ReadableSpan
from obsalt.util import utcnow


def _from_nano(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1e9, tz=UTC)


def is_root_span(span: ReadableSpan) -> bool:
    attrs = span.attributes or {}
    if attrs.get(OBSALT_AS_ROOT) in {True, "true", "1"}:
        return True
    return not span.parent_span_id


@dataclass
class TraceRecord:
    org_id: str
    trace_id: str
    first_seen_at: datetime
    events: list[NormalizedEvent] = field(default_factory=list)
    rooted: bool = False
    root_ended_at: datetime | None = None
    call_id: str | None = None
    finalized: bool = False
    unrooted: bool = False
    mapper_name: str | None = None
    late_after_finalize: bool = False
    caller_token: str | None = None


class MemoryTraceAssembler:
    """In-memory assembly buffer. Production persists the same record in Postgres."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], TraceRecord] = {}

    def ingest(
        self,
        org_id: str,
        spans: list[ReadableSpan],
        events: list[NormalizedEvent],
        *,
        now: datetime | None = None,
        mapper_name: str | None = None,
    ) -> TraceRecord:
        now = now or utcnow()
        trace_id = spans[0].trace_id if spans else "unknown"
        key = (org_id, trace_id)
        record = self.records.get(key)
        if record is None:
            record = TraceRecord(org_id=org_id, trace_id=trace_id, first_seen_at=now)
            self.records[key] = record
        if mapper_name:
            record.mapper_name = mapper_name
        if record.finalized and (spans or events):
            from obsalt.metrics import late_spans_after_finalize_total

            late_spans_after_finalize_total.inc(max(len(spans), 1))
            record.finalized = False
            record.late_after_finalize = True
            if record.unrooted and any(is_root_span(span) for span in spans):
                record.unrooted = False
        from obsalt.domain.events import CallObserved
        from obsalt.privacy.caller import DEFAULT_PEPPER, caller_token
        from obsalt.redact.choke import redact_events

        for event in events:
            if isinstance(event, CallObserved) and event.from_number and event.from_number != "<phone>":
                record.caller_token = caller_token(org_id, event.from_number, DEFAULT_PEPPER)
        record.events.extend(redact_events(events).events)
        for span in spans:
            if is_root_span(span):
                record.rooted = True
                ended = _from_nano(span.end_unix_nano) if span.end_unix_nano else None
                if ended is not None and (record.root_ended_at is None or ended > record.root_ended_at):
                    record.root_ended_at = ended
        return record

    def ready(
        self,
        record: TraceRecord,
        *,
        now: datetime | None = None,
        grace_seconds: float = 0,
        max_call_duration_seconds: float = 4 * 60 * 60,
    ) -> bool:
        if record.finalized:
            return False
        if record.late_after_finalize:
            return True
        now = now or utcnow()
        max_deadline = record.first_seen_at + timedelta(seconds=max_call_duration_seconds)
        if record.rooted and record.root_ended_at is not None:
            grace_deadline = record.root_ended_at + timedelta(seconds=grace_seconds)
            return now >= min(grace_deadline, max_deadline)
        return now >= max_deadline

    def due(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float = 0,
        max_call_duration_seconds: float = 4 * 60 * 60,
    ) -> list[TraceRecord]:
        now = now or utcnow()
        return [
            record
            for record in self.records.values()
            if self.ready(
                record,
                now=now,
                grace_seconds=grace_seconds,
                max_call_duration_seconds=max_call_duration_seconds,
            )
        ]

    def mark_finalized(self, record: TraceRecord, *, unrooted: bool = False) -> None:
        record.finalized = True
        record.late_after_finalize = False
        record.unrooted = unrooted and not record.rooted


def unrooted_events(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    """Facts valid without root authority. Do not invent call name, outcome, or end time."""

    from obsalt.domain.events import CallObserved, OutcomeObserved

    out: list[NormalizedEvent] = []
    for event in events:
        if isinstance(event, OutcomeObserved):
            continue
        if isinstance(event, CallObserved):
            out.append(
                event.model_copy(
                    update={
                        "ended_at": None,
                        "status": None,
                        "agent_id": event.agent_id if event.agent_id and event.agent_id != "unknown" else None,
                    }
                )
            )
            continue
        out.append(event)
    return out
