"""Coverage and fidelity are derived from decode output, not marketing copy."""

from __future__ import annotations

from datetime import UTC, datetime

from obsalt.domain.coverage import derive_coverage, derive_fidelity
from obsalt.domain.enums import (
    GroundingKind,
    MeasurementPlacement,
    Metric,
    Provenance,
    Signal,
    SignalCoverageStatus,
    Speaker,
    Stage,
    TimelineFidelity,
)
from obsalt.domain.events import GroundingObserved, OutcomeObserved, StageObserved, TurnObserved
from obsalt_example.plugin import ExamplePlugin
from obsalt_vapi.plugin import VapiPlugin

from tests.helpers import EXAMPLE_FIXTURES, VAPI_FIXTURES


def _envelope(path, provider: str = "example"):
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    return RawEnvelope(
        envelope_id=new_id(),
        org_id="acme",
        provider=provider,
        connection_id="c1",
        object_key="k",
        delivery_key="d",
        content_sha256="x",
        body=path.read_bytes(),
        received_at=utcnow(),
    )


def test_example_coverage_marks_stage_interval_unsupported_and_stt_present() -> None:
    plugin = ExamplePlugin()
    events = list(plugin.decode(_envelope(EXAMPLE_FIXTURES / "raw" / "call_ended.json")))
    rows = {row.signal: row for row in derive_coverage(events, plugin.fidelity, "example/1")}
    assert rows[Signal.STAGE_INTERVAL].status is SignalCoverageStatus.UNSUPPORTED
    assert rows[Signal.STT_DURATION].status is SignalCoverageStatus.PRESENT
    assert rows[Signal.STT_DURATION].source_path == "turns[0].stt_ms"
    assert rows[Signal.GROUNDING_USER].status is SignalCoverageStatus.PRESENT
    assert rows[Signal.VAD].status is SignalCoverageStatus.UNSUPPORTED
    assert derive_fidelity(events) is TimelineFidelity.TURN_LEVEL


def test_vapi_coverage_does_not_claim_unobtainable_stage_intervals() -> None:
    plugin = VapiPlugin()
    events = list(plugin.decode(_envelope(VAPI_FIXTURES / "raw" / "end_of_call.json", "vapi")))
    rows = {row.signal: row for row in derive_coverage(events, plugin.fidelity, plugin.decoder_version)}
    assert rows[Signal.STAGE_INTERVAL].status is SignalCoverageStatus.UNSUPPORTED
    assert rows[Signal.STT_DURATION].status is SignalCoverageStatus.PRESENT
    assert "transcriberLatency" in (rows[Signal.STT_DURATION].source_path or "")
    assert derive_fidelity(events) is TimelineFidelity.TURN_LEVEL


def test_declared_but_missing_signal_is_absent_not_unsupported() -> None:
    from tests.helpers import fidelity_declaration

    events = [
        TurnObserved(turn_index=0, speaker=Speaker.USER, text="hi"),
        StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=10,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="stt_ms",
        ),
        GroundingObserved(kind=GroundingKind.SYSTEM_PROMPT, content="be helpful", provenance=Provenance.PROVIDER_REPORTED),
        OutcomeObserved(provider_code="user_hangup"),
    ]
    declaration = fidelity_declaration()
    rows = {row.signal: row for row in derive_coverage(events, declaration, "t/1")}
    assert rows[Signal.TOOL_RESULT].status is SignalCoverageStatus.ABSENT
    assert "not present on this payload" in (rows[Signal.TOOL_RESULT].reason or "")
    assert rows[Signal.VAD].status is SignalCoverageStatus.UNSUPPORTED


def test_interval_without_end_does_not_raise_fidelity_to_stage_level() -> None:
    start = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    events = [
        StageObserved(
            stage=Stage.LLM,
            metric=Metric.DURATION,
            value_ms=200,
            placement=MeasurementPlacement.INTERVAL,
            started_at=start,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="span:llm",
        )
    ]
    assert derive_fidelity(events) is not TimelineFidelity.STAGE_LEVEL
