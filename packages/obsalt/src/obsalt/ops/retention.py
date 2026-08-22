"""Finite retention defaults. Raw blobs are unredacted and have a shorter horizon (§12.3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from obsalt.domain.enums import EnvelopeState
from obsalt.util import utcnow


def replay_horizon(*, raw_retention_days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    cutoff = now - timedelta(days=raw_retention_days)
    return {
        "raw_retention_days": raw_retention_days,
        "replayable_after": cutoff.isoformat(),
        "note": (
            "Replay is bounded by configured raw retention and upstream provider "
            "retention. Expired raw data cannot be re-decoded."
        ),
    }


def sweep_raw(
    state: Any,
    *,
    now: datetime | None = None,
    raw_retention_days: int | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    days = raw_retention_days if raw_retention_days is not None else int(state.settings.raw_retention_days)
    cutoff = now - timedelta(days=days)
    inbox = state.inbox
    objects = state.objects
    purged = 0
    inspected = 0
    list_envelopes = getattr(inbox, "list_envelopes", None)
    orgs = _orgs(state)
    envelopes = []
    if callable(list_envelopes):
        for org_id in orgs:
            envelopes.extend(list_envelopes(org_id))
    else:
        envelopes = list(getattr(inbox, "by_id", {}).values())
    for envelope in envelopes:
        inspected += 1
        received = getattr(envelope, "received_at", None)
        if received is None or received > cutoff:
            continue
        key = getattr(envelope, "object_key", "")
        if key and objects is not None:
            try:
                objects.delete(key)
                purged += 1
            except Exception:
                continue
        if hasattr(envelope, "state"):
            envelope.state = EnvelopeState.FAILED
    return {
        **replay_horizon(raw_retention_days=days, now=now),
        "inspected": inspected,
        "purged_blobs": purged,
    }


def _orgs(state: Any) -> list[str]:
    seen: set[str] = set()
    for org, _scopes in getattr(state, "keys", {}).values():
        seen.add(org)
    lister = getattr(state.pointers, "list_org", None)
    if callable(lister) and seen:
        return sorted(seen)
    return sorted(seen) or ["local"]
