"""Durable OTLP forward outbox. Destination failures never fail ingest."""

from __future__ import annotations

from typing import Any

from obsalt.otel.forward_queue import ForwardJob
from obsalt.util import new_id, utcnow


class PostgresForwardQueue:
    def __init__(self, conn: Any, objects: Any | None = None) -> None:
        self._conn = conn
        self._objects = objects
        self.delivered: list[ForwardJob] = []
        self.failed: list[tuple[ForwardJob, str]] = []

    def enqueue(self, job: ForwardJob) -> None:
        if self._objects is not None and job.raw:
            self._objects.put(job.object_key, job.raw, content_type=job.content_type)
        self._conn.execute(
            """
            INSERT INTO otlp_forward_outbox (
                id, org_id, object_key, content_type, destination_id, available_at, attempts
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                job.job_id or new_id(),
                job.org_id,
                job.object_key,
                job.content_type,
                job.destination_id,
                utcnow(),
                job.attempts,
            ),
        )

    def claim(self, limit: int = 32) -> list[ForwardJob]:
        rows = self._conn.execute(
            """
            UPDATE otlp_forward_outbox
            SET attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM otlp_forward_outbox
                WHERE delivered_at IS NULL AND available_at <= now()
                ORDER BY available_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            RETURNING id, org_id, object_key, content_type, destination_id, attempts
            """,
            (limit,),
        ).fetchall()
        jobs: list[ForwardJob] = []
        for row in rows:
            raw = b""
            if self._objects is not None:
                try:
                    raw = self._objects.get(row["object_key"])
                except Exception:
                    raw = b""
            jobs.append(
                ForwardJob(
                    job_id=row["id"],
                    org_id=row["org_id"],
                    object_key=row["object_key"],
                    content_type=row["content_type"],
                    raw=raw,
                    destination_id=row["destination_id"],
                    attempts=row["attempts"],
                )
            )
        return jobs

    def requeue(self, job: ForwardJob) -> None:
        job.attempts += 1
        self._conn.execute(
            """
            UPDATE otlp_forward_outbox
            SET attempts = %s, available_at = now() + interval '5 seconds' * LEAST(%s, 10)
            WHERE id = %s
            """,
            (job.attempts, job.attempts, job.job_id),
        )

    def mark_delivered(self, job: ForwardJob) -> None:
        self._conn.execute(
            "UPDATE otlp_forward_outbox SET delivered_at = now() WHERE id = %s",
            (job.job_id,),
        )
        self.delivered.append(job)

    def purge_org(self, org_id: str, *, call_id: str | None = None) -> int:
        if call_id:
            row = self._conn.execute(
                "DELETE FROM otlp_forward_outbox WHERE org_id = %s AND object_key LIKE %s",
                (org_id, f"%{call_id}%"),
            )
        else:
            row = self._conn.execute(
                "DELETE FROM otlp_forward_outbox WHERE org_id = %s",
                (org_id,),
            )
        return int(getattr(row, "rowcount", 0) or 0)

    def mark_failed_job(self, job: ForwardJob, detail: str) -> None:
        self._conn.execute(
            "UPDATE otlp_forward_outbox SET last_error = %s WHERE id = %s",
            (detail, job.job_id),
        )
        self.failed.append((job, detail))
