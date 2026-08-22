from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

from obsalt._version import PLUGIN_API_VERSION
from obsalt.crypto.primitives import constant_time_eq, header_values
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
from obsalt.domain.events import (
    CallFinalized,
    CallObserved,
    NormalizedEvent,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.plugin.types import (
    ConnectionConfig,
    PluginManifest,
    RawEnvelope,
    TombstoneHints,
    VerifyResult,
    WebhookResponse,
)
from obsalt.util import as_float, as_str, parse_datetime


class CartesiaPlugin:
    """Cartesia Line: real turn intervals + unplaced STT/TTS TTFBs. Shared secret is the weakest committed scheme."""

    API_VERSION = PLUGIN_API_VERSION
    name = "cartesia"
    display_name = "Cartesia Line"
    decoder_version = "cartesia/1"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-webhook-secret"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="cartesia.line.webhook",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL, MeasurementPlacement.UNPLACED}),
        provides=frozenset({Signal.TURN_INTERVAL, Signal.STT_DURATION, Signal.TTS_TTFB, Signal.TRANSCRIPT}),
        structurally_absent={Signal.STAGE_INTERVAL: "Cartesia Line reports unplaced STT/TTS TTFBs, not stage intervals"},
        schema_source="https://docs.cartesia.ai (Line webhooks, x-webhook-secret)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        secret = cfg.secrets.get("webhook_secret")
        if not secret:
            return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="webhook_secret required")
        values = header_values(headers, "x-webhook-secret")
        if not values:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing x-webhook-secret")
        if not constant_time_eq(values[0], secret):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        return ObservationalEventKind.CALL_ENDED

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        payload = _json(raw)
        return as_str(payload.get("call_id") or payload.get("id"))

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        payload = _json(raw)
        return TombstoneHints(source_call_id=as_str(payload.get("call_id") or payload.get("id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        payload = _json(envelope.body or b"{}")
        call_id = as_str(payload.get("call_id") or payload.get("id"))
        if not call_id:
            return
        yield CallObserved(
            source_call_id=call_id,
            agent_id=as_str(payload.get("agent_id")) or "unknown",
            started_at=parse_datetime(payload.get("started_at")),
            ended_at=parse_datetime(payload.get("ended_at")),
            provenance_by_field={"source_call_id": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="call_id")},
        )
        for index, turn in enumerate(payload.get("turns") or []):
            if not isinstance(turn, dict):
                continue
            started = parse_datetime(turn.get("started_at"))
            ended = parse_datetime(turn.get("ended_at"))
            yield TurnObserved(
                turn_index=index,
                speaker=Speaker.USER if turn.get("speaker") == "user" else Speaker.AGENT,
                text=as_str(turn.get("text")) or "",
                started_at=started,
                ended_at=ended,
                provenance_by_field={
                    "started_at": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED, source_path=f"turns[{index}].started_at"
                    ),
                    "ended_at": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED, source_path=f"turns[{index}].ended_at"
                    ),
                },
            )
            stt_field = "stt_ttfb_ms" if turn.get("stt_ttfb_ms") is not None else ("stt_ms" if turn.get("stt_ms") is not None else None)
            tts_field = "tts_ttfb_ms" if turn.get("tts_ttfb_ms") is not None else ("tts_ms" if turn.get("tts_ms") is not None else None)
            if stt_field is not None:
                stt = as_float(turn.get(stt_field))
                if stt is not None:
                    yield StageObserved(
                        stage=Stage.STT,
                        metric=Metric.TTFB if "ttfb" in stt_field else Metric.DURATION,
                        value_ms=stt,
                        turn_index=index,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"turns[{index}].{stt_field}",
                    )
            if tts_field is not None:
                tts = as_float(turn.get(tts_field))
                if tts is not None:
                    yield StageObserved(
                        stage=Stage.TTS,
                        metric=Metric.TTFB if "ttfb" in tts_field else Metric.DURATION,
                        value_ms=tts,
                        turn_index=index,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"turns[{index}].{tts_field}",
                    )
        yield CallFinalized(reason="provider")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
