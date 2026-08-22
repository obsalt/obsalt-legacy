"""Inheritable conformance tests. Skipping a required test needs a marker + reason."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from obsalt.assemble.assembler import stamp_events
from obsalt.assemble.fidelity import derive_coverage, derive_fidelity
from obsalt.domain.enums import MeasurementPlacement, Signal, SignalCoverageStatus
from obsalt.domain.events import CallObserved, GroundingObserved, NormalizedEvent, StageObserved
from obsalt.otel.conventions import is_pii_attr, tool_span_name, turn_span_name
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope

from obsalt_testkit.schema import FixtureSuite, validate_raw_fixtures


class DecoderConformanceTests:
    plugin_cls: ClassVar[Any]
    fixtures_dir: ClassVar[Path]
    org_id: ClassVar[str] = "org-conformance"

    @pytest.fixture
    def suite(self) -> FixtureSuite:
        return FixtureSuite(self.fixtures_dir)

    @pytest.fixture
    def plugin(self) -> Any:
        inst = self.plugin_cls
        return inst() if isinstance(inst, type) else inst

    def _envelope(self, raw: bytes, name: str = "raw") -> RawEnvelope:
        return RawEnvelope(
            envelope_id=name,
            org_id=self.org_id,
            provider=getattr(
                self.plugin_cls if not isinstance(self.plugin_cls, type) else self.plugin_cls(),
                "name",
                "example",
            ),
            connection_id="c1",
            object_key="k",
            body=raw,
            delivery_key=name,
            received_at="2026-08-22T00:00:00+00:00",
        )

    def _decode_all(self, plugin: Any, suite: FixtureSuite) -> list[list[NormalizedEvent]]:
        streams = []
        for path, _payload in suite.raw_payloads():
            events = list(plugin.decode(self._envelope(path.read_bytes(), path.stem)))
            streams.append(events)
        return streams

    def test_fixtures_validate_against_vendor_schema(self, suite: FixtureSuite) -> None:
        errors = [e for e in validate_raw_fixtures(suite) if "DIVERGENCE" not in e]
        assert errors == []

    def test_idempotent_decode(self, plugin: Any, suite: FixtureSuite) -> None:
        for path, _ in suite.raw_payloads():
            raw = path.read_bytes()
            first = [e.model_dump() for e in plugin.decode(self._envelope(raw, path.stem))]
            second = [e.model_dump() for e in plugin.decode(self._envelope(raw, path.stem))]
            assert first == second

    def test_stable_fact_ids(self, plugin: Any, suite: FixtureSuite) -> None:
        for path, _ in suite.raw_payloads():
            events = list(plugin.decode(self._envelope(path.read_bytes(), path.stem)))
            stamped = stamp_events(
                events,
                org_id=self.org_id,
                source=plugin.name,
                envelope_id="e1",
                decoder_version=getattr(plugin, "DECODER_VERSION", "1"),
                processing_run_id="run",
            )
            ids = [e.fact_id for e in stamped]
            assert all(ids)
            again = stamp_events(
                events,
                org_id=self.org_id,
                source=plugin.name,
                envelope_id="e2",
                decoder_version=getattr(plugin, "DECODER_VERSION", "1"),
                processing_run_id="run2",
            )
            assert [e.fact_id for e in again] == ids

    def test_unknown_event_types_do_not_crash(self, plugin: Any) -> None:
        raw = json.dumps({"type": "totally-unknown-obsalt-event", "id": "x"}).encode()
        list(plugin.decode(self._envelope(raw, "unknown")))

    def test_call_identity_extracted(self, plugin: Any, suite: FixtureSuite) -> None:
        found = False
        for events in self._decode_all(plugin, suite):
            for event in events:
                if isinstance(event, CallObserved) and event.source_call_id:
                    found = True
        assert found

    def test_declaration_matches_possible_output(self, plugin: Any, suite: FixtureSuite) -> None:
        declaration = plugin.fidelity
        placements = set()
        architectures = set()
        for events in self._decode_all(plugin, suite):
            for event in events:
                if isinstance(event, StageObserved):
                    placements.add(event.placement)
                if isinstance(event, CallObserved) and event.architecture:
                    architectures.add(event.architecture)
        assert placements <= set(declaration.possible_placements)
        assert architectures <= set(declaration.possible_architectures)

    def test_grounding_populated_where_supplied(self, plugin: Any, suite: FixtureSuite) -> None:
        declaration = plugin.fidelity
        provides_grounding = {
            Signal.GROUNDING_SYSTEM_PROMPT,
            Signal.GROUNDING_TOOL_RESULTS,
            Signal.GROUNDING_USER_TEXT,
            Signal.GROUNDING_KNOWLEDGE,
        } & set(declaration.provides)
        if not provides_grounding:
            pytest.skip("plugin does not claim grounding")
        found = False
        for events in self._decode_all(plugin, suite):
            if any(isinstance(e, GroundingObserved) for e in events):
                found = True
        assert found, "plugin claims grounding but fixtures emit none"

    def test_no_pii_in_span_names(self) -> None:
        assert turn_span_name(7) == "turn"
        assert tool_span_name("create_booking") == "execute_tool"
        assert not is_pii_attr("turn.index")
        assert is_pii_attr("obsalt.pii.user_transcript")

    def test_interval_requires_real_timestamps(self, plugin: Any, suite: FixtureSuite) -> None:
        for events in self._decode_all(plugin, suite):
            for event in events:
                if isinstance(event, StageObserved) and event.placement is MeasurementPlacement.INTERVAL:
                    assert event.started_at is not None and event.ended_at is not None

    def test_structurally_unsupported_not_present_in_fixtures(self, plugin: Any, suite: FixtureSuite) -> None:
        declaration = plugin.fidelity
        if not declaration.structurally_absent:
            return
        for events in self._decode_all(plugin, suite):
            coverage = derive_coverage(events, decoder_version="test", declaration=declaration)
            present = {row.signal for row in coverage if row.status is SignalCoverageStatus.PRESENT}
            leaked = present & set(declaration.structurally_absent)
            assert not leaked, f"declared unsupported but present: {leaked}"

    def test_fidelity_derived_from_decode_output(self, plugin: Any, suite: FixtureSuite) -> None:
        for events in self._decode_all(plugin, suite):
            fidelity = derive_fidelity(events)
            assert fidelity.value in {p.value for p in type(fidelity)}

    def test_golden_expected_outputs(self, plugin: Any, suite: FixtureSuite) -> None:
        for path, _ in suite.raw_payloads():
            expected = suite.expected_for(path.name)
            if expected is None:
                continue
            events = [
                e.model_dump(mode="json") for e in plugin.decode(self._envelope(path.read_bytes(), path.stem))
            ]
            assert _strip(events) == _strip(expected)


class UnitsConformanceTests:
    """Dedicated seconds-vs-milliseconds assertions."""

    plugin_cls: ClassVar[Any]
    seconds_payload: ClassVar[bytes]
    field_path: ClassVar[str]
    expected_ms: ClassVar[float]

    def test_seconds_are_not_read_as_milliseconds(self) -> None:
        inst = self.plugin_cls
        plugin = inst() if isinstance(inst, type) else inst
        envelope = RawEnvelope(
            envelope_id="units",
            org_id="org",
            provider=plugin.name,
            connection_id="c",
            object_key="k",
            body=self.seconds_payload,
            delivery_key="units",
            received_at="2026-08-22T00:00:00+00:00",
        )
        values = []
        for event in plugin.decode(envelope):
            if (
                isinstance(event, StageObserved)
                and event.source_path
                and self.field_path in event.source_path
            ):
                values.append(event.value_ms)
            if hasattr(event, "started_at") and hasattr(event, "ended_at"):
                if event.started_at and event.ended_at:
                    values.append((event.ended_at - event.started_at).total_seconds() * 1000.0)
        assert values, f"no measurement produced for {self.field_path}"
        assert any(abs(v - self.expected_ms) < 1.0 for v in values)


class AuthConformanceTests:
    plugin_cls: ClassVar[Any]
    valid_headers: ClassVar[list[tuple[bytes, bytes]]]
    valid_body: ClassVar[bytes]
    secret_field: ClassVar[str]
    secret: ClassVar[str]

    def _plugin(self) -> Any:
        inst = self.plugin_cls
        return inst() if isinstance(inst, type) else inst

    def test_missing_credential_fails_closed(self) -> None:
        plugin = self._plugin()
        cfg = ConnectionConfig(org_id="o", provider=plugin.name, connection_id="c", credentials={})
        result = plugin.authenticate(self.valid_body, self.valid_headers, cfg)
        assert result.outcome.value == "missing_credential"

    def test_bad_signature_rejected(self) -> None:
        plugin = self._plugin()
        cfg = ConnectionConfig(
            org_id="o",
            provider=plugin.name,
            connection_id="c",
            credentials={self.secret_field: self.secret},
        )
        result = plugin.authenticate(self.valid_body + b"x", self.valid_headers, cfg)
        assert result.outcome.value in {"bad_signature", "malformed"}

    def test_valid_signature_accepted(self) -> None:
        plugin = self._plugin()
        cfg = ConnectionConfig(
            org_id="o",
            provider=plugin.name,
            connection_id="c",
            credentials={self.secret_field: self.secret},
        )
        result = plugin.authenticate(self.valid_body, self.valid_headers, cfg)
        assert result.ok


def _strip(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {
            k: _strip(v)
            for k, v in value.items()
            if k
            not in {
                "envelope_id",
                "processing_run_id",
                "event_occurred_at",
                "decoder_version",
            }
        }
    if isinstance(value, str) and value.endswith("+00:00"):
        return value.replace("+00:00", "Z")
    return value
