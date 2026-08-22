"""Small declarative field mapping helper for the boring majority of decoder fields.

Not a DSL. Event discrimination, unit conversion, and pairing stay in Python.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from obsalt.util import as_float, as_str, dig, parse_datetime


class Ts:
    """Mark a path as a timestamp."""

    def __init__(self, path: str) -> None:
        self.path = path


class FieldMap:
    def __init__(self, mapping: dict[str, str | Sequence[str] | Ts | Callable[[dict[str, Any]], Any]]) -> None:
        self.mapping = mapping

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dest, spec in self.mapping.items():
            out[dest] = self._read(payload, spec)
        return out

    def _read(self, payload: dict[str, Any], spec: Any) -> Any:
        if isinstance(spec, Ts):
            return parse_datetime(_path(payload, spec.path))
        if callable(spec) and not isinstance(spec, str):
            return spec(payload)
        if isinstance(spec, (list, tuple)):
            for item in spec:
                value = self._read(payload, item)
                if value is not None and value != "":
                    return value
            return None
        if isinstance(spec, str):
            return _path(payload, spec)
        return None


def _path(payload: dict[str, Any], dotted: str) -> Any:
    parts: list[str | int] = []
    for part in dotted.split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            parts.append(part)
    return dig(payload, *parts)


def str_field(value: Any) -> str | None:
    return as_str(value)


def float_field(value: Any) -> float | None:
    return as_float(value)


def dt_field(value: Any) -> datetime | None:
    return parse_datetime(value)
