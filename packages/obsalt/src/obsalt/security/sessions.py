"""Signed server-side session cookies. Not a substitute for hashed API keys."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from obsalt.crypto.primitives import constant_time_eq
from obsalt.domain.enums import KeyScope, Role

ROLE_SCOPES: dict[Role, frozenset[KeyScope]] = {
    Role.OWNER: frozenset(KeyScope),
    Role.ADMIN: frozenset({KeyScope.ADMIN, KeyScope.ANALYZE, KeyScope.READ, KeyScope.INGEST}),
    Role.ANALYST: frozenset({KeyScope.READ, KeyScope.ANALYZE}),
    Role.REVIEWER: frozenset({KeyScope.READ}),
}


@dataclass(frozen=True)
class SessionInfo:
    org_id: str
    role: Role
    csrf: str

    @property
    def scopes(self) -> frozenset[KeyScope]:
        return ROLE_SCOPES.get(self.role, frozenset({KeyScope.READ}))


def sign_session(
    org_id: str,
    secret: str,
    *,
    ttl_seconds: int = 12 * 60 * 60,
    role: str | Role = Role.ANALYST,
    csrf: str | None = None,
) -> str:
    exp = str(int(time.time()) + ttl_seconds)
    csrf_token = csrf or secrets.token_urlsafe(18)
    role_value = role.value if isinstance(role, Role) else str(role)
    body = f"{org_id}|{role_value}|{exp}|{csrf_token}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def read_session(token: str | None, secret: str) -> SessionInfo | None:
    if not token:
        return None
    parts = token.split("|")
    if len(parts) == 3:
        org_id, exp, sig = parts
        body = f"{org_id}|{exp}"
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not constant_time_eq(sig, expected):
            return None
        if _expired(exp):
            return None
        return SessionInfo(org_id=org_id, role=Role.ANALYST, csrf="")
    if len(parts) != 5:
        return None
    org_id, role_raw, exp, csrf, sig = parts
    body = f"{org_id}|{role_raw}|{exp}|{csrf}"
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not constant_time_eq(sig, expected):
        return None
    if _expired(exp):
        return None
    try:
        role = Role(role_raw)
    except ValueError:
        role = Role.ANALYST
    return SessionInfo(org_id=org_id, role=role, csrf=csrf)


def verify_session(token: str | None, secret: str) -> str | None:
    info = read_session(token, secret)
    return info.org_id if info else None


def session_csrf(token: str | None, secret: str) -> str | None:
    info = read_session(token, secret)
    return info.csrf if info and info.csrf else None


def check_csrf(session_token: str | None, provided: str | None, secret: str) -> bool:
    info = read_session(session_token, secret)
    if info is None or not info.csrf or not provided:
        return False
    return constant_time_eq(info.csrf, provided)


def _expired(exp: str) -> bool:
    try:
        return int(exp) < int(time.time())
    except ValueError:
        return True
