"""Endpoint authorization matrix (§11.1). Roles map to actions; scopes remain the API-key gate."""

from __future__ import annotations

from obsalt.domain.enums import Role

# reviewer < analyst < admin < owner
_RANK = {
    Role.REVIEWER: 0,
    Role.ANALYST: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}

# Minimum role for each product action. Reviewer is read-only.
ACTIONS: dict[str, Role] = {
    "calls.read": Role.REVIEWER,
    "search.read": Role.REVIEWER,
    "fleet.read": Role.REVIEWER,
    "plugins.read": Role.REVIEWER,
    "quality.review": Role.REVIEWER,
    "calls.analyze": Role.ANALYST,
    "rubrics.write": Role.ANALYST,
    "settings.read": Role.ANALYST,
    "connections.write": Role.ADMIN,
    "replay": Role.ADMIN,
    "backfill": Role.ADMIN,
    "privacy.delete": Role.ADMIN,
    "webhooks.write": Role.ADMIN,
    "eval_runners.write": Role.ADMIN,
    "export": Role.ADMIN,
    "users.read": Role.ADMIN,
    "users.write": Role.OWNER,
    "keys.rotate": Role.OWNER,
}


def allowed(role: Role, action: str) -> bool:
    needed = ACTIONS.get(action)
    if needed is None:
        return False
    return _RANK[role] >= _RANK[needed]


def require_role(role: Role, action: str) -> bool:
    return allowed(role, action)


def can_map(role: Role | None) -> dict[str, bool]:
    if role is None:
        return {action: False for action in ACTIONS}
    return {action: allowed(role, action) for action in ACTIONS}
