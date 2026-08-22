"""Standard Webhooks outbound events. PII-minimal payloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from uuid import uuid4

STANDARD_TOLERANCE_S = 300


def new_signing_secret() -> str:
    raw = hashlib.sha256(uuid4().bytes + uuid4().bytes).digest()
    return "whsec_" + base64.b64encode(raw).decode("ascii")


def sign(
    payload: bytes,
    *,
    secret: str,
    msg_id: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    ts = timestamp if timestamp is not None else int(time.time())
    key = _secret_bytes(secret)
    signed = f"{msg_id}.{ts}.".encode() + payload
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    sig = "v1," + base64.b64encode(digest).decode("ascii")
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sig,
    }


def verify(
    payload: bytes,
    headers: dict[str, str],
    secret: str,
    *,
    now: int | None = None,
    tolerance_s: int = STANDARD_TOLERANCE_S,
) -> bool:
    msg_id = headers.get("webhook-id") or headers.get("Webhook-Id")
    ts_raw = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp")
    sigs = headers.get("webhook-signature") or headers.get("Webhook-Signature") or ""
    if not msg_id or not ts_raw:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > tolerance_s:
        return False
    expected = sign(payload, secret=secret, msg_id=msg_id, timestamp=ts)["webhook-signature"]
    want = expected.split(",", 1)[1]
    for part in sigs.split():
        if "," not in part:
            continue
        version, value = part.split(",", 1)
        if version != "v1":
            continue
        if hmac.compare_digest(value, want):
            return True
    return False


def _secret_bytes(secret: str) -> bytes:
    raw = secret[6:] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw)
    except Exception:
        return raw.encode("utf-8")
