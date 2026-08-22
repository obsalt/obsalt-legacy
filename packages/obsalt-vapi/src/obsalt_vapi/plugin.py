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
    CallDirection,
    Capability,
    EvidenceKind,
    GroundingKind,
    MeasurementPlacement,
    Metric,
    ObservationalEventKind,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
    Statistic,
    ToolStatus,
    VerifyOutcome,
    speaker_from,
)
from obsalt.domain.events import (
    AggregateObserved,
    CallFinalized,
    CallObserved,
    EvidenceObserved,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    SnapshotBoundaryObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.models import FidelityDeclaration, ProvenanceStamp
from obsalt.plugin.fieldmap import FieldMap, Ts
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
from obsalt.util import as_float, as_str, dig, parse_datetime, sha256_bytes

_SYNCHRONOUS = {
    "assistant-request",
    "assistant.started",  # not used as sync but listed for safety if misconfigured
    "tool-calls",
    "function-call",
    "transfer-destination-request",
    "knowledge-base-request",
    "knowledge-base",
}

MAP = FieldMap(
    {
        "source_call_id": ["message.call.id", "call.id", "message.callId", "call_id"],
        "agent_id": ["message.call.assistantId", "message.assistant.id", "call.assistantId"],
        "started_at": Ts("message.startedAt"),
        "ended_at": Ts("message.endedAt"),
        "cost": "message.cost",
    }
)


class VapiPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "vapi"
    display_name = "Vapi"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION, Capability.REST_BACKFILL})
    singleton_headers = frozenset(
        {b"x-vapi-secret", b"authorization", b"x-vapi-signature", b"x-vapi-timestamp"}
    )
    decoder_version = "vapi/3"
    manifest = PluginManifest(
        secret_fields=frozenset({"legacy_secret", "bearer_token", "hmac_secret", "oauth_token", "api_key"}),
        config_schema={
            "type": "object",
            "properties": {
                "auth_mode": {"enum": ["legacy_secret", "bearer", "hmac", "oauth2"]},
                "hmac_algorithm": {"type": "string"},
                "hmac_header": {"type": "string"},
                "timestamp_header": {"type": "string"},
            },
        },
    )
    fidelity = FidelityDeclaration(
        source_format="vapi.server-message",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(
            {MeasurementPlacement.UNPLACED, MeasurementPlacement.ANCHORED_DURATION, MeasurementPlacement.COARSE_ANCHOR}
        ),
        provides=frozenset(
            {
                Signal.STT_DURATION,
                Signal.LLM_TTFT,
                Signal.TTS_DURATION,
                Signal.E2E_DURATION,
                Signal.TTFA,
                Signal.ENDPOINTING,
                Signal.INTERRUPTION_COUNT,
                Signal.TURN_INTERVAL,
                Signal.TRANSCRIPT,
                Signal.GROUNDING_PROMPT,
                Signal.GROUNDING_TOOLS,
                Signal.GROUNDING_USER,
                Signal.TOOL_RESULT,
                Signal.RECORDING,
                Signal.COST,
                Signal.HANGUP,
                Signal.STT_CONFIDENCE,
                Signal.TRANSPORT,
            }
        ),
        structurally_absent={
            Signal.STAGE_INTERVAL: "Vapi publishes per-turn stage durations without stage timestamps",
            Signal.VAD: "Vapi does not publish VAD start/stop clocks on the webhook",
            Signal.TOOL_TIMING: "tool messages have a time field but not a measured invocation duration",
        },
        schema_source="https://docs.vapi.ai (PerformanceMetrics, TurnLatency, ServerMessage)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        mode = (cfg.settings.get("auth_mode") or "legacy_secret").lower()
        if mode == "legacy_secret":
            secret = cfg.secrets.get("legacy_secret") or cfg.secrets.get("secret")
            if not secret:
                return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="legacy_secret required")
            values = header_values(headers, "x-vapi-secret")
            if not values:
                return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing X-Vapi-Secret")
            if not constant_time_eq(values[0], secret):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
            return VerifyResult(outcome=VerifyOutcome.OK)
        if mode == "bearer":
            token = cfg.secrets.get("bearer_token")
            if not token:
                return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="bearer_token required")
            values = header_values(headers, "authorization")
            if not values:
                return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing Authorization")
            provided = values[0]
            if provided.lower().startswith("bearer "):
                provided = provided[7:]
            if not constant_time_eq(provided, token):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
            return VerifyResult(outcome=VerifyOutcome.OK)
        if mode == "oauth2":
            token = cfg.secrets.get("oauth_token")
            if not token:
                return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="oauth_token required")
            values = header_values(headers, "authorization")
            if not values or not values[0].lower().startswith("bearer "):
                return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing bearer token")
            if not constant_time_eq(values[0][7:], token):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
            return VerifyResult(outcome=VerifyOutcome.OK)
        if mode == "hmac":
            secret = cfg.secrets.get("hmac_secret")
            if not secret:
                return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="hmac_secret required")
            header_name = cfg.settings.get("hmac_header") or "x-vapi-signature"
            algo = cfg.settings.get("hmac_algorithm") or "sha256"
            values = header_values(headers, header_name)
            if not values:
                return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail=f"missing {header_name}")
            ts_header = cfg.settings.get("timestamp_header")
            message = raw
            if ts_header:
                ts_values = header_values(headers, ts_header)
                if not ts_values:
                    return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail=f"missing {ts_header}")
                window = enforce_window(float(ts_values[0]), tolerance_seconds=300, unit="s")
                if window is not None:
                    return window
                message = ts_values[0].encode("utf-8") + raw
            expected = hmac_hex(secret, message, digestmod=algo)
            provided = values[0]
            kv = parse_kv_header(provided)
            if "d" in kv:
                provided = kv["d"]
            if not constant_time_eq(provided, expected):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
            return VerifyResult(outcome=VerifyOutcome.OK)
        return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail=f"unknown auth_mode {mode}")

    def classify(self, raw: bytes) -> ObservationalEventKind:
        payload = _json(raw)
        message = _unwrap(payload)
        event_type = as_str(message.get("type")) or ""
        if event_type in _SYNCHRONOUS or event_type.startswith("assistant-request"):
            return ObservationalEventKind.REJECTED_SYNCHRONOUS
        if event_type == "end-of-call-report":
            return ObservationalEventKind.CALL_ENDED
        if event_type in {"transcript", 'transcript[transcriptType="final"]'}:
            return ObservationalEventKind.TRANSCRIPT
        if event_type == "user-interrupted":
            return ObservationalEventKind.INTERRUPTION
        if event_type == "status-update":
            return ObservationalEventKind.STATUS
        return ObservationalEventKind.UNKNOWN_OBSERVATIONAL

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        ids = header_values(headers, "x-request-id")
        if ids:
            return ids[0]
        payload = _json(raw)
        message = _unwrap(payload)
        call_id = as_str(dig(message, "call", "id")) or as_str(message.get("callId"))
        event_type = as_str(message.get("type")) or "unknown"
        if call_id:
            return f"{call_id}:{event_type}:{sha256_bytes(raw)[:16]}"
        return sha256_bytes(raw)

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        payload = _json(raw)
        message = _unwrap(payload)
        call_id = as_str(dig(message, "call", "id")) or as_str(message.get("callId"))
        return TombstoneHints(source_call_id=call_id, event_time=parse_datetime(message.get("timestamp") or message.get("startedAt")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        payload = _json(envelope.body or b"{}")
        message = _unwrap(payload)
        event_type = as_str(message.get("type")) or "unknown"
        if event_type in _SYNCHRONOUS:
            return
        mapped = MAP.apply(payload if "message" in payload else {"message": message})
        source_call_id = as_str(mapped.get("source_call_id"))
        if not source_call_id:
            return
        call_obj = message.get("call") if isinstance(message.get("call"), dict) else {}
        assistant = message.get("assistant") if isinstance(message.get("assistant"), dict) else {}
        direction = CallDirection.UNKNOWN
        call_type = as_str(call_obj.get("type")) or ""
        if "inbound" in call_type.lower():
            direction = CallDirection.INBOUND
        elif "outbound" in call_type.lower():
            direction = CallDirection.OUTBOUND
        yield CallObserved(
            source_call_id=source_call_id,
            agent_id=as_str(mapped.get("agent_id")) or "unknown",
            direction=direction,
            started_at=mapped.get("started_at") or parse_datetime(call_obj.get("startedAt")),
            ended_at=mapped.get("ended_at") or parse_datetime(call_obj.get("endedAt")),
            cost=as_float(mapped.get("cost")),
            architecture=PipelineArchitecture.CASCADE,
            provenance_by_field={
                "source_call_id": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="message.call.id"),
                "started_at": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="message.startedAt"),
            },
        )
        yield SnapshotBoundaryObserved(authoritative_domains=["turn_observed", "stage_observed", "tool_observed", "outcome_observed"])

        prompt_messages = dig(assistant, "model", "messages") or []
        if isinstance(prompt_messages, list):
            prompts = [
                as_str(m.get("content"))
                for m in prompt_messages
                if isinstance(m, dict) and m.get("role") == "system" and as_str(m.get("content"))
            ]
            if prompts:
                yield GroundingObserved(
                    kind=GroundingKind.SYSTEM_PROMPT,
                    content="\n".join(p for p in prompts if p),
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="message.assistant.model.messages",
                )

        artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
        recording = (
            as_str(dig(artifact, "recording", "stereoUrl"))
            or as_str(dig(artifact, "recording", "monoUrl"))
            or as_str(artifact.get("recordingUrl"))
            or as_str(artifact.get("stereoRecordingUrl"))
        )
        if recording:
            yield EvidenceObserved(
                kind=EvidenceKind.RECORDING,
                uri=recording,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="message.artifact.recordingUrl",
            )
        transcript = as_str(artifact.get("transcript"))
        if transcript:
            yield EvidenceObserved(
                kind=EvidenceKind.TRANSCRIPT,
                uri="inline",
                metadata={"text": transcript},
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="message.artifact.transcript",
            )

        messages = artifact.get("messages") or message.get("messages") or []
        if isinstance(messages, list):
            yield from _turns_and_tools(messages)

        yield from _latency(message, artifact)
        ended_reason = as_str(message.get("endedReason")) or as_str(call_obj.get("endedReason"))
        if ended_reason:
            yield OutcomeObserved(
                provider_code=ended_reason,
                ended_at=parse_datetime(message.get("endedAt")),
                cost=as_float(message.get("cost")),
                provenance_by_field={
                    "provider_code": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED, source_path="message.endedReason"
                    )
                },
            )
        if event_type == "end-of-call-report" or ended_reason:
            yield CallFinalized(reason="provider")

    def scan(self, cfg: ConnectionConfig, cursor: BackfillCursor) -> BackfillPage:
        """List Vapi calls. Without an API key this is an empty, retention-truncated page."""
        api_key = cfg.secrets.get("api_key")
        if not api_key:
            return BackfillPage(items=[], next_cursor=None, truncated_by_retention=True)
        try:
            import httpx
            from obsalt.egress import validate_destination

            url = "https://api.vapi.ai/call"
            validate_destination(url)
            params: dict[str, str] = {}
            if cursor.token:
                params["cursor"] = cursor.token
            response = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, params=params, timeout=20.0)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return BackfillPage(items=[], next_cursor=None, truncated_by_retention=True)
        rows = payload if isinstance(payload, list) else payload.get("data") or payload.get("calls") or []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entity = as_str(row.get("id")) or ""
            if not entity:
                continue
            items.append(
                BackfillItem(
                    upstream_entity_id=entity,
                    content_hash=sha256_bytes(json.dumps(row, sort_keys=True).encode()),
                    upstream_revision=as_str(row.get("updatedAt") or row.get("endedAt")),
                    payload=row,
                )
            )
        next_token = None
        if isinstance(payload, dict):
            next_token = as_str((payload.get("metadata") or {}).get("nextCursor") or payload.get("nextCursor"))
        return BackfillPage(
            items=items,
            next_cursor=BackfillCursor(token=next_token) if next_token else None,
        )

    def hydrate(self, cfg: ConnectionConfig, item: BackfillItem) -> RawEnvelope:
        from obsalt.util import utcnow

        body = json.dumps({"message": {"type": "end-of-call-report", **item.payload}}).encode()
        digest = sha256_bytes(body)
        return RawEnvelope(
            envelope_id=item.upstream_entity_id,
            org_id=cfg.org_id,
            provider=self.name,
            connection_id=cfg.connection_id,
            object_key=f"org/{cfg.org_id}/backfill/{item.upstream_entity_id}",
            delivery_key=f"{cfg.connection_id}:{item.upstream_entity_id}:{item.content_hash or digest}",
            content_sha256=digest,
            body=body,
            received_at=utcnow(),
        )


def _turns_and_tools(messages: list) -> Iterable[NormalizedEvent]:
    turn_index = 0
    pending: dict[str, str] = {}
    user_texts: list[str] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = as_str(raw.get("role")) or ""
        if role in {"tool_calls", "tool_call", "function"} or raw.get("toolCalls") or raw.get("toolCallList"):
            for item in raw.get("toolCalls") or raw.get("toolCallList") or []:
                if not isinstance(item, dict):
                    continue
                inner = item.get("function") if isinstance(item.get("function"), dict) else item
                name = as_str(inner.get("name")) or "unknown"
                args = inner.get("arguments") or inner.get("parameters") or {}
                tool_id = as_str(item.get("id")) or name
                pending[tool_id] = name
                yield ToolObserved(
                    tool_id=tool_id,
                    name=name,
                    turn_index=turn_index,
                    started_at=parse_datetime(raw.get("time")),
                    status=ToolStatus.PENDING,
                    args=args,
                    provenance_by_field={
                        "name": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="artifact.messages.toolCalls")
                    },
                )
            continue
        if role in {"tool_call_result", "tool", "function_call_result"} or raw.get("toolCallId"):
            result_id = as_str(raw.get("toolCallId")) or as_str(dig(raw, "toolCallResult", "toolCallId")) or "tool"
            result = raw.get("result") or raw.get("content")
            error = as_str(raw.get("error"))
            status = ToolStatus.ERROR if error else ToolStatus.SUCCESS
            yield ToolObserved(
                tool_id=result_id,
                name=pending.get(result_id, "unknown"),
                ended_at=parse_datetime(raw.get("time")),
                status=status,
                result=result,
                error=error,
            )
            if result is not None:
                yield GroundingObserved(
                    kind=GroundingKind.TOOL_RESULT,
                    content=result if isinstance(result, str) else json.dumps(result),
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="artifact.messages.tool_call_result.result",
                )
            continue
        speaker = speaker_from(role)
        if speaker is Speaker.SYSTEM:
            continue
        text = as_str(raw.get("message")) or as_str(raw.get("content")) or ""
        duration_s = as_float(raw.get("duration"))
        started = parse_datetime(raw.get("time"))
        ended = None
        if started is not None and duration_s is not None:
            from datetime import timedelta

            ended = started + timedelta(seconds=duration_s)
        conf = None
        words = raw.get("words") if isinstance(raw.get("words"), list) else []
        if words:
            confs = [as_float(w.get("confidence")) for w in words if isinstance(w, dict) and as_float(w.get("confidence")) is not None]
            if confs:
                conf = sum(confs) / len(confs)
        yield TurnObserved(
            turn_index=turn_index,
            speaker=speaker,
            text=text,
            started_at=started,
            ended_at=ended,
            confidence=conf,
            interrupted=bool(raw.get("interrupted")),
            provenance_by_field={
                "started_at": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="artifact.messages[].time"),
                "text": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="artifact.messages[].message"),
            },
        )
        if speaker is Speaker.USER and text:
            user_texts.append(text)
        turn_index += 1
    if user_texts:
        yield GroundingObserved(
            kind=GroundingKind.USER_TEXT,
            content="\n".join(user_texts),
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="artifact.messages[role=user].message",
        )


def _latency(message: dict, artifact: dict) -> Iterable[NormalizedEvent]:
    perf = message.get("performanceMetrics") or artifact.get("performanceMetrics") or {}
    if not isinstance(perf, dict):
        return
    turns = perf.get("turnLatencies") or []
    if isinstance(turns, list):
        for index, item in enumerate(turns):
            if not isinstance(item, dict):
                continue
            turn_index = int(as_float(item.get("turn")) or index)
            mapping = (
                (Stage.STT, Metric.DURATION, "transcriberLatency", Signal.STT_DURATION),
                (Stage.LLM, Metric.TTFT, "modelLatency", Signal.LLM_TTFT),
                (Stage.TTS, Metric.DURATION, "voiceLatency", Signal.TTS_DURATION),
                (Stage.E2E, Metric.DURATION, "turnLatency", Signal.E2E_DURATION),
                (Stage.ENDPOINTING, Metric.DURATION, "endpointingLatency", Signal.ENDPOINTING),
            )
            for stage, metric, key, _signal in mapping:
                value = as_float(item.get(key))
                if value is None:
                    continue
                yield StageObserved(
                    stage=stage,
                    metric=metric,
                    value_ms=value,
                    turn_index=turn_index,
                    placement=MeasurementPlacement.UNPLACED,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"artifact.performanceMetrics.turnLatencies[{index}].{key}",
                )
    averages = (
        (Stage.STT, "transcriberLatencyAverage"),
        (Stage.LLM, "modelLatencyAverage"),
        (Stage.TTS, "voiceLatencyAverage"),
        (Stage.E2E, "turnLatencyAverage"),
        (Stage.ENDPOINTING, "endpointingLatencyAverage"),
        (Stage.TRANSPORT, "fromTransportLatencyAverage"),
    )
    for stage, key in averages:
        value = as_float(perf.get(key))
        if value is None:
            continue
        yield AggregateObserved(
            stage=stage,
            metric=Metric.DURATION if stage is not Stage.LLM else Metric.TTFT,
            statistic=Statistic.MEAN,
            value_ms=value,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path=f"artifact.performanceMetrics.{key}",
        )
    for field, kind in (("numAssistantInterrupted", "assistant"), ("numUserInterrupted", "user")):
        count = as_float(perf.get(field))
        if count is not None:
            yield InterruptionObserved(count=int(count), kind=kind)


def _unwrap(payload: dict) -> dict:
    if isinstance(payload.get("message"), dict):
        return payload["message"]
    return payload


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
