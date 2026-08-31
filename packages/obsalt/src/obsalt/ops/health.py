"""Day-one health signals from §12.2. Alert on these, not just a log line."""

from __future__ import annotations

from typing import Any

from obsalt.domain.enums import EnvelopeState
from obsalt.metrics import (
    deletion_backlog,
    dlq_depth,
    inbox_age_seconds,
    org_queue_pressure,
    orphan_blob_count,
)
from obsalt.util import utcnow

_SETTLED = {EnvelopeState.ASSEMBLED, EnvelopeState.TOMBSTONED}


def collect_health(state: Any) -> dict[str, Any]:
    inbox = state.inbox
    objects = state.objects
    depth = int(inbox.outbox_depth())
    now = utcnow()
    envelopes = inbox.list_envelopes()
    ages = []
    by_org: dict[str, int] = {}
    for envelope in envelopes:
        if envelope.state in _SETTLED:
            continue
        if envelope.received_at is not None:
            ages.append(max(0.0, (now - envelope.received_at).total_seconds()))
        org = envelope.org_id or ""
        by_org[org] = by_org.get(org, 0) + 1
    oldest_age = max(ages) if ages else 0.0
    dlq_n = len(inbox.list_dlq())
    for org, count in by_org.items():
        org_queue_pressure.labels(org_id=org).set(count)
    inbox_age_seconds.set(oldest_age)
    dlq_depth.set(dlq_n)

    known = {env.object_key for env in envelopes if env.object_key}
    orphans = 0
    for key in objects.list_keys("org/"):
        if "/raw/" in key and key not in known:
            orphans += 1
    orphan_blob_count.set(orphans)

    backlog = int(state.deletion_store.backlog()) if state.deletion_store is not None else 0
    deletion_backlog.set(backlog)

    return {
        "inbox_age_seconds": oldest_age,
        "outbox_depth": depth,
        "dlq_depth": dlq_n,
        "orphan_blobs": orphans,
        "deletion_backlog": backlog,
    }
