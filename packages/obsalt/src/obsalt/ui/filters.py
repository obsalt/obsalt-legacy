"""Console filter-bar view-model and range parsing. Templates stay dumb."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from obsalt.domain.models import CallRevision
from obsalt.ui.present import (
    EVAL_RESULT_OPTIONS,
    FLAG_LABELS,
    flag_label,
    hangup_label,
    hangup_option_groups,
)
from obsalt.util import utcnow

PRESET_DURATIONS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

PRESET_LABELS: dict[str, str] = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
}

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAIVE_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$")


@dataclass(frozen=True)
class UiRange:
    start: datetime | None
    end: datetime | None
    start_raw: str
    end_raw: str
    start_date: str
    end_date: str
    preset: str
    implicit_default: bool
    window_label: str

    @property
    def range_qs(self) -> str:
        if self.implicit_default:
            return ""
        if self.preset:
            return urlencode({"preset": self.preset})
        if self.start_raw and self.end_raw:
            return urlencode({"start": self.start_raw, "end": self.end_raw})
        return ""

    @property
    def valid(self) -> bool:
        return self.start is not None and self.end is not None


def parse_ui_range(params: Mapping[str, str], *, now: datetime | None = None) -> UiRange:
    """Parse console start/end/preset. Empty query is the last 7 days."""

    clock = now or utcnow()
    preset = (params.get("preset") or "").strip()
    start_raw = (params.get("start") or "").strip()
    end_raw = (params.get("end") or "").strip()

    if preset in PRESET_DURATIONS:
        end_dt = clock
        start_dt = end_dt - PRESET_DURATIONS[preset]
        return _range_from_datetimes(start_dt, end_dt, preset=preset, implicit_default=False)

    if not start_raw and not end_raw:
        end_dt = clock
        start_dt = end_dt - PRESET_DURATIONS["7d"]
        return _range_from_datetimes(start_dt, end_dt, preset="7d", implicit_default=True)

    if not start_raw or not end_raw:
        return UiRange(
            start=None,
            end=None,
            start_raw=start_raw,
            end_raw=end_raw,
            start_date=_date_fragment(start_raw),
            end_date=_date_fragment(end_raw),
            preset="",
            implicit_default=False,
            window_label="",
        )

    parsed_start = parse_ui_clock(start_raw, end_of_day=False)
    parsed_end = parse_ui_clock(end_raw, end_of_day=True)
    if parsed_start is None or parsed_end is None:
        return UiRange(
            start=None,
            end=None,
            start_raw=start_raw,
            end_raw=end_raw,
            start_date=_date_fragment(start_raw),
            end_date=_date_fragment(end_raw),
            preset="",
            implicit_default=False,
            window_label="",
        )
    return UiRange(
        start=parsed_start,
        end=parsed_end,
        start_raw=start_raw,
        end_raw=end_raw,
        start_date=parsed_start.strftime("%Y-%m-%d"),
        end_date=parsed_end.strftime("%Y-%m-%d"),
        preset="",
        implicit_default=False,
        window_label=_window_label(parsed_start, parsed_end),
    )


def parse_ui_clock(raw: str, *, end_of_day: bool) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if _DATE_ONLY.fullmatch(text):
        year, month, day = (int(part) for part in text.split("-"))
        if end_of_day:
            return datetime(year, month, day, 23, 59, 59, 999999, tzinfo=UTC)
        return datetime(year, month, day, 0, 0, 0, tzinfo=UTC)
    if _NAIVE_LOCAL.fullmatch(text):
        try:
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def present_filter_bar(
    *,
    action: str,
    ui_range: UiRange,
    values: Mapping[str, str] | None = None,
    fields: Sequence[str] = (),
    plugins: Sequence[Any] = (),
    calls: Sequence[CallRevision] = (),
    submit_label: str = "Filter",
    next_cursor: str | None = None,
) -> dict[str, Any]:
    values = dict(values or {})
    agent_id = values.get("agent_id") or ""
    source = values.get("source") or ""
    hangup_reason = values.get("hangup_reason") or ""
    flag = values.get("flag") or ""
    query = values.get("q") or ""
    eval_result = values.get("eval_result") or ""
    latency_ms = values.get("latency_ms") or ""
    field_set = frozenset(fields)

    current = _current_params(ui_range, values, field_set)
    source_options = _source_options(plugins, calls, source)
    next_href = ""
    if next_cursor:
        paged = dict(current)
        paged["cursor"] = next_cursor
        next_href = _href(action, paged)

    examples: list[dict[str, str]] = []
    if "search" in field_set:
        for word in ("refund", "supervisor", "order id"):
            example = {key: val for key, val in current.items() if key != "q"}
            example["q"] = word
            examples.append({"label": word, "href": _href(action, example)})

    preset_hrefs = {
        key: _href(action, {**_without_range(current), "preset": key}) for key in PRESET_LABELS
    }
    source_labels = dict(source_options)

    return {
        "action": action,
        "submit_label": submit_label,
        "start": ui_range.start_raw,
        "end": ui_range.end_raw,
        "start_date": ui_range.start_date,
        "end_date": ui_range.end_date,
        "preset": ui_range.preset,
        "window_label": ui_range.window_label,
        "range_qs": ui_range.range_qs,
        "nav_qs": f"?{ui_range.range_qs}" if ui_range.range_qs else "",
        "detail_qs": ui_range.range_qs,
        "agent_id": agent_id,
        "source": source,
        "hangup_reason": hangup_reason,
        "flag": flag,
        "q": query,
        "eval_result": eval_result,
        "latency_ms": latency_ms,
        "show_search": "search" in field_set,
        "show_source": "source" in field_set,
        "show_agent": "agent" in field_set,
        "show_hangup": "hangup" in field_set,
        "show_flag": "flag" in field_set,
        "show_eval": "eval" in field_set,
        "show_latency": "latency" in field_set,
        "source_options": source_options,
        "agent_options": _agent_options(calls, agent_id),
        "hangup_groups": hangup_option_groups() if "hangup" in field_set else [],
        "flag_options": _flag_options(flag) if "flag" in field_set else [],
        "eval_options": EVAL_RESULT_OPTIONS if "eval" in field_set else [],
        "preset_hrefs": preset_hrefs,
        "preset_labels": PRESET_LABELS,
        "active_chips": _active_chips(action, ui_range, values, field_set, source_labels),
        "reset_href": action,
        "next_href": next_href,
        "examples": examples,
        "autofocus_search": "search" in field_set and not query,
    }


def _range_from_datetimes(
    start_dt: datetime,
    end_dt: datetime,
    *,
    preset: str,
    implicit_default: bool,
) -> UiRange:
    start_raw = _iso_z(start_dt)
    end_raw = _iso_z(end_dt)
    return UiRange(
        start=start_dt,
        end=end_dt,
        start_raw=start_raw,
        end_raw=end_raw,
        start_date=start_dt.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        preset=preset,
        implicit_default=implicit_default,
        window_label=_window_label(start_dt, end_dt),
    )


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_label(start: datetime, end: datetime) -> str:
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if start_utc.year != end_utc.year:
        fmt = "%d %b %Y %H:%M"
    else:
        fmt = "%d %b %H:%M"
    return f"{start_utc.strftime(fmt)} – {end_utc.strftime(fmt)} UTC"


def _date_fragment(raw: str) -> str:
    if _DATE_ONLY.fullmatch(raw):
        return raw
    parsed = parse_ui_clock(raw, end_of_day=False)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _current_params(
    ui_range: UiRange, values: Mapping[str, str], fields: frozenset[str]
) -> dict[str, str]:
    params: dict[str, str] = {}
    if not ui_range.implicit_default:
        if ui_range.preset:
            params["preset"] = ui_range.preset
        elif ui_range.start_raw and ui_range.end_raw:
            params["start"] = ui_range.start_raw
            params["end"] = ui_range.end_raw
    for key in ("agent_id", "source", "hangup_reason", "flag", "q", "eval_result", "latency_ms"):
        field = {
            "agent_id": "agent",
            "hangup_reason": "hangup",
            "q": "search",
            "eval_result": "eval",
            "latency_ms": "latency",
        }.get(key, key)
        if field in fields and values.get(key):
            params[key] = values[key]
    return params


def _without_range(params: Mapping[str, str]) -> dict[str, str]:
    return {key: val for key, val in params.items() if key not in {"start", "end", "preset"}}


def _active_chips(
    action: str,
    ui_range: UiRange,
    values: Mapping[str, str],
    fields: frozenset[str],
    source_labels: Mapping[str, str],
) -> list[dict[str, str]]:
    current = _current_params(ui_range, values, fields)
    chips: list[dict[str, str]] = []
    if not ui_range.implicit_default and ui_range.preset and ui_range.preset != "7d":
        dropped = dict(current)
        dropped.pop("preset", None)
        chips.append(
            {
                "key": "preset",
                "label": f"Range: {PRESET_LABELS[ui_range.preset]}",
                "href": _href(action, dropped),
            }
        )
    elif not ui_range.implicit_default and not ui_range.preset and ui_range.start_raw:
        dropped = dict(current)
        dropped.pop("start", None)
        dropped.pop("end", None)
        chips.append(
            {
                "key": "range",
                "label": f"Range: {ui_range.window_label or 'custom'}",
                "href": _href(action, dropped),
            }
        )
    labels = {
        "source": "Source",
        "agent_id": "Agent",
        "hangup_reason": "Hangup",
        "flag": "Flag",
        "q": "Query",
        "eval_result": "Eval",
        "latency_ms": "Latency ≥",
    }
    for key, prefix in labels.items():
        field = {
            "agent_id": "agent",
            "hangup_reason": "hangup",
            "q": "search",
            "eval_result": "eval",
            "latency_ms": "latency",
        }.get(key, key)
        value = values.get(key) or ""
        if field not in fields or not value:
            continue
        shown = value
        if key == "flag":
            shown = flag_label(value)
        elif key == "source":
            shown = source_labels.get(value) or value
        elif key == "hangup_reason":
            shown = hangup_label(value)
        elif key == "eval_result":
            shown = "passed" if value == "pass" else "failed"
        elif key == "latency_ms":
            shown = f"{value} ms"
        dropped = dict(current)
        dropped.pop(key, None)
        chips.append({"key": key, "label": f"{prefix}: {shown}", "href": _href(action, dropped)})
    return chips


def _source_options(
    plugins: Sequence[Any], calls: Sequence[CallRevision], current: str
) -> list[tuple[str, str]]:
    labels: dict[str, str] = {}
    for plugin in plugins:
        name = getattr(plugin, "name", None)
        if not name:
            continue
        labels[str(name)] = str(getattr(plugin, "display_name", None) or name)
    for call in calls:
        if call.source and call.source not in labels:
            labels[call.source] = call.source
    if current and current not in labels:
        labels[current] = current
    return [("", "Any"), *sorted(labels.items(), key=lambda item: item[1].lower())]


def _agent_options(calls: Sequence[CallRevision], current: str) -> list[tuple[str, str]]:
    ids = sorted({call.agent_id for call in calls if call.agent_id and call.agent_id != "unknown"})
    has_unreported = any((not call.agent_id) or call.agent_id == "unknown" for call in calls)
    if current and current not in ids and current != "unknown":
        ids = [current, *ids]
    options: list[tuple[str, str]] = [("", "Any")]
    if has_unreported or current == "unknown":
        options.append(("unknown", "Agent not reported"))
    options.extend((item, item) for item in ids)
    return options


def _flag_options(current: str) -> list[tuple[str, str]]:
    items = list(FLAG_LABELS.items())
    known = {key for key, _label in items}
    if current and current not in known:
        items.append((current, flag_label(current)))
    items.sort(key=lambda item: item[1].lower())
    return items


def _href(action: str, params: Mapping[str, str]) -> str:
    cleaned = {key: val for key, val in params.items() if val}
    if not cleaned:
        return action
    return f"{action}?{urlencode(cleaned)}"
