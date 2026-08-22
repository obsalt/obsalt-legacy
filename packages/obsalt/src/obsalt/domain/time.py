from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_ISO_Z = re.compile(r"Z$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e10:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or re.fullmatch(r"-?\d+\.\d+", text):
            return parse_datetime(float(text))
        try:
            return datetime.fromisoformat(_ISO_Z.sub("+00:00", text)).astimezone(UTC)
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
