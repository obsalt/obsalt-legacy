from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt.domain.enums import MeasurementPlacement, Provenance, Stage
from obsalt.domain.events import StageObserved
from obsalt.plugin.protocol import RawEnvelope
from obsalt_testkit.conformance import DecoderConformanceTests
from obsalt_vapi.ended_reasons import coverage
from obsalt_vapi.plugin import VapiPlugin


class TestVapiDecoder(DecoderConformanceTests):
    plugin_cls = VapiPlugin
    fixtures_dir = Path(str(files("obsalt_vapi") / "fixtures"))


def test_vapi_reads_published_latency_keys() -> None:
    plugin = VapiPlugin()
    raw = (Path(str(files("obsalt_vapi") / "fixtures" / "raw" / "end_of_call.json"))).read_bytes()
    envelope = RawEnvelope(
        envelope_id="t",
        org_id="o",
        provider="vapi",
        connection_id="c",
        object_key="k",
        body=raw,
        delivery_key="t",
        received_at="2026-08-22T00:00:00+00:00",
    )
    stages = [e for e in plugin.decode(envelope) if isinstance(e, StageObserved)]
    keys = {e.source_path for e in stages}
    assert any(p and "transcriberLatency" in p for p in keys)
    assert any(p and "modelLatency" in p for p in keys)
    assert any(p and "voiceLatency" in p for p in keys)
    assert any(p and "turnLatency" in p for p in keys)
    assert any(p and "endpointingLatency" in p for p in keys)
    assert not any(p and p.endswith(".stt") for p in keys)
    for event in stages:
        assert event.provenance is Provenance.PROVIDER_REPORTED
        assert event.placement is MeasurementPlacement.UNPLACED
        assert event.started_at is None and event.ended_at is None
    llm = [e for e in stages if e.stage is Stage.LLM]
    assert any(e.value_ms == 320 for e in llm)


def test_vapi_ended_reason_coverage() -> None:
    mapped, total, unmapped = coverage()
    assert total
    assert mapped / total > 0.95, unmapped
