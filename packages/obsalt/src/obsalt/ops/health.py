"""Day-one health signals from §12.2. Alert on these, not just a log line."""

from __future__ import annotations

from typing import Any

from obsalt.metrics import (
    deletion_backlog,
    dlq_depth,
    inbox_age_seconds,
    org_queue_pressure,
    orphan_blob_count,
)
from obsalt.util import utcnow


def collect_health(state: Any) -> dict[str, Any]:
    inbox = getattr(state, "inbox", None)
    objects = getattr(state, "objects", None)
    depth = 0
    oldest_age = 0.0
    dlq_n = 0
    if inbox is not None:
        depth_fn = getattr(inbox, "outbox_depth", None)
        depth = int(depth_fn()) if callable(depth_fn) else len(getattr(inbox, "outbox", []) or [])
        now = utcnow()
        envelopes = list(getattr(inbox, "by_id", {}).values())
        ages = []
        for envelope in envelopes:
            if getattr(envelope, "state", None) and envelope.state.value in {"assembled", "tombstoned"}:
                continue
            received = getattr(envelope, "received_at", None)
            if received is not None:
                ages.append(max(0.0, (now - received).total_seconds()))
        oldest_age = max(ages) if ages else 0.0
        dlq_n = len(getattr(inbox, "dlq", []) or [])
        list_dlq = getattr(inbox, "list_dlq", None)
        if callable(list_dlq):
            try:
                dlq_n = len(list_dlq())
            except Exception:
                pass
        by_org: dict[str, int] = {}
        for envelope in envelopes:
            if getattr(envelope, "state", None) and envelope.state.value in {"assembled", "tombstoned"}:
                continue
            org = getattr(envelope, "org_id", "") or ""
            by_org[org] = by_org.get(org, 0) + 1
        for org, count in by_org.items():
            org_queue_pressure.labels(org_id=org).set(count)
    inbox_age_seconds.set(oldest_age)
    dlq_depth.set(dlq_n)

    orphans = 0
    if objects is not None and inbox is not None and hasattr(objects, "list_keys"):
        known = {getattr(env, "object_key", "") for env in getattr(inbox, "by_id", {}).values()}
        for key in objects.list_keys("org/"):
            if "/raw/" in key and key not in known:
                orphans += 1
    orphan_blob_count.set(orphans)

    backlog = 0
    store = getattr(state, "deletion_store", None)
    if store is not None and hasattr(store, "backlog"):
        try:
            backlog = int(store.backlog())
        except Exception:
            backlog = 0
    else:
        completions = getattr(state, "deletion_completions", None) or []
        backlog = sum(1 for item in completions if item.get("status") != "completed")
    deletion_backlog.set(backlog)

    return {
        "inbox_age_seconds": oldest_age,
        "outbox_depth": depth,
        "dlq_depth": dlq_n,
        "orphan_blobs": orphans,
        "deletion_backlog": backlog,
    }
