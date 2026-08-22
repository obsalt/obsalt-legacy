"""Small declarative field-mapping helper for the boring majority of decode.

Not a DSL. Event discrimination, unit normalization, and tool pairing stay in
Python. ``Ts`` marks a path that should be parsed as a timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from obsalt.domain.time import parse_datetime


@dataclass(frozen=True, slots=True)
class Ts:
    path: str


@dataclass(frozen=True, slots=True)
class SecondsToMs:
    path: str


def dig(data: Any, path: str, default: Any = None) -> Any:
    current = data
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, Mapping):
            current = current.get(part, default)
        else:
            return default
    return current if current is not None else default


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


class FieldMap:
    def __init__(self, mapping: Mapping[str, str | Sequence[str] | Ts | SecondsToMs]) -> None:
        self.mapping = dict(mapping)

    def extract(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dest, spec in self.mapping.items():
            out[dest] = self._read(payload, spec)
        return out

    def _read(self, payload: Mapping[str, Any], spec: str | Sequence[str] | Ts | SecondsToMs) -> Any:
        if isinstance(spec, Ts):
            return parse_datetime(dig(payload, spec.path))
        if isinstance(spec, SecondsToMs):
            raw = dig(payload, spec.path)
            if raw is None or raw == "":
                return None
            return float(raw) * 1000.0
        if isinstance(spec, str):
            return dig(payload, spec)
        for path in spec:
            value = dig(payload, path)
            if value is not None and value != "":
                return value
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


def as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
