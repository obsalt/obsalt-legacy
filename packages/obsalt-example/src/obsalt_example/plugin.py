from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date

from obsalt.auth.primitives import constant_time_eq, require_secret, singleton_or_reject
from obsalt.domain.enums import (
    Capability,
    CallDirection,
    GroundingKind,
    HangupParty,
    HangupReason,
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
    OutcomeObserved,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.fieldmap import FieldMap, Ts, as_str
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

DECODER_VERSION = "example/1"

MAP = FieldMap(
    {
        "source_call_id": "call_id",
        "agent_id": "agent_id",
        "started_at": Ts("started_at"),
        "ended_at": Ts("ended_at"),
    }
)


class ExamplePlugin:
    API_VERSION = 1
    name = "example"
    display_name = "Example"
    DECODER_VERSION = DECODER_VERSION
    capabilities = frozenset(
        {Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION, Capability.STREAM_SOURCE}
    )
    singleton_headers = frozenset({b"x-example-secret"})
    manifest = PluginManifest(secret_fields=frozenset({"shared_secret"}))
    fidelity = FidelityDeclaration(
        source_format="example.post_call.v1",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.INTERVAL, MeasurementPlacement.UNPLACED}),
        provides=frozenset(
            {
                Signal.TRANSCRIPT,
                Signal.TURN_INTERVALS,
                Signal.STAGE_INTERVALS,
                Signal.E2E_DURATION,
                Signal.HANGUP_REASON,
                Signal.GROUNDING_SYSTEM_PROMPT,
                Signal.GROUNDING_USER_TEXT,
            }
        ),
        structurally_absent={
            Signal.TRANSPORT: "example payloads have no transport spans",
        },
        schema_source="https://obsalt.dev/example/schema/v1",
        schema_revision="example-1",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        missing = require_secret(cfg.credentials.get("shared_secret"), name="shared_secret")
        if missing:
            return missing
        value, err = singleton_or_reject(headers, b"x-example-secret")
        if err:
            return err
        assert value is not None
        if not constant_time_eq(value.decode("latin-1"), cfg.credentials["shared_secret"]):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="shared secret mismatch")
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return ObservationalEventKind.UNKNOWN_OBSERVATIONAL
        event = str(payload.get("event") or "")
        if event == "assistant-request":
            return ObservationalEventKind.REJECTED_SYNCHRONOUS
        if event in {"call.completed", "call_ended"}:
            return ObservationalEventKind.CALL_ENDED
        return ObservationalEventKind.UNKNOWN_OBSERVATIONAL

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        call_id = as_str(payload.get("call_id"))
        event = as_str(payload.get("event"))
        if call_id and event:
            return f"{event}:{call_id}"
        return None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return TombstoneHints()
        return TombstoneHints(source_call_id=as_str(payload.get("call_id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        if not envelope.body:
            return []
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("event") not in {"call.completed", "call_ended"}:
            return []
        extracted = MAP.extract(payload)
        source_call_id = as_str(extracted.get("source_call_id"))
        if not source_call_id:
            return []
        events: list[NormalizedEvent] = [
            CallObserved(
                source_call_id=source_call_id,
                agent_id=as_str(extracted.get("agent_id")) or "unknown",
                direction=CallDirection.INBOUND,
                started_at=extracted.get("started_at"),
                architecture=PipelineArchitecture.CASCADE,
                source_path="call_id",
            )
        ]
        prompt = as_str(payload.get("system_prompt"))
        if prompt:
            events.append(
                GroundingObserved(
                    kind=GroundingKind.SYSTEM_PROMPT,
                    content=prompt,
                    source_path="system_prompt",
                )
            )
        user_bits: list[str] = []
        for item in payload.get("turns") or []:
            if not isinstance(item, dict):
                continue
            speaker = Speaker(item.get("speaker") or "unknown") if item.get("speaker") in {s.value for s in Speaker} else Speaker.UNKNOWN
            started = parse_datetime(item.get("started_at"))
            ended = parse_datetime(item.get("ended_at"))
            text = as_str(item.get("text")) or ""
            events.append(
                TurnObserved(
                    turn_index=int(item.get("index") or 0),
                    speaker=speaker,
                    text=text,
                    started_at=started,
                    ended_at=ended,
                    source_path="turns[].text",
                )
            )
            if speaker is Speaker.USER and text:
                user_bits.append(text)
            if started and ended:
                events.append(
                    StageObserved(
                        stage=Stage.E2E,
                        metric=Metric.DURATION,
                        value_ms=(ended - started).total_seconds() * 1000.0,
                        turn_index=int(item.get("index") or 0),
                        placement=MeasurementPlacement.INTERVAL,
                        started_at=started,
                        ended_at=ended,
                        provenance=Provenance.OBSALT_DERIVED,
                        source_path="turns[].started_at",
                        derivation="turn_gap(started_at, ended_at)",
                    )
                )
        if user_bits:
            events.append(
                GroundingObserved(
                    kind=GroundingKind.USER_TEXT,
                    content="\n".join(user_bits),
                    source_path="turns[user].text",
                )
            )
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
        code = as_str(outcome.get("code")) or "completed"
        events.append(
            OutcomeObserved(
                provider_code=code,
                reason=HangupReason.USER_HANGUP if code == "user_hangup" else HangupReason.COMPLETED,
                party=HangupParty.USER if code == "user_hangup" else HangupParty.AGENT,
                ended_at=extracted.get("ended_at"),
                source_path="outcome.code",
            )
        )
        events.append(CallFinalized(reason="provider"))
        return events

    async def frames(self, cfg: ConnectionConfig):
        from obsalt_example.stream import ExampleStreamSource

        async for envelope in ExampleStreamSource().frames(cfg):
            yield envelope
