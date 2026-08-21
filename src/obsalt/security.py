from __future__ import annotations

import hashlib
import hmac
from typing import Any


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_vapi(headers: dict[str, str], secret: str) -> bool:
    if not secret:
        return True
    provided = headers.get("x-vapi-secret") or headers.get("X-Vapi-Secret") or ""
    return constant_time_equals(provided, secret)


def verify_retell(headers: dict[str, str], body: bytes, secret: str) -> bool:
    if not secret:
        return True
    provided = headers.get("x-retell-signature") or headers.get("X-Retell-Signature") or ""
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return constant_time_equals(provided, expected)


def verify_bland(headers: dict[str, str], secret: str) -> bool:
    if not secret:
        return True
    provided = (
        headers.get("x-webhook-secret")
        or headers.get("X-Webhook-Secret")
        or headers.get("authorization")
        or headers.get("Authorization")
        or ""
    )
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    return constant_time_equals(provided, secret)


def header_map(raw: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in dict(raw).items()}
