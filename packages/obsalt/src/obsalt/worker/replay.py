"""Replay re-decodes retained raw envelopes and promotes a new revision."""

from __future__ import annotations

from typing import Any

from obsalt.domain.enums import EnvelopeState
from obsalt.plugin.types import RawEnvelope
from obsalt.worker.drain import drain_once


def replay_envelopes(
    state: Any,
    *,
    org_id: str,
    provider: str | None = None,
    source_call_id: str | None = None,
    limit: int = 256,
) -> int:
    """Re-queue matching envelopes and drain. Tombstoned envelopes stay dead."""

    lister = getattr(state.inbox, "list_envelopes", None)
    requeue = getattr(state.inbox, "requeue", None)
    if not callable(lister) or not callable(requeue):
        return drain_once(state, limit=limit)
    queued = 0
    for envelope in lister(org_id):
        if not _matches(envelope, provider=provider, source_call_id=source_call_id):
            continue
        if envelope.state is EnvelopeState.TOMBSTONED:
            continue
        requeue(envelope.envelope_id)
        queued += 1
        if queued >= limit:
            break
    drain_once(state, limit=max(limit, queued or 1))
    return queued


def _matches(
    envelope: RawEnvelope,
    *,
    provider: str | None,
    source_call_id: str | None,
) -> bool:
    if provider and envelope.provider != provider:
        return False
    if source_call_id and envelope.source_call_id != source_call_id:
        return False
    return True
