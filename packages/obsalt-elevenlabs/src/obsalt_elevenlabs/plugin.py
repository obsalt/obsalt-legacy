from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from obsalt.auth.primitives import (
    constant_time_eq,
    enforce_window,
    hmac_hex,
    parse_kv_multi,
    require_secret,
    singleton_or_reject,
)
from obsalt.domain.enums import (
    Capability,
    GroundingKind,
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
    GroundingObserved,
    NormalizedEvent,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.fieldmap import as_str
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

DECODER_VERSION = "elevenlabs/1"


class ElevenLabsPlugin:
    API_VERSION = 1
    name = "elevenlabs"
    display_name = "ElevenLabs"
    DECODER_VERSION = DECODER_VERSION
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"elevenlabs-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="elevenlabs.post_call_transcription",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.COARSE_ANCHOR}),
        provides=frozenset(
            {Signal.TRANSCRIPT, Signal.TURN_INTERVALS, Signal.GROUNDING_USER_TEXT, Signal.HANGUP_REASON}
        ),
        structurally_absent={
            Signal.STAGE_INTERVALS: "ElevenLabs post-call JSON uses whole-second message anchors, no documented end timestamps",
        },
        schema_source="https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        missing = require_secret(cfg.credentials.get("webhook_secret"), name="webhook_secret")
        if missing:
            return missing
        value, err = singleton_or_reject(headers, b"elevenlabs-signature")
        if err:
            return err
        assert value is not None
        parsed = parse_kv_multi(value.decode("latin-1"))
        timestamps = parsed.get("t") or []
        signatures = parsed.get("v0") or []
        if not timestamps or not signatures:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="expected t={unix},v0={hex}")
        try:
            event_s = int(timestamps[0])
        except ValueError:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="bad timestamp")
        window = enforce_window(event_s * 1000, tolerance=timedelta(minutes=30))
        if not window.ok:
            return window
        expected = hmac_hex(cfg.credentials["webhook_secret"], f"{event_s}.".encode() + raw)
        if not any(constant_time_eq(expected, sig) for sig in signatures):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="elevenlabs hmac mismatch")
        return VerifyResult(outcome=VerifyOutcome.OK, event_time_ms=event_s * 1000)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return ObservationalEventKind.UNKNOWN_OBSERVATIONAL
        if payload.get("type") == "post_call_transcription":
            return ObservationalEventKind.CALL_ENDED
        return ObservationalEventKind.UNKNOWN_OBSERVATIONAL

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        conv = as_str(data.get("conversation_id") or data.get("agent_id"))
        return f"post_call:{conv}" if conv else None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return TombstoneHints()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return TombstoneHints(source_call_id=as_str(data.get("conversation_id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"status":"received"}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        if not envelope.body:
            return []
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        call_id = as_str(data.get("conversation_id"))
        if not call_id:
            return []
        events: list[NormalizedEvent] = [
            CallObserved(
                source_call_id=call_id,
                agent_id=as_str(data.get("agent_id")) or "unknown",
                architecture=PipelineArchitecture.CASCADE,
                source_path="data.conversation_id",
            )
        ]
        user_bits = []
        for i, item in enumerate(data.get("transcript") or []):
            if not isinstance(item, dict):
                continue
            role = as_str(item.get("role")) or "agent"
            speaker = Speaker.USER if role in {"user"} else Speaker.AGENT
            text = as_str(item.get("message") or item.get("text")) or ""
            started = parse_datetime(item.get("time_in_call_secs"))
            events.append(
                TurnObserved(
                    turn_index=i,
                    speaker=speaker,
                    text=text,
                    started_at=started,
                    source_path="data.transcript[].time_in_call_secs",
                )
            )
            if speaker is Speaker.USER:
                user_bits.append(text)
        if user_bits:
            events.append(
                GroundingObserved(kind=GroundingKind.USER_TEXT, content="\n".join(user_bits), source_path="data.transcript")
            )
        events.append(CallFinalized(reason="post_call_transcription"))
        return events

class ElevenLabsOtelMapper:
    API_VERSION = 1
    name = "elevenlabs-otel"
    display_name = "ElevenLabs OTLP-shaped webhook"
    capabilities = frozenset({Capability.OTLP_MAPPER})
    manifest = PluginManifest()
    fidelity = FidelityDeclaration(
        source_format="elevenlabs.otlp_webhook",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL}),
        provides=frozenset({Signal.STAGE_INTERVALS, Signal.E2E_DURATION}),
        structurally_absent={},
        schema_source="https://elevenlabs.io/docs",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def claims(self, span: Any) -> int:
        name = getattr(span, "name", "") or ""
        attrs = getattr(span, "attributes", {}) or {}
        if str(attrs.get("elevenlabs.conversation_id") or "") or name.startswith("elevenlabs."):
            return 40
        return 0

    def decode(self, spans: Sequence[Any]) -> Iterable[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for span in spans:
            start = getattr(span, "start_time", None)
            end = getattr(span, "end_time", None)
            if start is None or end is None:
                continue
            events.append(
                StageObserved(
                    stage=Stage.E2E,
                    metric=Metric.DURATION,
                    value_ms=(end - start) / 1_000_000.0 if end > 10_000_000 else (end - start) * 1000.0,
                    placement=MeasurementPlacement.INTERVAL,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="elevenlabs.span.start_time",
                )
            )
        return events
