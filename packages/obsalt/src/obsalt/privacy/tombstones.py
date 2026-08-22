from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from hmac import HMAC


@dataclass(frozen=True, slots=True)
class Tombstone:
    org_id: str
    kind: str  # call | caller | range
    source_call_id: str | None = None
    caller_token: str | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None


def caller_token(org_id: str, caller: str, *, key: bytes) -> str:
    """Per-org keyed HMAC retained only for privacy operations."""
    mac = HMAC(key, f"{org_id}:{caller}".encode(), sha256)
    return mac.hexdigest()


def matches(
    stone: Tombstone, *, source_call_id: str | None, caller: str | None, when: datetime | None
) -> bool:
    if stone.org_id and source_call_id and stone.kind == "call" and stone.source_call_id == source_call_id:
        return True
    if stone.kind == "caller" and stone.caller_token and caller and stone.caller_token == caller:
        return True
    if stone.kind == "range" and when and stone.range_start and stone.range_end:
        return stone.range_start <= when <= stone.range_end
    return False
