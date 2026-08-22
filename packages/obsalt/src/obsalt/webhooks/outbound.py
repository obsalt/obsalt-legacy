"""Outbound Standard Webhooks (call.finalized, eval.failed, flag.raised, slo.breached)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any
from uuid import uuid4

import httpx

from obsalt.egress import EgressDenied, validate_destination
from obsalt.util import canonical_json

log = logging.getLogger("obsalt.webhooks")

STANDARD_EVENTS = ("call.finalized", "eval.failed", "flag.raised", "slo.breached")
SCHEMA_VERSION = "1"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 529})


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


def mint_whsec() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode("ascii")


def deliver(
    url: str,
    secret: bytes,
    body: bytes,
    *,
    allow_http_localhost: bool = False,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> tuple[bool, str]:
    """POST a PII-minimal payload. HTTPS-only unless localhost is opted in for tests."""
    try:
        validate_destination(url, allow_http_localhost=allow_http_localhost)
    except EgressDenied as exc:
        return False, f"permanent: {exc}"
    headers = sign(secret, body)
    headers["Content-Type"] = "application/json"
    own = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        response = http.post(url, content=body, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        return False, f"retryable: {exc}"
    except httpx.HTTPError as exc:
        return False, f"retryable: {exc}"
    finally:
        if own:
            http.close()
    if 200 <= response.status_code < 300:
        return True, "ok"
    kind = "retryable" if response.status_code in RETRYABLE_STATUS else "permanent"
    return False, f"{kind}: HTTP {response.status_code}"


def emit_call_finalized(state: Any, revision: Any) -> None:
    """Fire call.finalized after a promoted revision. Never a second logical event on retry."""
    dests = list(getattr(state, "webhook_destinations", None) or [])
    if not dests:
        return
    payload = {
        "type": "call.finalized",
        "schema_version": SCHEMA_VERSION,
        "org_id": revision.org_id,
        "call_id": revision.call_id,
        "revision": revision.revision,
        "source": revision.source,
    }
    body = canonical_json(payload).encode()
    for dest in dests:
        if dest.get("org_id") and dest["org_id"] != revision.org_id:
            continue
        event = dest.get("event_type") or dest.get("event") or "call.finalized"
        if event not in {"call.finalized", "*"}:
            continue
        secret_raw = dest.get("secret_bytes")
        if secret_raw is None:
            stored = dest.get("secret") or dest.get("whsec") or ""
            if not stored:
                continue
            secret_raw = parse_whsec(str(stored))
        elif isinstance(secret_raw, str):
            secret_raw = secret_raw.encode()
        allow = dest.get("allow_http_localhost") in {True, "true"}
        ok, detail = deliver(
            dest["url"],
            secret_raw,
            body,
            allow_http_localhost=bool(allow),
        )
        if not ok:
            log.warning("outbound %s to %s: %s", payload["type"], dest.get("url"), detail)
