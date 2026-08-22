"""Per-destination durable OTLP forward queue.

Ingest acknowledges first. The worker POSTs the original raw bytes to each
destination. One destination failing does not block the others. Payloads are
never reconstructed from the Call aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from obsalt.otel.forwarder import ForwardDestination, ForwardOutcome, forward_to_destination


@dataclass
class ForwardJob:
    job_id: str
    org_id: str
    object_key: str
    content_type: str
    raw: bytes
    destination_id: str | None = None
    attempts: int = 0


class MemoryForwardQueue:
    def __init__(self) -> None:
        self.pending: list[ForwardJob] = []
        self.delivered: list[ForwardJob] = []
        self.failed: list[tuple[ForwardJob, str]] = []

    def enqueue(self, job: ForwardJob) -> None:
        self.pending.append(job)

    def claim(self, limit: int = 32) -> list[ForwardJob]:
        claimed = self.pending[:limit]
        self.pending = self.pending[limit:]
        return claimed

    def requeue(self, job: ForwardJob) -> None:
        job.attempts += 1
        self.pending.append(job)

    def mark_delivered(self, job: ForwardJob) -> None:
        self.delivered.append(job)

    def mark_failed_job(self, job: ForwardJob, detail: str) -> None:
        self.failed.append((job, detail))


def enqueue_raw_batch(
    queue: MemoryForwardQueue | None,
    *,
    org_id: str,
    object_key: str,
    content_type: str,
    raw: bytes,
) -> None:
    if queue is None:
        return
    from obsalt.util import new_id

    queue.enqueue(
        ForwardJob(
            job_id=new_id(),
            org_id=org_id,
            object_key=object_key,
            content_type=content_type,
            raw=raw,
        )
    )


def drain_forward_queue(state: Any, *, limit: int = 32) -> int:
    queue = getattr(state, "forward_queue", None)
    if queue is None:
        return 0
    destinations = [d for d in getattr(state, "destinations", []) or []]
    claimed = queue.claim(limit)
    processed = 0
    for job in claimed:
        org_dests = [
            d for d in destinations if not d.get("org_id") or d.get("org_id") == job.org_id
        ]
        if not org_dests:
            _mark_delivered(queue, job)
            processed += 1
            continue
        retry = False
        for dest in org_dests:
            allow = dest.get("allow_http_localhost") in {True, "true"}
            destination = ForwardDestination(
                url=str(dest["url"]),
                emit_pii=dest.get("emit_pii") in {True, "true"},
                allow_http_localhost=allow,
            )
            result = forward_to_destination(destination, job.raw, content_type=job.content_type)
            if result.outcome is ForwardOutcome.RETRYABLE:
                retry = True
                _mark_failed(queue, job, result.detail or "retryable")
            elif result.outcome is ForwardOutcome.PERMANENT:
                _mark_failed(queue, job, result.detail or "permanent")
        if retry:
            queue.requeue(job)
        else:
            _mark_delivered(queue, job)
            processed += 1
    return processed


def _mark_delivered(queue: Any, job: ForwardJob) -> None:
    marker = getattr(queue, "mark_delivered", None)
    if callable(marker):
        marker(job)
        return
    queue.delivered.append(job)


def _mark_failed(queue: Any, job: ForwardJob, detail: str) -> None:
    marker = getattr(queue, "mark_failed_job", None)
    if callable(marker):
        marker(job, detail)
        return
    queue.failed.append((job, detail))
