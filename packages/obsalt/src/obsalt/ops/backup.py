"""Backup expiry. Tombstones survive restore; expired backups cannot resurrect deleted calls."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from obsalt.plugin.types import TombstoneHints
from obsalt.util import new_id, utcnow


def record_backup(
    state: Any, *, taken_at: datetime | None = None, retention_days: int | None = None
) -> dict[str, Any]:
    taken = taken_at or utcnow()
    days = (
        retention_days
        if retention_days is not None
        else int(getattr(state.settings, "backup_retention_days", 30))
    )
    item = {
        "id": new_id(),
        "taken_at": taken,
        "expires_at": taken + timedelta(days=days),
        "status": "retained",
        "note": "Managed backups expire. External warehouse copies cannot be revoked.",
    }
    backups = getattr(state, "backups", None)
    if backups is None:
        state.backups = []
        backups = state.backups
    backups.append(item)
    return item


def expire_backups(state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    expired = 0
    for item in getattr(state, "backups", None) or []:
        expires = item.get("expires_at")
        if expires is not None and expires <= now and item.get("status") != "expired":
            item["status"] = "expired"
            expired += 1
    return {
        "expired": expired,
        "retained": sum(
            1
            for item in (getattr(state, "backups", None) or [])
            if item.get("status") == "retained"
        ),
    }


def restore_allowed(
    state: Any,
    *,
    org_id: str,
    source_call_id: str | None,
    caller_token: str | None = None,
    backup_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A tombstoned call cannot be restored from a managed backup, expired or not."""

    now = now or utcnow()
    if source_call_id or caller_token:
        if state.inbox.is_tombstoned(
            org_id, TombstoneHints(source_call_id=source_call_id, caller_token=caller_token)
        ):
            return {"allowed": False, "reason": "deletion cannot be undone"}
    if backup_id:
        backup = next(
            (
                item
                for item in (getattr(state, "backups", None) or [])
                if item.get("id") == backup_id
            ),
            None,
        )
        if backup is None:
            return {"allowed": False, "reason": "unknown backup"}
        if backup.get("status") == "expired" or (
            backup.get("expires_at") and backup["expires_at"] <= now
        ):
            return {"allowed": False, "reason": "backup expired"}
    return {"allowed": True, "reason": None}
