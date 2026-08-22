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

    hints = TombstoneHints(source_call_id=source_call_id or call_id, caller_token=token)
    if hints.source_call_id or hints.caller_token:
        state.inbox.tombstone(org_id, hints)

    to_delete: set[str] = set()
    if call_id:
        to_delete.add(call_id)

    for rev in _all_revisions(state, org_id):
        if call_id and rev.call_id == call_id:
            to_delete.add(rev.call_id)
        if source_call_id and rev.source_call_id == source_call_id:
            to_delete.add(rev.call_id)
        if token and getattr(rev, "caller_token", None) == token:
            to_delete.add(rev.call_id)
        if start is not None and end is not None and in_range(rev, start, end):
            to_delete.add(rev.call_id)

    deleter = getattr(state.pointers, "delete", None)
    search = getattr(state, "search", None)
    rollups = getattr(state, "rollups", None)
    objects = getattr(state, "objects", None)
    source_ids = {
        rev.source_call_id
        for rev in _all_revisions(state, org_id)
        if rev.call_id in to_delete and rev.source_call_id
    }
    for source_id in source_ids:
        state.inbox.tombstone(org_id, TombstoneHints(source_call_id=source_id, caller_token=token))
    for cid in to_delete:
        state.sink.delete_call(org_id, cid)
        if callable(deleter):
            deleter(org_id, cid)
        if search is not None and hasattr(search, "delete_for_call"):
            search.delete_for_call(org_id, cid)
        if rollups is not None and hasattr(rollups, "delete_call"):
            rollups.delete_call(org_id, cid)
        if objects is not None:
            _purge_evidence(objects, org_id, cid)

    bump_generation(state)
    completed = _complete_deletion(
        state,
        org_id,
        call_ids=to_delete,
        source_call_id=source_call_id or call_id,
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
    store = getattr(state, "deletion_store", None)
    if store is not None and hasattr(store, "complete"):
        store.complete(
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
    revisions = getattr(state.sink, "revisions", {})
    for rev in revisions.values():
        if rev.org_id == org_id and rev not in items:
            items.append(rev)
    return items


def _purge_evidence(objects: Any, org_id: str, call_id: str) -> None:
    list_keys = getattr(objects, "list_keys", None)
    if not callable(list_keys):
        return
    prefix = f"org/{org_id}/"
    for key in list_keys(prefix):
        if call_id in key:
            try:
                objects.delete(key)
            except Exception:
                continue
