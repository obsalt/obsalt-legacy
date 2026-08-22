from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

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
        if ts > 1e10:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or re.fullmatch(r"-?\d+\.\d+", text):
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
