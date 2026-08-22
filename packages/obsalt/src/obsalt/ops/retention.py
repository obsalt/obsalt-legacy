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


def sweep(
    state: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply finite retention: raw, transcripts, then aggregates (§12.3)."""

    raw = sweep_raw(state, now=now)
    transcripts = sweep_transcripts(state, now=now)
    aggregates = sweep_aggregates(state, now=now)
    return {**raw, "transcripts": transcripts, "aggregates": aggregates}


def sweep_transcripts(
    state: Any,
    *,
    now: datetime | None = None,
    transcript_retention_days: int | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    days = (
        transcript_retention_days
        if transcript_retention_days is not None
        else int(getattr(state.settings, "transcript_retention_days", 90))
    )
    cutoff = now - timedelta(days=days)
    purged = 0
    sink = getattr(state, "sink", None)
    client = getattr(sink, "_client", None)
    if client is not None:
        try:
            client.command(
                "ALTER TABLE turns DELETE WHERE started_at < {cutoff:DateTime64}",
                parameters={"cutoff": cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff},
            )
            purged += 1
        except Exception:
            pass
    search = getattr(state, "search", None)
    docs = getattr(search, "_docs", None)
    if isinstance(docs, dict):
        for key, doc in list(docs.items()):
            started = getattr(doc, "started_at", None)
            if started is not None and started < cutoff:
                docs.pop(key, None)
                purged += 1
    return {"transcript_retention_days": days, "purged": purged}


def sweep_aggregates(
    state: Any,
    *,
    now: datetime | None = None,
    aggregate_retention_days: int | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    days = (
        aggregate_retention_days
        if aggregate_retention_days is not None
        else int(getattr(state.settings, "aggregate_retention_days", 400))
    )
    cutoff = now - timedelta(days=days)
    purged = 0
    rollups = getattr(state, "rollups", None)
    if rollups is not None:
        samples = getattr(rollups, "samples", None)
        if isinstance(samples, list):
            before = len(samples)
            rollups.samples = [row for row in samples if row.get("started_at", now) >= cutoff]
            purged += before - len(rollups.samples)
        client = getattr(rollups, "_client", None)
        if client is not None:
            try:
                client.command(
                    """
                    ALTER TABLE rollup_contributions
                    DELETE WHERE bucket < {cutoff:DateTime64}
                    """,
                    parameters={"cutoff": cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff},
                )
                purged += 1
            except Exception:
                pass
    return {"aggregate_retention_days": days, "purged": purged}


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
    orphans = sweep_orphan_blobs(objects, inbox, older_than_seconds=300)
    return {
        **replay_horizon(raw_retention_days=days, now=now),
        "inspected": inspected,
        "purged_blobs": purged,
        "orphan_blobs": orphans,
    }


def sweep_orphan_blobs(objects: Any, inbox: Any, *, older_than_seconds: int = 300) -> int:
    """Delete raw object keys that have no matching inbox row (§12.2)."""

    if objects is None or not hasattr(objects, "list_keys"):
        return 0
    known = {getattr(envelope, "object_key", "") for envelope in getattr(inbox, "by_id", {}).values()}
    if hasattr(inbox, "list_envelopes") and not known:
        return 0
    purged = 0
    for key in objects.list_keys("org/"):
        if "/raw/" not in key:
            continue
        if key in known:
            continue
        try:
            objects.delete(key)
            purged += 1
        except Exception:
            continue
    return purged


def _orgs(state: Any) -> list[str]:
    seen: set[str] = set()
    for org, _scopes in getattr(state, "keys", {}).values():
        seen.add(org)
    lister = getattr(state.pointers, "list_org", None)
    if callable(lister) and seen:
        return sorted(seen)
    return sorted(seen) or ["local"]
