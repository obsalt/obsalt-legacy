"""Outbound Standard Webhooks (call.finalized, eval.failed, flag.raised, slo.breached)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from uuid import uuid4

STANDARD_EVENTS = ("call.finalized", "eval.failed", "flag.raised", "slo.breached")


def sign(secret: bytes, body: bytes, *, timestamp: int | None = None, msg_id: str | None = None) -> dict[str, str]:
    ts = str(timestamp or int(time.time()))
    msg_id = msg_id or f"msg_{uuid4().hex}"
    to_sign = f"{msg_id}.{ts}.".encode() + body
    digest = hmac.new(secret, to_sign, hashlib.sha256).digest()
    sig = "v1," + base64.b64encode(digest).decode("ascii")
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sig,
    }


def parse_whsec(stored: str) -> bytes:
    if stored.startswith("whsec_"):
        stored = stored[len("whsec_") :]
    return base64.b64decode(stored)
