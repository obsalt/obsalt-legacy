from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt.assemble.assembler import fold_events, stamp_events
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import MeasurementPlacement, TimelineFidelity
from obsalt.domain.events import AggregateObserved, StageObserved, TurnObserved
from obsalt.plugin.protocol import RawEnvelope
from obsalt_retell.plugin import RetellPlugin
from obsalt_testkit.conformance import DecoderConformanceTests, UnitsConformanceTests


class TestRetellDecoder(DecoderConformanceTests):
    plugin_cls = RetellPlugin
    fixtures_dir = Path(str(files("obsalt_retell") / "fixtures"))


class TestRetellUnits(UnitsConformanceTests):
    plugin_cls = RetellPlugin
    seconds_payload = (
        Path(str(files("obsalt_retell") / "fixtures" / "raw" / "call_ended.json"))
    ).read_bytes()
    field_path = "words.start"
    expected_ms = 300.0  # 0.4s to 0.7s


def test_retell_samples_and_aggregates_are_separate() -> None:
    plugin = RetellPlugin()
    raw = Path(str(files("obsalt_retell") / "fixtures" / "raw" / "call_ended.json")).read_bytes()
    envelope = RawEnvelope(
        envelope_id="t",
        org_id="o",
        provider="retell",
        connection_id="c",
        object_key="k",
        body=raw,
        delivery_key="t",
        received_at="2026-08-22T00:00:00+00:00",
    )
    events = list(plugin.decode(envelope))
    samples = [e for e in events if isinstance(e, StageObserved) and e.stage.value == "e2e"]
    aggs = [e for e in events if isinstance(e, AggregateObserved) and e.stage.value == "e2e"]
    assert [e.value_ms for e in samples] == [580, 900]
    assert {e.statistic.value: e.value_ms for e in aggs}["p50"] == 620
    assert {e.statistic.value: e.value_ms for e in aggs}["p95"] == 900
    turns = [e for e in events if isinstance(e, TurnObserved)]
    first = turns[0]
    assert first.started_at and first.ended_at
    assert abs((first.ended_at - first.started_at).total_seconds() * 1000.0 - 300.0) < 1.0

    stamped = stamp_events(
        events,
        org_id="o",
        source="retell",
        envelope_id="e",
        decoder_version="retell/2",
        processing_run_id="r",
    )
    revision = fold_events(stamped, org_id="o", source="retell", source_call_id="retell-call-happy-1")
    view = timeline_view(revision)
    assert view["waterfall"] is False
    assert revision.lifecycle.timeline_fidelity in {
        TimelineFidelity.TURN_LEVEL,
        TimelineFidelity.MESSAGE_LEVEL,
    }
    assert all(
        m.placement is not MeasurementPlacement.INTERVAL or m.started_at for m in revision.stage_measurements
    )
