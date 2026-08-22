from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

from obsalt._version import PLUGIN_API_VERSION
from obsalt.crypto.primitives import (
    constant_time_eq,
    enforce_window,
    header_values,
    hmac_hex,
    parse_kv_header,
)
from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    ObservationalEventKind,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    VerifyOutcome,
)
from obsalt.domain.events import (
    CallFinalized,
    CallObserved,
    NormalizedEvent,
    OutcomeObserved,
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
from obsalt.util import as_str, parse_datetime


class ElevenLabsPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "elevenlabs"
    display_name = "ElevenLabs"
    decoder_version = "elevenlabs/1"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION, Capability.OTLP_MAPPER})
    singleton_headers = frozenset({b"elevenlabs-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="elevenlabs.post_call_transcription",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.COARSE_ANCHOR}),
        provides=frozenset({Signal.TRANSCRIPT, Signal.TURN_INTERVAL, Signal.HANGUP}),
        structurally_absent={
            Signal.STAGE_INTERVAL: "post-call JSON uses whole-second message anchors without documented end timestamps",
        },
        schema_source="https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        secret = cfg.secrets.get("webhook_secret")
        if not secret:
            return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="webhook_secret required")
        values = header_values(headers, "elevenlabs-signature")
        if not values:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing ElevenLabs-Signature")
        parsed = parse_kv_header(values[0])
        ts = parsed.get("t")
        # header may contain multiple v0= values; parse_kv_header keeps the last
        sigs = [part.split("=", 1)[1] for part in values[0].split(",") if part.strip().startswith("v0=")]
        if not ts or not sigs:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="expected t={unix},v0={hex}")
        window = enforce_window(float(ts), tolerance_seconds=30 * 60, unit="s", one_sided=False)
        if window is not None:
            return window
        expected = hmac_hex(secret, f"{ts}.".encode() + raw)
        if not any(constant_time_eq(sig, expected) for sig in sigs):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        payload = _json(raw)
        typ = as_str(payload.get("type")) or ""
        if typ in {"post_call_transcription", "post_call_audio"}:
            return ObservationalEventKind.CALL_ENDED
        return ObservationalEventKind.UNKNOWN_OBSERVATIONAL

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        payload = _json(raw)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        conv = as_str(data.get("conversation_id") or data.get("agent_id"))
        return conv

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        payload = _json(raw)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return TombstoneHints(source_call_id=as_str(data.get("conversation_id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b"")

    def claims(self, span) -> int:
        attrs = getattr(span, "attributes", {}) or {}
        if any(str(k).startswith("elevenlabs.") for k in attrs):
            return 40
        return 0

    def decode_spans(self, spans):
        return []

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        payload = _json(envelope.body or b"{}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        conv = as_str(data.get("conversation_id"))
        if not conv:
            return
        yield CallObserved(
            source_call_id=conv,
            agent_id=as_str(data.get("agent_id")) or "unknown",
            provenance_by_field={"source_call_id": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="data.conversation_id")},
        )
        transcript = data.get("transcript") or []
        if isinstance(transcript, list):
            for index, item in enumerate(transcript):
                if not isinstance(item, dict):
                    continue
                role = as_str(item.get("role")) or "agent"
                speaker = Speaker.USER if role in {"user", "customer"} else Speaker.AGENT
                # whole-second message anchors
                started = parse_datetime(item.get("time_in_call_secs"))
                yield TurnObserved(
                    turn_index=index,
                    speaker=speaker,
                    text=as_str(item.get("message")) or "",
                    started_at=started,
                )
        if data.get("status") or data.get("termination_reason"):
            yield OutcomeObserved(provider_code=as_str(data.get("termination_reason") or data.get("status") or "completed"))
            yield CallFinalized(reason="provider")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
