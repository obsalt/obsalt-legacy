"""Postgres-backed trace assembly records (§6.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Json

from obsalt.domain.events import NormalizedEvent, parse_normalized_event
from obsalt.otel.trace_assembly import MemoryTraceAssembler, TraceRecord
from obsalt.plugin.types import ReadableSpan
from obsalt.util import utcnow


class PostgresTraceAssembler:
    """Same protocol as MemoryTraceAssembler; durable across restarts."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._mem = MemoryTraceAssembler()

    @property
    def records(self) -> dict[tuple[str, str], TraceRecord]:
        return self._mem.records

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
        existing = self._load(org_id, trace_id)
        if existing is not None:
            self._mem.records[(org_id, trace_id)] = existing
        record = self._mem.ingest(org_id, spans, events, now=now, mapper_name=mapper_name)
        self._save(record)
        return record

    def ready(
        self,
        record: TraceRecord,
        *,
        now: datetime | None = None,
        grace_seconds: float = 0,
        max_call_duration_seconds: float = 4 * 60 * 60,
    ) -> bool:
        return self._mem.ready(
            record,
            now=now,
            grace_seconds=grace_seconds,
            max_call_duration_seconds=max_call_duration_seconds,
        )

    def due(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float = 0,
        max_call_duration_seconds: float = 4 * 60 * 60,
    ) -> list[TraceRecord]:
        self._refresh()
        return self._mem.due(
            now=now,
            grace_seconds=grace_seconds,
            max_call_duration_seconds=max_call_duration_seconds,
        )

    def mark_finalized(self, record: TraceRecord, *, unrooted: bool = False) -> None:
        self._mem.mark_finalized(record, unrooted=unrooted)
        self._save(record)

    def _refresh(self) -> None:
        rows = self._conn.execute(
            """
            SELECT org_id, trace_id, first_seen_at, rooted, root_ended_at, call_id,
                   events, finalized_at, mapper_name, unrooted, late_after_finalize
            FROM trace_assemblies
            WHERE finalized_at IS NULL OR late_after_finalize = TRUE
            """
        ).fetchall()
        for row in rows:
            record = _record_from_row(row)
            self._mem.records[(record.org_id, record.trace_id)] = record

    def _load(self, org_id: str, trace_id: str) -> TraceRecord | None:
        cached = self._mem.records.get((org_id, trace_id))
        if cached is not None:
            return cached
        row = self._conn.execute(
            """
            SELECT org_id, trace_id, first_seen_at, rooted, root_ended_at, call_id,
                   events, finalized_at, mapper_name, unrooted, late_after_finalize
            FROM trace_assemblies
            WHERE org_id = %s AND trace_id = %s
            """,
            (org_id, trace_id),
        ).fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        self._mem.records[(org_id, trace_id)] = record
        return record

    def _save(self, record: TraceRecord) -> None:
        payload = [event.model_dump(mode="json") for event in record.events]
        self._conn.execute(
            """
            INSERT INTO trace_assemblies (
                org_id, trace_id, first_seen_at, rooted, root_ended_at, call_id,
                events, finalized_at, mapper_name, unrooted, late_after_finalize
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (org_id, trace_id) DO UPDATE SET
                rooted = EXCLUDED.rooted,
                root_ended_at = EXCLUDED.root_ended_at,
                call_id = EXCLUDED.call_id,
                events = EXCLUDED.events,
                finalized_at = EXCLUDED.finalized_at,
                mapper_name = EXCLUDED.mapper_name,
                unrooted = EXCLUDED.unrooted,
                late_after_finalize = EXCLUDED.late_after_finalize
            """,
            (
                record.org_id,
                record.trace_id,
                record.first_seen_at,
                record.rooted,
                record.root_ended_at,
                record.call_id,
                Json(payload),
                utcnow() if record.finalized else None,
                record.mapper_name,
                record.unrooted,
                record.late_after_finalize,
            ),
        )


def _record_from_row(row: dict[str, Any]) -> TraceRecord:
    raw_events = row.get("events") or []
    events = [parse_normalized_event(item) for item in raw_events if isinstance(item, dict)]
    return TraceRecord(
        org_id=row["org_id"],
        trace_id=row["trace_id"],
        first_seen_at=row["first_seen_at"],
        events=events,
        rooted=bool(row.get("rooted")),
        root_ended_at=row.get("root_ended_at"),
        call_id=row.get("call_id"),
        finalized=row.get("finalized_at") is not None,
        unrooted=bool(row.get("unrooted")),
        mapper_name=row.get("mapper_name"),
        late_after_finalize=bool(row.get("late_after_finalize")),
    )
