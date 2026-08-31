"""Authentication primitives for plugins to compose rather than reimplement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from obsalt.domain.enums import VerifyOutcome
from obsalt.plugin.types import VerifyResult


def constant_time_eq(left: str | bytes, right: str | bytes) -> bool:
    a = left.encode("utf-8") if isinstance(left, str) else left
    b = right.encode("utf-8") if isinstance(right, str) else right
    if len(a) != len(b):
        return hmac.compare_digest(hashlib.sha256(a).digest(), hashlib.sha256(b).digest()) and False
    return hmac.compare_digest(a, b)


def hmac_hex(key: str | bytes, message: bytes, digestmod: str = "sha256") -> str:
    secret = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(secret, message, digestmod).hexdigest()


def hmac_base64(key: str | bytes, message: bytes, digestmod: str = "sha256") -> str:
    secret = key.encode("utf-8") if isinstance(key, str) else key
    digest = hmac.new(secret, message, digestmod).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_kv_header(value: str, *, pair_sep: str = ",", kv_sep: str = "=") -> dict[str, str]:
    out: dict[str, str] = {}
    if not value:
        return out
    for part in value.split(pair_sep):
        part = part.strip()
        if not part:
            continue
        if kv_sep not in part:
            continue
        key, rest = part.split(kv_sep, 1)
        out[key.strip()] = rest.strip()
    return out


def enforce_window(
    timestamp: int | float,
    *,
    now: float | None = None,
    tolerance_seconds: float,
    unit: str = "s",
    one_sided: bool = False,
) -> VerifyResult | None:
    """Return a failure result if the timestamp is outside the window, else None.

    ElevenLabs documents a one-sided 30-minute window. We still enforce both
    sides by default (`one_sided=False`) so future-dated payloads fail closed.
    """
    current = now if now is not None else time.time()
    ts = float(timestamp)
    if unit == "ms":
        ts = ts / 1000.0
        current_s = current if current < 1e12 else current / 1000.0
    else:
        current_s = current if current < 1e12 else current / 1000.0
    delta = ts - current_s
    if one_sided:
        if delta < -tolerance_seconds:
            return VerifyResult(
                outcome=VerifyOutcome.STALE, detail="timestamp older than tolerance"
            )
        if delta > tolerance_seconds:
            return VerifyResult(outcome=VerifyOutcome.STALE, detail="timestamp in the future")
        return None
    if abs(delta) > tolerance_seconds:
        kind = VerifyOutcome.STALE if delta < 0 else VerifyOutcome.STALE
        return VerifyResult(outcome=kind, detail=f"timestamp outside ±{tolerance_seconds}s")
    return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_hs256_sign(payload: Mapping[str, object], secret: str) -> str:
    """Mint an HS256 JWT for tests and plugin composition."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(dict(payload), separators=(",", ":")).encode())
    signing = f"{header}.{body}".encode("ascii")
    sig = _b64url(hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def jwt_hs256_verify(
    token: str, secret: str, *, now: float | None = None
) -> dict[str, object] | None:
    """Minimal HS256 JWT validation for plugin composition. Fail closed on expiry."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    signing = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
    pad = "=" * (-len(sig_b64) % 4)
    try:
        provided = base64.urlsafe_b64decode(sig_b64 + pad)
    except ValueError:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    pad_p = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad_p))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if exp is not None:
        current = now if now is not None else time.time()
        try:
            if float(exp) < current:
                return None
        except (TypeError, ValueError):
            return None
    return payload


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def header_values(headers: list[tuple[bytes, bytes]] | Mapping[str, str], name: str) -> list[str]:
    want = name.lower().encode("latin-1") if isinstance(name, str) else name.lower()
    if isinstance(headers, Mapping):
        found: list[str] = []
        for key, value in headers.items():
            if key.lower() == name.lower():
                found.append(value)
        return found
    values: list[str] = []
    for hdr_key, hdr_value in headers:
        key_bytes = (
            hdr_key.lower() if isinstance(hdr_key, bytes) else hdr_key.lower().encode("latin-1")
        )
        if key_bytes == want:
            values.append(
                hdr_value.decode("latin-1") if isinstance(hdr_value, bytes) else str(hdr_value)
            )
    return values


def require_singleton(
    headers: list[tuple[bytes, bytes]], singleton: frozenset[bytes]
) -> VerifyResult | None:
    counts: dict[bytes, int] = {}
    for key, _value in headers:
        lowered = key.lower()
        if lowered in singleton:
            counts[lowered] = counts.get(lowered, 0) + 1
            if counts[lowered] > 1:
                return VerifyResult(
                    outcome=VerifyOutcome.MALFORMED,
                    detail=f"duplicate singleton header {lowered.decode('latin-1')}",
                )
    return None
