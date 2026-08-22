"""Outbound Standard Webhooks (call.finalized, eval.failed, flag.raised, slo.breached)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

from obsalt.egress import EgressDenied, validate_destination, validate_redirect
from obsalt.util import canonical_json, utcnow

log = logging.getLogger("obsalt.webhooks")

STANDARD_EVENTS = ("call.finalized", "eval.failed", "flag.raised", "slo.breached")
SCHEMA_VERSION = "1"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 529})


def sign(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    msg_id: str | None = None,
    extra_secrets: list[bytes] | None = None,
) -> dict[str, str]:
    ts = str(timestamp or int(time.time()))
    msg_id = msg_id or f"msg_{uuid4().hex}"
    to_sign = f"{msg_id}.{ts}.".encode() + body
    signatures = [_v1_sig(secret, to_sign)]
    for extra in extra_secrets or []:
        signatures.append(_v1_sig(extra, to_sign))
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": " ".join(signatures),
    }


def _v1_sig(secret: bytes, to_sign: bytes) -> str:
    digest = hmac.new(secret, to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


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
    extra_secrets: list[bytes] | None = None,
) -> tuple[bool, str]:
    """POST a PII-minimal payload. HTTPS-only unless localhost is opted in for tests."""
    try:
        validate_destination(url, allow_http_localhost=allow_http_localhost)
    except EgressDenied as exc:
        return False, f"permanent: {exc}"
    headers = sign(secret, body, extra_secrets=extra_secrets)
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
    if 300 <= response.status_code < 400:
        location = response.headers.get("location") or ""
        try:
            nxt = validate_redirect(url, location, allow_http_localhost=allow_http_localhost)
        except EgressDenied as exc:
            return False, f"permanent: {exc}"
        try:
            bounced = http.post(nxt, content=body, headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return False, f"retryable: {exc}"
        if 200 <= bounced.status_code < 300:
            return True, "ok"
        if 300 <= bounced.status_code < 400:
            return False, "permanent: nested redirect refused"
        kind = "retryable" if bounced.status_code in RETRYABLE_STATUS else "permanent"
        return False, f"{kind}: HTTP {bounced.status_code}"
    kind = "retryable" if response.status_code in RETRYABLE_STATUS else "permanent"
    return False, f"{kind}: HTTP {response.status_code}"


def emit_call_finalized(state: Any, revision: Any) -> None:
    emit_standard_event(state, revision, "call.finalized")


def emit_standard_event(
    state: Any,
    revision: Any,
    event_type: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Enqueue one Standard Webhooks event. Stable id; never a second logical event."""
    if event_type not in STANDARD_EVENTS and event_type != "*":
        return
    store = getattr(state, "webhook_store", None)
    dests = (
        store.list_destinations(revision.org_id)
        if getattr(store, "durable", False)
        else list(getattr(state, "webhook_destinations", None) or [])
    )
    if not dests:
        return
    payload = {
        "type": event_type,
        "schema_version": SCHEMA_VERSION,
        "org_id": revision.org_id,
        "call_id": revision.call_id,
        "revision": revision.revision,
        "source": revision.source,
    }
    if extra:
        payload.update(extra)
    suffix = extra.get("kind") if extra else None
    event_id = f"{event_type}:{revision.org_id}:{revision.call_id}:{revision.revision}"
    if suffix:
        event_id = f"{event_id}:{suffix}"
    if getattr(store, "durable", False):
        for dest in dests:
            event = dest.get("event_type") or dest.get("event") or "call.finalized"
            if event not in {event_type, "*"}:
                continue
            store.enqueue({"event_id": event_id, "dest": dest, "payload": payload, "attempts": 0})
        drain_outbound(state)
        return
    outbox = getattr(state, "webhook_outbox", None)
    if outbox is None:
        state.webhook_outbox = []
        outbox = state.webhook_outbox
    if any(item.get("event_id") == event_id for item in outbox):
        return
    for dest in dests:
        if dest.get("org_id") and dest["org_id"] != revision.org_id:
            continue
        event = dest.get("event_type") or dest.get("event") or "call.finalized"
        if event not in {event_type, "*"}:
            continue
        outbox.append(
            {
                "event_id": event_id,
                "dest": dest,
                "payload": payload,
                "attempts": 0,
            }
        )
    drain_outbound(state)


def maybe_emit_slo(state: Any, revision: Any) -> None:
    threshold = float(getattr(getattr(state, "settings", None), "slo_e2e_ms", 2000.0) or 2000.0)
    values = [float(item.value_ms) for item in getattr(revision, "stage_measurements", []) or []]
    if not values:
        return
    observed = max(values)
    if observed > threshold:
        emit_standard_event(
            state,
            revision,
            "slo.breached",
            {"threshold_ms": threshold, "value_ms": observed},
        )


def _overlap_secrets(dest: dict[str, Any]) -> list[bytes]:
    extras: list[bytes] = []
    previous = dest.get("previous_secret")
    expires = dest.get("previous_secret_expires_at")
    if not previous:
        return extras
    if expires is not None:
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                expires = None
        if expires is not None and expires < utcnow():
            return extras
    if isinstance(previous, bytes):
        extras.append(previous)
    else:
        extras.append(parse_whsec(str(previous)))
    return extras


def drain_outbound(state: Any) -> int:
    store = getattr(state, "webhook_store", None)
    durable = getattr(store, "durable", False)
    outbox = store.claim(32) if durable else list(getattr(state, "webhook_outbox", None) or [])
    remaining: list[dict[str, Any]] = []
    delivered = 0
    for item in outbox:
        dest = item["dest"]
        if not dest.get("url"):
            continue
        body = canonical_json(item["payload"]).encode()
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
            extra_secrets=_overlap_secrets(dest),
        )
        if ok:
            delivered += 1
            if durable:
                store.mark_delivered(item["event_id"])
            continue
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_error"] = detail
        if item["attempts"] < 8 and str(detail).startswith("retryable"):
            remaining.append(item)
            if durable:
                store.mark_failed(item["event_id"], detail, item["attempts"])
        else:
            log.warning("outbound dlq %s to %s: %s", item["payload"]["type"], dest.get("url"), detail)
            from obsalt.metrics import dlq_inserts_total

            dlq_inserts_total.inc()
            if durable:
                store.mark_failed(item["event_id"], detail, item["attempts"])
    if not durable:
        state.webhook_outbox = remaining
    return delivered
