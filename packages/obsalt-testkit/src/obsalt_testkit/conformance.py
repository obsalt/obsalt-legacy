"""Inheritable conformance tests. Skipping a required test needs an explicit marker plus a written reason."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from obsalt.assemble.facts import fact_id_for
from obsalt.crypto.primitives import require_singleton
from obsalt.domain.enums import (
    GroundingKind,
    MeasurementPlacement,
    VerifyOutcome,
)
from obsalt.domain.events import (
    CallObserved,
    GroundingObserved,
    NormalizedEvent,
    StageObserved,
    ToolObserved,
)
from obsalt.domain.models import FidelityDeclaration
from obsalt.ingest.headers import RawHeaders
from obsalt.plugin.contract import WebhookSource
from obsalt.plugin.types import ConnectionConfig, RawEnvelope
from obsalt.testing.fakes import MemoryResolver
from obsalt.util import new_id, utcnow

# Core stamps these after decode. Plugin decode usually leaves them unset; strip
# them from golden NormalizedEvent dumps so fixtures stay stable across runs.
DEFAULT_IGNORE_FIELDS: frozenset[str] = frozenset(
    {
        "envelope_id",
        "processing_run_id",
        "event_occurred_at",
        "fact_id",
    }
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_ignore_fields(fixtures_dir: Path) -> frozenset[str]:
    """Load `fixtures/ignore_fields` (one field per line). Falls back to DEFAULT_IGNORE_FIELDS."""

    path = fixtures_dir / "ignore_fields"
    if not path.exists():
        return DEFAULT_IGNORE_FIELDS
    fields: set[str] = set()
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        fields.add(text)
    return frozenset(fields) if fields else DEFAULT_IGNORE_FIELDS


def stable_event_dump(event: NormalizedEvent, ignore: frozenset[str] | None = None) -> dict[str, Any]:
    """JSON-mode dump with volatile core-stamped fields removed."""

    skip = ignore if ignore is not None else DEFAULT_IGNORE_FIELDS
    data = event.model_dump(mode="json")
    for key in skip:
        data.pop(key, None)
    return data


def decode_raw_fixture(plugin: Any, path: Path, *, org_id: str = "test-org") -> list[NormalizedEvent]:
    body = path.read_bytes()
    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id=org_id,
        provider=getattr(plugin, "name", "example"),
        connection_id="c1",
        object_key="k",
        delivery_key=path.name,
        content_sha256="x",
        body=body,
        received_at=utcnow(),
    )
    return list(plugin.decode(envelope))


class DecoderConformanceTests:
    """Required for every decoder/mapper. Subclass and provide plugin + fixtures_dir."""

    plugin: WebhookSource
    fixtures_dir: Path
    org_id: str = "test-org"
    decoder_version: str = "test/1"

    def raw_payloads(self) -> list[Path]:
        raw = self.fixtures_dir / "raw"
        if not raw.exists():
            return []
        return sorted(p for p in raw.iterdir() if p.suffix == ".json")

    def decode_raw(self, path: Path) -> list[NormalizedEvent]:
        return decode_raw_fixture(self.plugin, path, org_id=self.org_id)

    def test_idempotent_decode(self) -> None:
        for path in self.raw_payloads():
            first = [e.model_dump() for e in self.decode_raw(path)]
            second = [e.model_dump() for e in self.decode_raw(path)]
            assert first == second

    def test_stable_fact_ids(self) -> None:
        for path in self.raw_payloads():
            events = self.decode_raw(path)
            ids = [fact_id_for(e) for e in events]
            again = [fact_id_for(e) for e in self.decode_raw(path)]
            assert ids == again

    def test_unknown_event_types_do_not_crash(self) -> None:
        envelope = RawEnvelope(
            envelope_id=new_id(),
            org_id=self.org_id,
            provider="x",
            connection_id="c",
            object_key="k",
            delivery_key="d",
            content_sha256="x",
            body=b'{"type":"definitely-not-a-real-event","junk":true}',
        )
        list(self.plugin.decode(envelope))

    def test_call_identity_extraction(self) -> None:
        for path in self.raw_payloads():
            events = self.decode_raw(path)
            calls = [e for e in events if isinstance(e, CallObserved)]
            if calls:
                assert calls[0].source_call_id

    def test_grounding_populated_where_supplied(self) -> None:
        for path in self.raw_payloads():
            payload = json.loads(path.read_text())
            events = self.decode_raw(path)
            grounding = [e for e in events if isinstance(e, GroundingObserved)]
            if _payload_has_prompt(payload) or _payload_has_tool_result(payload):
                kinds = {e.kind for e in grounding}
                if _payload_has_prompt(payload):
                    assert GroundingKind.SYSTEM_PROMPT in kinds or GroundingKind.KNOWLEDGE in kinds
                if _payload_has_tool_result(payload):
                    assert GroundingKind.TOOL_RESULT in kinds or any(
                        isinstance(e, ToolObserved) and e.result not in (None, "") for e in events
                    )

    def test_declaration_matches_decode_output(self) -> None:
        declaration: FidelityDeclaration = self.plugin.fidelity  # type: ignore[attr-defined]
        for path in self.raw_payloads():
            for event in self.decode_raw(path):
                if isinstance(event, StageObserved):
                    assert event.placement in declaration.possible_placements
                    if event.placement is MeasurementPlacement.INTERVAL:
                        assert event.started_at is not None and event.ended_at is not None

    def test_no_pii_in_span_names(self) -> None:
        from obsalt.otel.conventions import SPAN_TOOL, SPAN_TURN

        assert "{" not in SPAN_TURN
        assert "execute_tool" == SPAN_TOOL


class SecondsVsMillisecondsTests:
    """Dedicated class: unit mistakes here produced the 290ms-vs-740ms error."""

    def assert_seconds_field_converted(self, raw_seconds: float, decoded_ms: float, *, places: int = 0) -> None:
        expected = raw_seconds * 1000.0
        assert decoded_ms == pytest.approx(expected, rel=0, abs=10 ** -places or 0.5)

    def assert_millisecond_field_unchanged(self, raw_ms: float, decoded_ms: float) -> None:
        assert decoded_ms == pytest.approx(raw_ms, rel=0, abs=0.5)


class AuthenticationConformanceTests:
    plugin: WebhookSource
    connection: ConnectionConfig
    valid_raw: bytes
    valid_headers: dict[str, str]

    def _resolver(self) -> MemoryResolver:
        resolver = MemoryResolver()
        resolver.add(self.connection, "ingest-key")
        return resolver

    def test_missing_credential_fail_closed(self) -> None:
        cfg = self.connection.model_copy(update={"secrets": {}})
        result = self.plugin.authenticate(self.valid_raw, RawHeaders.from_mapping(self.valid_headers).as_list(), cfg)
        assert result.outcome is VerifyOutcome.MISSING_CREDENTIAL
        assert not result.ok

    def test_bad_signature_rejected(self) -> None:
        headers = dict(self.valid_headers)
        for key in list(headers):
            headers[key] = "deadbeef"
        result = self.plugin.authenticate(
            self.valid_raw, RawHeaders.from_mapping(headers).as_list(), self.connection
        )
        assert result.outcome in {VerifyOutcome.BAD_SIGNATURE, VerifyOutcome.MALFORMED}
        assert not result.ok

    def test_duplicate_singleton_headers_rejected(self) -> None:
        singleton = getattr(self.plugin, "singleton_headers", frozenset())
        if not singleton:
            pytest.skip("plugin declares no singleton headers")
        name = next(iter(singleton))
        pairs = [(name, b"a"), (name, b"b")]
        dup = require_singleton(pairs, singleton)
        assert dup is not None
        assert dup.outcome is VerifyOutcome.MALFORMED


class SchemaFixtureTests:
    """Schema gate plus golden NormalizedEvent comparison.

    Subclasses should set ``fixtures_dir`` and, for expected-output comparison,
    ``plugin`` (a webhook decoder).
    """

    fixtures_dir: Path
    plugin: Any = None
    org_id: str = "test-org"

    def _json_schemas(self) -> list[Path]:
        schema_dir = self.fixtures_dir / "schema"
        if not schema_dir.exists():
            return []
        return [
            p
            for p in schema_dir.glob("*.json")
            if '"$schema"' in p.read_text()[:500] or p.name.endswith(".schema.json")
        ]

    def _schema_for(self, raw: Path, schemas: list[Path]) -> Path:
        for schema in schemas:
            if schema.stem == raw.stem or raw.stem.replace("-", "_") in schema.stem:
                return schema
        return schemas[0]

    def test_raw_fixtures_validate_against_vendored_schema(self) -> None:
        schema_dir = self.fixtures_dir / "schema"
        raw_dir = self.fixtures_dir / "raw"
        if not schema_dir.exists() or not raw_dir.exists():
            pytest.skip("no schema/raw fixtures")
        schemas = self._json_schemas()
        if not schemas:
            pytest.skip("no json schema")
        overlay_dir = self.fixtures_dir / "schema_overlays"
        if overlay_dir.exists():
            for overlay in overlay_dir.glob("*.json"):
                meta = json.loads(overlay.read_text())
                for required in ("original_failure", "owner", "review_date", "expiry"):
                    assert required in meta, f"overlay {overlay.name} missing {required}"
        validators = {p: jsonschema.Draft202012Validator(json.loads(p.read_text())) for p in schemas}
        for raw in sorted(raw_dir.glob("*.json")):
            payload = json.loads(raw.read_text())
            validators[self._schema_for(raw, schemas)].validate(payload)

    def test_raw_fixtures_match_expected_normalized_events(self) -> None:
        raw_dir = self.fixtures_dir / "raw"
        expected_dir = self.fixtures_dir / "expected"
        if not raw_dir.exists():
            pytest.skip("no raw fixtures")
        raws = sorted(p for p in raw_dir.glob("*.json"))
        if not raws:
            pytest.skip("no raw fixtures")
        if self.plugin is None:
            pytest.skip("SchemaFixtureTests.plugin not set")
        if not expected_dir.exists():
            pytest.fail(
                f"raw fixtures exist under {raw_dir} but {expected_dir} is missing; "
                "write stable NormalizedEvent dumps (see obsalt_testkit.stable_event_dump)"
            )
        ignore = load_ignore_fields(self.fixtures_dir)
        for raw in raws:
            expected_path = expected_dir / raw.name
            assert expected_path.exists(), f"missing expected golden {expected_path.name}"
            events = decode_raw_fixture(self.plugin, raw, org_id=self.org_id)
            actual = [stable_event_dump(event, ignore) for event in events]
            expected = json.loads(expected_path.read_text())
            assert actual == expected, f"golden mismatch for {raw.name}"


def _payload_has_prompt(payload: Any) -> bool:
    blob = json.dumps(payload).lower()
    return "system" in blob and ("prompt" in blob or '"role": "system"' in blob or '"role":"system"' in blob)


def _payload_has_tool_result(payload: Any) -> bool:
    blob = json.dumps(payload).lower()
    return "tool" in blob and ("result" in blob or "tool_call_result" in blob)
