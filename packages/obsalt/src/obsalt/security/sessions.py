"""Signed server-side session cookies. Not a substitute for hashed API keys."""

from __future__ import annotations

import hashlib
import hmac
import time

from obsalt.crypto.primitives import constant_time_eq


def sign_session(org_id: str, secret: str, *, ttl_seconds: int = 12 * 60 * 60) -> str:
    exp = str(int(time.time()) + ttl_seconds)
    body = f"{org_id}|{exp}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def verify_session(token: str | None, secret: str) -> str | None:
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    org_id, exp, sig = parts
    body = f"{org_id}|{exp}"
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not constant_time_eq(sig, expected):
        return None
    try:
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    return org_id
