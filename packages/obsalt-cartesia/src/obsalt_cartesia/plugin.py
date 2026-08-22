"""Cartesia Line: real turn intervals + unplaced STT/TTS TTFBs. Auth is a plain shared secret."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

from obsalt.auth.primitives import constant_time_eq, require_secret, singleton_or_reject
from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    Metric,
    ObservationalEventKind,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
    VerifyOutcome,
)
from obsalt.domain.events import CallFinalized, CallObserved, NormalizedEvent, StageObserved, TurnObserved
from obsalt.domain.fieldmap import as_float, as_str
from obsalt.domain.time import parse_datetime
from obsalt.plugin.protocol import (
    ConnectionConfig,
    FidelityDeclaration,
    PluginManifest,
    RawEnvelope,
    TombstoneHints,
    VerifyResult,
    WebhookResponse,
)


class CartesiaPlugin:
    API_VERSION = 1
    name = "cartesia"
    display_name = "Cartesia Line"
    DECODER_VERSION = "cartesia/1"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-webhook-secret"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="cartesia.line.post_call",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.UNPLACED, MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.TURN_INTERVALS, Signal.STT_DURATION, Signal.TTS_TTFB, Signal.TRANSCRIPT}),
        structurally_absent={
            Signal.STAGE_INTERVALS: "Cartesia publishes turn intervals and unplaced STT/TTS TTFBs, not stage intervals",
        },
        schema_source="https://docs.cartesia.ai",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(
        self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig
    ) -> VerifyResult:
        missing = require_secret(cfg.credentials.get("webhook_secret"), name="webhook_secret")
        if missing:
            return missing
        value, err = singleton_or_reject(headers, b"x-webhook-secret")
        if err:
            return err
        assert value is not None
        if not constant_time_eq(value.decode("latin-1"), cfg.credentials["webhook_secret"]):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="shared secret mismatch")
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        return ObservationalEventKind.CALL_ENDED

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        call_id = as_str(payload.get("call_id") or payload.get("id"))
        return f"cartesia:{call_id}" if call_id else None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return TombstoneHints()
        return TombstoneHints(source_call_id=as_str(payload.get("call_id") or payload.get("id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        if not envelope.body:
            return []
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
        call_id = as_str(payload.get("call_id") or payload.get("id"))
        if not call_id:
            return []
        events: list[NormalizedEvent] = [
            CallObserved(
                source_call_id=call_id,
                agent_id=as_str(payload.get("agent_id")) or "unknown",
                architecture=PipelineArchitecture.CASCADE,
                source_path="call_id",
            )
        ]
        for i, item in enumerate(payload.get("turns") or []):
            if not isinstance(item, dict):
                continue
            started = parse_datetime(item.get("started_at"))
            ended = parse_datetime(item.get("ended_at"))
            events.append(
                TurnObserved(
                    turn_index=i,
                    speaker=Speaker.USER if item.get("speaker") == "user" else Speaker.AGENT,
                    text=as_str(item.get("text")) or "",
                    started_at=started,
                    ended_at=ended,
                    source_path="turns[].started_at",
                )
            )
            stt = as_float(item.get("stt_ttfb_ms") or item.get("stt_ms"))
            tts = as_float(item.get("tts_ttfb_ms") or item.get("tts_ms"))
            if stt is not None:
                events.append(
                    StageObserved(
                        stage=Stage.STT,
                        metric=Metric.TTFB,
                        value_ms=stt,
                        turn_index=i,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="turns[].stt_ttfb_ms",
                    )
                )
            if tts is not None:
                events.append(
                    StageObserved(
                        stage=Stage.TTS,
                        metric=Metric.TTFB,
                        value_ms=tts,
                        turn_index=i,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="turns[].tts_ttfb_ms",
                    )
                )
        events.append(CallFinalized(reason="cartesia"))
        return events
