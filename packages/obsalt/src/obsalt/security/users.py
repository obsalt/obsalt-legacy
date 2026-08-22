"""Org users. Browser roles come from here; API keys stay hashed and scoped."""

from __future__ import annotations

from typing import Any

from obsalt.domain.enums import Role
from obsalt.util import new_id, utcnow


class MemoryUserStore:
    durable = False

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}

    def upsert(
        self, org_id: str, email: str, role: Role | str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        role_value = role.value if isinstance(role, Role) else str(role)
        existing = self.get_by_email(org_id, email)
        rid = user_id or (existing["id"] if existing else new_id())
        item = {
            "id": rid,
            "org_id": org_id,
            "email": email.lower(),
            "role": role_value,
            "created_at": existing["created_at"] if existing else utcnow(),
        }
        self.users[rid] = item
        return item

    def get(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        item = self.users.get(user_id)
        if item is None or item["org_id"] != org_id:
            return None
        return item

    def get_by_email(self, org_id: str, email: str) -> dict[str, Any] | None:
        wanted = email.lower()
        for item in self.users.values():
            if item["org_id"] == org_id and item["email"] == wanted:
                return item
        return None

    def list(self, org_id: str) -> list[dict[str, Any]]:
        return [item for item in self.users.values() if item["org_id"] == org_id]

    def delete(self, org_id: str, user_id: str) -> bool:
        item = self.users.get(user_id)
        if item is None or item["org_id"] != org_id:
            return False
        del self.users[user_id]
        return True
