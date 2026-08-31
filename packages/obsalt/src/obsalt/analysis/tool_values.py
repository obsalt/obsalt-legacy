"""Parsed tool/grounding scalars. Keys are a filter, never spoken facts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from obsalt.domain.models import ToolInvocation

_MONEY_PATH_PARTS = ("amount", "price", "total", "charge", "refund", "balance", "cost", "fare")
_ID_PATH_PARTS = (
    "id",
    "number",
    "order",
    "confirmation",
    "booking",
    "ticket",
    "invoice",
    "reference",
    "case",
)
_COUNT_PATH_PARTS = ("count", "quantity", "qty", "items", "totalitems", "results")
_DATETIME_PATH_PARTS = (
    "date",
    "time",
    "when",
    "scheduled",
    "appointment",
    "slot",
    "starts",
    "ends",
)
_NOT_MONEY_PATH_PARTS = ("count", "page_size", "limit", "status", "retry", "timeout", "http")
_ID_SHAPE_RE = re.compile(r"^[a-z]+\d{3,}$")
_MONEY_STRIP_RE = re.compile(r"[$,]|usd|dollars", re.I)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_DAY_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.I,
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)


class ToolScalar(BaseModel):
    path: str
    value: Any
    as_decimal: Decimal | None = None
    as_id: str | None = None
    as_datetime: datetime | None = None
    as_count: int | None = None
    money_like: bool = False
    id_like: bool = False
    count_like: bool = False


def parse_money(span: str) -> Decimal | None:
    raw = _MONEY_STRIP_RE.sub("", span).strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


def normalize_id(span: str) -> str:
    return re.sub(r"[^a-z0-9]", "", span.lower())


def month_day_span(text: str) -> str | None:
    match = _MONTH_DAY_RE.search(text) or _DAY_MONTH_RE.search(text)
    return match.group(0) if match else None


def has_month_day(text: str) -> bool:
    return month_day_span(text) is not None


def parse_datetime_span(span: str, *, anchor: datetime) -> datetime | None:
    """Absolute dates/times only. Relative-only spans return None, never guessed.

    Date-less times ("3pm") anchor to ``anchor``'s date; year-less month-days
    anchor to ``anchor``'s year. ``anchor`` must be tz-aware.
    """
    text = span.strip()
    if not text:
        return None
    parsed = _iso_or_month_day(text)
    if parsed is not None:
        return parsed
    month_day = _MONTH_DAY_RE.search(text) or _DAY_MONTH_RE.search(text)
    match = _TIME_RE.search(text)
    if match is None and month_day is None:
        return None
    month = anchor.month
    day = anchor.day
    if month_day is not None:
        resolved_month, resolved_day = _month_day(month_day)
        if resolved_month is None:
            return None
        month, day = resolved_month, resolved_day
    if match is None:
        year = _year_in(text) or anchor.year
        try:
            return datetime(year, month, day, tzinfo=anchor.tzinfo or UTC)
        except ValueError:
            return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    half = match.group(3).lower()
    if half == "pm" and hour != 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    try:
        return anchor.replace(
            month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
        )
    except ValueError:
        return None


def _year_in(text: str) -> int | None:
    match = re.search(r"\b(20\d{2})\b", text)
    return int(match.group(1)) if match else None


def _iso_or_month_day(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("T", " "))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    month_day = _MONTH_DAY_RE.search(text) or _DAY_MONTH_RE.search(text)
    if month_day is None:
        return None
    month, day = _month_day(month_day)
    if month is None:
        return None
    year = _year_in(text)
    if year is None:
        return None
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _month_day(match: re.Match[str]) -> tuple[int | None, int]:
    groups = match.groups()
    month_text, day_text = groups[-2], groups[-1]
    if month_text is None or not month_text[:3].isalpha():
        month_text, day_text = groups[-1], groups[-2]
    month = _MONTHS.get(str(month_text).lower()[:3])
    if month is None or not str(day_text).isdigit():
        return None, 0
    return month, int(day_text)


def money_equal(left: Decimal, right: Decimal) -> bool:
    return left.quantize(Decimal("0.01")) == right.quantize(Decimal("0.01"))


def id_like_for_spoken(scalar: ToolScalar, spoken: str) -> bool:
    if not scalar.as_id:
        return False
    if scalar.id_like:
        return True
    spoken_n = normalize_id(spoken)
    other = scalar.as_id
    if abs(len(spoken_n) - len(other)) <= 2:
        return True
    return bool(_ID_SHAPE_RE.match(spoken_n) and _ID_SHAPE_RE.match(other))


def tool_scalar_values(tool: ToolInvocation) -> list[ToolScalar]:
    """Walk args and result as JSON. Values only; keys are a filter, never spoken facts."""
    found: list[ToolScalar] = []
    if tool.args not in (None, ""):
        found.extend(_walk(_parse_jsonish(tool.args), "$"))
    if tool.result not in (None, ""):
        found.extend(_walk(_parse_jsonish(tool.result), "$"))
    if tool.error:
        found.extend(_walk(tool.error, "$.error"))
    return found


def tool_arg_scalars(tool: ToolInvocation) -> list[ToolScalar]:
    """Scalars from tool arguments only, never the result body."""
    if tool.args in (None, ""):
        return []
    return _walk(_parse_jsonish(tool.args), "$")


def content_scalars(content: str) -> list[ToolScalar] | None:
    """Walk a grounding snippet if it is JSON. None means treat as text."""
    text = content.strip()
    if not text or text[0] not in "{[":
        return None
    parsed = _parse_jsonish(text)
    if parsed is text or not isinstance(parsed, (dict, list)):
        return None
    return _walk(parsed, "$")


def tool_result_text(tool: ToolInvocation) -> str:
    if tool.result not in (None, ""):
        if isinstance(tool.result, str):
            return tool.result
        return json.dumps(tool.result, default=str)
    if tool.error:
        return str(tool.error)
    return ""


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _walk(node: Any, path: str) -> list[ToolScalar]:
    if isinstance(node, dict):
        out: list[ToolScalar] = []
        for key, child in node.items():
            out.extend(_walk(child, f"{path}.{key}"))
        return out
    if isinstance(node, list):
        out = [_length_scalar(path, len(node))]
        for index, child in enumerate(node):
            out.extend(_walk(child, f"{path}[{index}]"))
        return out
    if isinstance(node, bool) or node is None:
        return []
    return [_scalar(path, node)]


def _length_scalar(path: str, length: int) -> ToolScalar:
    return ToolScalar(
        path=f"{path}.length",
        value=length,
        as_decimal=Decimal(length).quantize(Decimal("0.01")),
        as_count=length,
        count_like=True,
    )


def _scalar(path: str, value: Any) -> ToolScalar:
    as_decimal = _decimal_value(value)
    as_id = normalize_id(str(value)) if isinstance(value, (str, int)) else None
    if isinstance(value, float):
        as_id = None
    money_like = False
    if as_decimal is not None:
        if _path_has(path, _NOT_MONEY_PATH_PARTS) and _is_integral(as_decimal):
            money_like = False
        elif _is_currency_or_decimal_raw(value) or not _is_integral(as_decimal):
            money_like = True
        elif _path_has(path, _MONEY_PATH_PARTS):
            money_like = True
    id_like = bool(as_id) and (
        _path_has(path, _ID_PATH_PARTS) or bool(as_id and _ID_SHAPE_RE.match(as_id))
    )
    as_count = None
    count_like = False
    if as_decimal is not None and not money_like and _is_integral(as_decimal):
        if _path_has(path, _COUNT_PATH_PARTS):
            as_count = int(as_decimal)
            count_like = True
    as_datetime = None
    if isinstance(value, str):
        if _path_has(path, _DATETIME_PATH_PARTS):
            parsed = _iso_or_month_day(value)
        else:
            parsed = _strict_iso(value)
        if parsed is not None:
            as_datetime = parsed
    return ToolScalar(
        path=path,
        value=value,
        as_decimal=as_decimal,
        as_id=as_id or None,
        as_datetime=as_datetime,
        as_count=as_count,
        money_like=money_like,
        id_like=id_like,
        count_like=count_like,
    )


def _strict_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text or not re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return None
    return _iso_or_month_day(text)


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value).quantize(Decimal("0.01"))
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    if isinstance(value, str):
        return parse_money(value)
    return None


def _is_integral(value: Decimal) -> bool:
    return value == value.to_integral_value()


def _is_currency_or_decimal_raw(value: Any) -> bool:
    if isinstance(value, str) and (
        "$" in value or "dollar" in value.lower() or "usd" in value.lower()
    ):
        return True
    if isinstance(value, str) and re.search(r"\.\d{1,2}\b", value):
        return True
    return False


def _path_has(path: str, parts: tuple[str, ...]) -> bool:
    segments = [item for item in path.lower().replace("[", ".").replace("]", "").split(".") if item]
    return any(any(part == seg or part in seg for part in parts) for seg in segments)
