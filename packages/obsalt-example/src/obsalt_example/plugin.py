from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from datetime import date

from obsalt.crypto.primitives import constant_time_eq, header_values, hmac_hex
from obsalt.domain.enums import (
    CallDirection,
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
    OutcomeObserved,
    SnapshotBoundaryObserved,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.plugin import PLUGIN_API_VERSION
from obsalt.plugin.types import (
    BackfillCursor,
    BackfillItem,
    BackfillPage,
    ConnectionConfig,
    PluginManifest,
    RawEnvelope,
    TombstoneHints,
    VerifyResult,
    WebhookResponse,
)


class ExamplePlugin:
    """Minimal webhook source used by tests and as a plugin template."""

    API_VERSION = PLUGIN_API_VERSION
    name = "example"
    display_name = "Example"
    decoder_version = "example/1"
    capabilities = frozenset(
        {
            Capability.WEBHOOK_SOURCE,
            Capability.AUTHENTICATION,
            Capability.STREAM_SOURCE,
            Capability.REST_BACKFILL,
        }
    )
    singleton_headers = frozenset({b"x-obsalt-example-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"hmac_secret"}))
    fidelity = FidelityDeclaration(
        source_format="example.v1",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.UNPLACED}),
        provides=frozenset(
            {
                Signal.TRANSCRIPT,
                Signal.TURN_INTERVAL,
                Signal.STT_DURATION,
                Signal.GROUNDING_PROMPT,
                Signal.GROUNDING_USER,
                Signal.HANGUP,
            }
        ),
        structurally_absent={
            Signal.VAD: "example payloads have no VAD clock",
            Signal.STAGE_INTERVAL: "example payloads publish stt_ms durations without stage clocks",
        },
        schema_source="obsalt-example/fixtures/schema/example.schema.json",
        schema_revision="1",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(
        self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig
    ) -> VerifyResult:
        secret = cfg.secrets.get("hmac_secret")
        if not secret:
            return VerifyResult(
                outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="hmac_secret required"
            )
        values = header_values(headers, "x-obsalt-example-signature")
        if not values:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing signature header")
        expected = hmac_hex(secret, raw)
        if not constant_time_eq(values[0], expected):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        payload = _json(raw)
        if payload.get("type") == "assistant-request":
            return ObservationalEventKind.REJECTED_SYNCHRONOUS
        if payload.get("final") is True:
            return ObservationalEventKind.CALL_ENDED
        return ObservationalEventKind.SNAPSHOT

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        payload = _json(raw)
        return payload.get("delivery_id") or payload.get("call_id")

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        payload = _json(raw)
        event_time = _ts(payload.get("timestamp") or payload.get("started_at"))
        if event_time is None:
            turns = payload.get("turns") or []
            if turns:
                event_time = _ts(turns[0].get("started_at"))
        return TombstoneHints(source_call_id=payload.get("call_id"), event_time=event_time)

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        payload = _json(envelope.body or b"{}")
        if payload.get("type") == "definitely-not-a-real-event":
            return
        call_id = payload.get("call_id")
        if not call_id:
            return
        yield CallObserved(
            source_call_id=str(call_id),
            agent_id=payload.get("agent_id") or "example-agent",
            direction=CallDirection.INBOUND,
            architecture=PipelineArchitecture.CASCADE,
            provenance_by_field={
                "source_call_id": ProvenanceStamp(
                    provenance=Provenance.PROVIDER_REPORTED, source_path="call_id"
                )
            },
        )
        # Full-call snapshots may retract omitted facts in the declared domains.
        # stt_ms is a duration without a stage clock — never an INTERVAL (T1).
        if payload.get("type") != "assistant-request":
            yield SnapshotBoundaryObserved(
                authoritative_domains=[
                    "turn_observed",
                    "stage_observed",
                    "outcome_observed",
                    "grounding_observed",
                ]
            )
        prompt = payload.get("system_prompt")
        if prompt:
            yield GroundingObserved(
                kind=GroundingKind.SYSTEM_PROMPT,
                content=str(prompt),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="system_prompt",
            )
        user_texts: list[str] = []
        for index, turn in enumerate(payload.get("turns") or []):
            speaker = (
                Speaker(turn.get("speaker") or "user")
                if turn.get("speaker") in {"user", "agent"}
                else Speaker.USER
            )
            text = turn.get("text") or ""
            yield TurnObserved(
                turn_index=index,
                speaker=speaker,
                text=text,
                started_at=_ts(turn.get("started_at")),
                ended_at=_ts(turn.get("ended_at")),
            )
            if speaker is Speaker.USER and text:
                user_texts.append(str(text))
            stt = turn.get("stt_ms")
            if stt is not None:
                yield StageObserved(
                    stage=Stage.STT,
                    metric=Metric.DURATION,
                    value_ms=float(stt),
                    turn_index=index,
                    placement=MeasurementPlacement.UNPLACED,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"turns[{index}].stt_ms",
                )
        if user_texts:
            yield GroundingObserved(
                kind=GroundingKind.USER_TEXT,
                content="\n".join(user_texts),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="turns[speaker=user].text",
            )
        if payload.get("ended_reason"):
            yield OutcomeObserved(provider_code=str(payload["ended_reason"]))
        if payload.get("final") is True:
            yield CallFinalized(reason="provider")

    def scan(self, cfg: ConnectionConfig, cursor: BackfillCursor) -> BackfillPage:
        """Example RestBackfill. Identity is (connection, upstream_entity_id, content_hash)."""
        raw_items = cfg.settings.get("backfill_items") or []
        items = []
        for row in raw_items:
            if not isinstance(row, dict):
                continue
            entity = str(row.get("id") or row.get("upstream_entity_id") or "")
            if not entity:
                continue
            items.append(
                BackfillItem(
                    upstream_entity_id=entity,
                    content_hash=str(row.get("hash") or row.get("content_hash") or "x"),
                    payload=row,
                )
            )
        return BackfillPage(items=items, next_cursor=None, truncated_by_retention=False)

    def hydrate(self, cfg: ConnectionConfig, item: BackfillItem) -> RawEnvelope:
        from obsalt.util import sha256_bytes, utcnow

        payload = item.payload if isinstance(item.payload, dict) else {}
        body = json.dumps(
            {
                "call_id": item.upstream_entity_id,
                "final": True,
                "ended_reason": payload.get("ended_reason") or "completed",
            }
        ).encode()
        return RawEnvelope(
            envelope_id=item.upstream_entity_id,
            org_id=cfg.org_id,
            provider=self.name,
            connection_id=cfg.connection_id,
            object_key=f"org/{cfg.org_id}/backfill/{item.upstream_entity_id}",
            delivery_key=f"{cfg.connection_id}:{item.upstream_entity_id}:{item.content_hash or 'x'}",
            content_sha256=sha256_bytes(body),
            body=body,
            received_at=utcnow(),
        )

    async def frames(self, cfg: ConnectionConfig) -> AsyncIterator[RawEnvelope]:
        """Example StreamSource. Deepgram is additive against this contract."""
        if not cfg.settings.get("emit_example_frame"):
            return
        from obsalt.util import sha256_bytes, utcnow

        body = b'{"call_id":"stream-1","final":true,"ended_reason":"completed"}'
        yield RawEnvelope(
            envelope_id="example-stream-1",
            org_id=cfg.org_id,
            provider=self.name,
            connection_id=cfg.connection_id,
            object_key=f"org/{cfg.org_id}/stream/example-stream-1",
            delivery_key="example-stream-1",
            content_sha256=sha256_bytes(body),
            body=body,
            received_at=utcnow(),
        )


def _json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ts(value):
    from obsalt.util import parse_datetime

    return parse_datetime(value)
