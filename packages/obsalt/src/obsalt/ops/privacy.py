"""Deletion cannot be undone. Tombstones are checked by receive, replay, backfill (§12.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from obsalt.domain.models import CallRevision
from obsalt.plugin.types import TombstoneHints
from obsalt.privacy.caller import DEFAULT_PEPPER, caller_token
from obsalt.query import active_calls, in_range
from obsalt.runtime import bump_generation


def apply_deletion(
    state: Any,
    *,
    org_id: str,
    call_id: str | None = None,
    source_call_id: str | None = None,
    caller: str | None = None,
    caller_token_value: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    token = caller_token_value
    if caller and not token:
        token = caller_token(org_id, caller, DEFAULT_PEPPER)

    owned = list(_all_revisions(state, org_id))
    to_delete: set[str] = set()
    for rev in owned:
        if call_id and rev.call_id == call_id:
            to_delete.add(rev.call_id)
        if source_call_id and rev.source_call_id == source_call_id:
            to_delete.add(rev.call_id)
        if token and getattr(rev, "caller_token", None) == token:
            to_delete.add(rev.call_id)
        if start is not None and end is not None and in_range(rev, start, end):
            to_delete.add(rev.call_id)

    # Tombstones are per-org. Never treat another tenant's call_id as a source id (T10).
    resolved_source = None
    if source_call_id and source_call_id != call_id:
        resolved_source = source_call_id
    elif call_id and call_id in to_delete:
        for rev in owned:
            if rev.call_id == call_id and rev.source_call_id:
                resolved_source = rev.source_call_id
                break
    elif source_call_id and any(rev.source_call_id == source_call_id for rev in owned):
        resolved_source = source_call_id

    hints = TombstoneHints(
        source_call_id=resolved_source,
        caller_token=token,
        range_start=start,
        range_end=end,
    )
    if hints.source_call_id or hints.caller_token or (hints.range_start and hints.range_end):
        state.inbox.tombstone(org_id, hints)

    search = state.search
    rollups = state.rollups
    objects = state.objects
    source_ids = {
        rev.source_call_id
        for rev in _all_revisions(state, org_id)
        if rev.call_id in to_delete and rev.source_call_id
    }
    if resolved_source:
        source_ids.add(resolved_source)
    for source_id in source_ids:
        state.inbox.tombstone(org_id, TombstoneHints(source_call_id=source_id, caller_token=token))
    for cid in to_delete:
        state.sink.delete_call(org_id, cid)
        state.pointers.delete(org_id, cid)
        if search is not None:
            search.delete_for_call(org_id, cid)
        if rollups is not None:
            rollups.delete_call(org_id, cid)
        _purge_evidence(objects, org_id, cid)
        _purge_queues(state, org_id, cid, source_ids)

    # In-flight inbox/outbox/forward rows can exist before a revision is promoted.
    # Purge is org-scoped; do not add an unowned id to deleted_calls (T10).
    pending_ids = {item for item in (call_id, source_call_id) if item} - to_delete
    for cid in pending_ids:
        _purge_queues(state, org_id, cid, source_ids)

    bump_generation(state)
    from obsalt.ops.backup import expire_backups

    expire_backups(state)
    completed = _complete_deletion(
        state,
        org_id,
        call_ids=to_delete,
        source_call_id=resolved_source,
        caller_token=token,
    )
    return {
        "status": "accepted",
        "undoable": False,
        "deleted_calls": sorted(to_delete),
        "caller_token_retained": bool(token),
        "completed_at": completed.get("completed_at"),
        "note": "External warehouse copies cannot be revoked.",
    }


def _complete_deletion(
    state: Any,
    org_id: str,
    *,
    call_ids: set[str],
    source_call_id: str | None,
    caller_token: str | None,
) -> dict[str, Any]:
    from obsalt.util import utcnow

    item = {
        "org_id": org_id,
        "call_ids": sorted(call_ids),
        "source_call_id": source_call_id,
        "caller_token": caller_token,
        "completed_at": utcnow().isoformat(),
        "status": "completed",
    }
    if state.deletion_store is not None:
        state.deletion_store.complete(
            org_id,
            call_ids=call_ids,
            source_call_id=source_call_id,
            caller_token=caller_token,
        )
    completions = getattr(state, "deletion_completions", None)
    if completions is None:
        state.deletion_completions = []
        completions = state.deletion_completions
    completions.append(item)
    return item


def _all_revisions(state: Any, org_id: str) -> list[CallRevision]:
    items = list(active_calls(state, org_id))
    seen = {(rev.call_id, rev.revision) for rev in items}
    for call_id, _revision in state.pointers.list_org(org_id):
        for rev in state.sink.list_for_call(org_id, call_id):
            key = (rev.call_id, rev.revision)
            if key in seen or rev.org_id != org_id:
                continue
            items.append(rev)
            seen.add(key)
    return items


def _purge_queues(state: Any, org_id: str, call_id: str, source_ids: set[str]) -> None:
    from obsalt.domain.enums import EnvelopeState

    inbox = state.inbox
    for envelope in inbox.list_envelopes(org_id):
        source = envelope.source_call_id
        if source in source_ids or source == call_id or call_id in (envelope.object_key or ""):
            inbox.drop_outbox(envelope.envelope_id)
            envelope.state = EnvelopeState.TOMBSTONED
    inbox.purge_dlq(org_id, source_call_ids=source_ids | {call_id})

    if state.webhook_store is not None:
        state.webhook_store.purge_for_call(org_id, call_id)
    else:
        state.webhook_outbox = [
            item
            for item in state.webhook_outbox
            if item.get("org_id") != org_id or item.get("call_id") not in {call_id, *source_ids}
        ]

    queue = getattr(state, "forward_queue", None)
    pending = getattr(queue, "pending", None) if queue is not None else None
    if queue is not None and isinstance(pending, list):
        queue.pending = [
            job
            for job in pending
            if getattr(job, "org_id", None) != org_id
            or call_id not in (getattr(job, "object_key", "") or "")
        ]
    purge_fwd = getattr(queue, "purge_org", None) if queue is not None else None
    if callable(purge_fwd):
        purge_fwd(org_id, call_id=call_id)


def _purge_evidence(objects: Any, org_id: str, call_id: str) -> None:
    prefix = f"org/{org_id}/"
    for key in objects.list_keys(prefix):
        if call_id in key:
            try:
                objects.delete(key)
            except Exception:
                continue
