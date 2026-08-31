"""Small tests: console range parsing and filter-bar options."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obsalt.analysis.hangup import ENDING_NOT_REPORTED, ENDING_UNROOTED
from obsalt.domain.enums import HangupReason
from obsalt.ui.filters import parse_ui_clock, parse_ui_range, present_filter_bar
from obsalt.ui.present import hangup_option_groups
from tests.unit.test_present import _call


class _Plugin:
    name = "vapi"
    display_name = "Vapi"


def test_empty_query_is_the_last_seven_days() -> None:
    now = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
    parsed = parse_ui_range({}, now=now)
    assert parsed.implicit_default
    assert parsed.preset == "7d"
    assert parsed.valid
    assert parsed.end == now
    assert parsed.start == now - timedelta(days=7)
    assert parsed.range_qs == ""


def test_preset_24h_is_exactly_one_day_wide() -> None:
    now = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
    parsed = parse_ui_range({"preset": "24h"}, now=now)
    assert parsed.preset == "24h"
    assert not parsed.implicit_default
    assert parsed.end - parsed.start == timedelta(hours=24)
    assert parsed.range_qs == "preset=24h"


def test_date_only_covers_the_whole_utc_day() -> None:
    parsed = parse_ui_range({"start": "2026-08-22", "end": "2026-08-22"})
    assert parsed.start == datetime(2026, 8, 22, 0, 0, 0, tzinfo=UTC)
    assert parsed.end == datetime(2026, 8, 22, 23, 59, 59, 999999, tzinfo=UTC)
    assert parsed.start_date == "2026-08-22"
    assert "2026-08-22" in parsed.start_raw or parsed.start_raw == "2026-08-22"


def test_iso_passthrough_keeps_the_submitted_strings() -> None:
    start = "2020-01-01T00:00:00Z"
    end = "2030-01-01T00:00:00Z"
    parsed = parse_ui_range({"start": start, "end": end})
    assert parsed.start_raw == start
    assert parsed.end_raw == end
    assert parsed.preset == ""
    assert parsed.start_date == "2020-01-01"
    assert parsed.end_date == "2030-01-01"


def test_datetime_local_is_utc() -> None:
    clock = parse_ui_clock("2026-08-22T12:00", end_of_day=False)
    assert clock == datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_invalid_or_one_sided_range_is_not_valid() -> None:
    assert not parse_ui_range({"start": "2026-08-22"}).valid
    assert not parse_ui_range({"start": "nope", "end": "also-nope"}).valid


def test_source_options_use_plugin_display_names_and_keep_unknown_current() -> None:
    bar = present_filter_bar(
        action="/v1/ui",
        ui_range=parse_ui_range({}),
        values={"source": "custom-bot", "agent_id": "ghost"},
        fields=("source", "agent"),
        plugins=[_Plugin()],
        calls=[_call()],
    )
    sources = dict(bar["source_options"])
    assert sources["vapi"] == "Vapi"
    assert sources["example"] == "example"
    assert sources["custom-bot"] == "custom-bot"
    agents = dict(bar["agent_options"])
    assert agents["support"] == "support"
    assert agents["ghost"] == "ghost"


def test_hangup_groups_cover_every_reason_once() -> None:
    groups = hangup_option_groups()
    values = [value for group in groups for value, _label in group["options"]]
    assert set(reason.value for reason in HangupReason) <= set(values)
    assert ENDING_NOT_REPORTED in values
    assert ENDING_UNROOTED in values
    assert len(values) == len(set(values))
    assert any(group["label"] == "Lost callers" for group in groups)
    assert any(group["label"] == "Not reported" for group in groups)


def test_unreported_agent_is_labelled_not_named_unknown() -> None:
    missing = _call(agent_id="unknown")
    bar = present_filter_bar(
        action="/v1/ui",
        ui_range=parse_ui_range({}),
        fields=("agent",),
        calls=[missing],
    )
    agents = dict(bar["agent_options"])
    assert agents["unknown"] == "Agent not reported"
    assert "unknown" not in {label for _value, label in bar["agent_options"] if _value == "support"}


def test_eval_and_latency_fields_round_trip() -> None:
    parsed = parse_ui_range({})
    bar = present_filter_bar(
        action="/v1/ui",
        ui_range=parsed,
        values={"eval_result": "fail", "latency_ms": "800"},
        fields=("eval", "latency"),
    )
    assert bar["show_eval"]
    assert bar["show_latency"]
    assert bar["eval_result"] == "fail"
    chips = {chip["key"]: chip["label"] for chip in bar["active_chips"]}
    assert "eval_result" in chips
    assert "latency_ms" in chips
