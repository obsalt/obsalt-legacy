"""Shared authentication primitives. Plugins compose these; they do not reimplement them.

``VerifyResult`` outcomes are explicit. No helper silently disables itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import timedelta

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from obsalt.domain.enums import VerifyOutcome
from obsalt.plugin.protocol import VerifyResult


def constant_time_eq(left: str | bytes, right: str | bytes) -> bool:
    a = left.encode("utf-8") if isinstance(left, str) else left
    b = right.encode("utf-8") if isinstance(right, str) else right
    if len(a) != len(b):
        return hmac.compare_digest(hashlib.sha256(a).digest(), hashlib.sha256(b).digest()) and False
    return hmac.compare_digest(a, b)


def hmac_hex(key: bytes | str, message: bytes | str, *, digest: str = "sha256") -> str:
    key_b = key.encode("utf-8") if isinstance(key, str) else key
    msg_b = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(key_b, msg_b, digest).hexdigest()


def hmac_base64(key: bytes | str, message: bytes | str, *, digest: str = "sha256") -> str:
    key_b = key.encode("utf-8") if isinstance(key, str) else key
    msg_b = message.encode("utf-8") if isinstance(message, str) else message
    digest_bytes = hmac.new(key_b, msg_b, digest).digest()
    return base64.b64encode(digest_bytes).decode("ascii")


def parse_kv_header(value: str, *, pair_sep: str = ",", kv_sep: str = "=") -> dict[str, str]:
    out: dict[str, list[str]] = {}
    if not value:
        return {}
    for part in value.split(pair_sep):
        part = part.strip()
        if not part or kv_sep not in part:
            continue
        key, raw = part.split(kv_sep, 1)
        out.setdefault(key.strip(), []).append(raw.strip())
    # last-wins for lookup convenience; callers that need multiples use parse_kv_multi
    return {k: v[-1] for k, v in out.items()}


def parse_kv_multi(value: str, *, pair_sep: str = ",", kv_sep: str = "=") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not value:
        return {}
    for part in value.split(pair_sep):
        part = part.strip()
        if not part or kv_sep not in part:
            continue
        key, raw = part.split(kv_sep, 1)
        out.setdefault(key.strip(), []).append(raw.strip())
    return out


def enforce_window(
    event_time_ms: int,
    *,
    now_ms: int | None = None,
    tolerance: timedelta = timedelta(minutes=5),
    one_sided: bool = False,
) -> VerifyResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    window_ms = int(tolerance.total_seconds() * 1000)
    delta = now - event_time_ms
    if one_sided:
        stale = delta > window_ms or event_time_ms - now > window_ms
    else:
        stale = abs(delta) > window_ms
    if stale:
        return VerifyResult(
            outcome=VerifyOutcome.STALE,
            detail="timestamp outside replay window",
            event_time_ms=event_time_ms,
        )
    return VerifyResult(outcome=VerifyOutcome.OK, event_time_ms=event_time_ms)


def ed25519_verify(public_key_hex: str, message: bytes, signature_hex: str) -> VerifyResult:
    try:
        key = VerifyKey(bytes.fromhex(public_key_hex))
        key.verify(message, bytes.fromhex(signature_hex))
    except (BadSignatureError, ValueError) as exc:
        return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail=str(exc))
    return VerifyResult(outcome=VerifyOutcome.OK)


def require_secret(secret: str | None, *, name: str) -> VerifyResult | None:
    if not secret:
        return VerifyResult(
            outcome=VerifyOutcome.MISSING_CREDENTIAL,
            detail=f"{name} is required; authentication does not fail open",
        )
    return None


def missing_header(name: str) -> VerifyResult:
    return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail=f"missing header {name}")


def header_values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    want = name.lower()
    return [value for key, value in headers if key.lower() == want]


def singleton_or_reject(
    headers: list[tuple[bytes, bytes]], name: bytes
) -> tuple[bytes | None, VerifyResult | None]:
    values = header_values(headers, name)
    if not values:
        return None, missing_header(name.decode("latin-1"))
    if len(values) > 1:
        return None, VerifyResult(
            outcome=VerifyOutcome.MALFORMED,
            detail=f"duplicate singleton header {name.decode('latin-1')}",
        )
    return values[0], None
