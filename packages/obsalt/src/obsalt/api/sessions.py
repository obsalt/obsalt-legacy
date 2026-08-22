"""Browser sessions. Secure cookie, not a query-string API key."""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "obsalt_session"
CSRF_COOKIE = "obsalt_csrf"
MAX_AGE = 14 * 24 * 3600


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def dump_session(secret: str, payload: dict[str, Any]) -> str:
    return _serializer(secret, "obsalt-session").dumps(payload)


def load_session(secret: str, token: str) -> dict[str, Any] | None:
    try:
        loaded = _serializer(secret, "obsalt-session").loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return loaded if isinstance(loaded, dict) else None


def dump_csrf(secret: str, nonce: str) -> str:
    return _serializer(secret, "obsalt-csrf").dumps(nonce)


def check_csrf(secret: str, token: str, nonce: str) -> bool:
    try:
        loaded = _serializer(secret, "obsalt-csrf").loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(loaded == nonce)
