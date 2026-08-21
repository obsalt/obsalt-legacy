from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4, uuid5, UUID

OBSALT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_ISO_Z = re.compile(r"Z$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        elif ts > 1e10:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or re.fullmatch(r"\d+\.\d+", text):
            return parse_datetime(float(text))
        try:
            return datetime.fromisoformat(_ISO_Z.sub("+00:00", text)).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def ms_from_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value) * 1000.0
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def dig(data: Any, *path: str | int, default: Any = None) -> Any:
    current = data
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key, default if not isinstance(key, int) else None)
            if current is None and isinstance(key, str):
                return default
        elif isinstance(current, (list, tuple)) and isinstance(key, int):
            try:
                current = current[key]
            except IndexError:
                return default
        else:
            return default
    return current if current is not None else default


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def call_id_for(org_id: str, provider: str, provider_call_id: str) -> str:
    return str(uuid5(OBSALT_NAMESPACE, f"{org_id}:{provider}:{provider_call_id}"))


def new_id() -> str:
    return str(uuid4())
