"""Vapi webhook source. Observational events only.

Latency keys are the published ones: transcriberLatency / modelLatency /
voiceLatency / turnLatency. Stage durations are unplaced. Turn
``secondsFromStart`` + ``duration`` are real turn intervals.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import jwt

from obsalt.auth.primitives import (
    constant_time_eq,
    enforce_window,
    hmac_hex,
    require_secret,
    singleton_or_reject,
)
from obsalt.domain.enums import (
    Capability,
    CallDirection,
    EvidenceKind,
    GroundingKind,
    InterruptionKind,
    MeasurementPlacement,
    Metric,
    ObservationalEventKind,
    PipelineArchitecture,
    Provenance,
    Signal,
    Speaker,
    Stage,
    ToolStatus,
    VerifyOutcome,
    speaker_from,
)
from obsalt.domain.events import (
    CallFinalized,
    CallObserved,
    EvidenceObserved,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.fieldmap import FieldMap, Ts, as_float, as_str
from obsalt.domain.identity import canonical_json, content_hash
from obsalt.domain.time import ms_from_seconds, parse_datetime
from obsalt.plugin.protocol import (
    BackfillCursor,
    BackfillItem,
    BackfillPage,
    ConnectionConfig,
    FidelityDeclaration,
    PluginManifest,
    RawEnvelope,
    TombstoneHints,
    VerifyResult,
    WebhookResponse,
)
from obsalt_vapi.hangup import classify_ended_reason

DECODER_VERSION = "vapi/3"

REJECTED_TYPES = frozenset(
    {
        "assistant-request",
        "tool-calls",
        "function-call",
        "transfer-destination-request",
        "knowledge-base-request",
        "voice-request",
    }
)

OBSERVATIONAL = {
    "end-of-call-report": ObservationalEventKind.CALL_ENDED,
    "status-update": ObservationalEventKind.STATUS,
    "transcript": ObservationalEventKind.TRANSCRIPT,
    "hang": ObservationalEventKind.STATUS,
    "speech-update": ObservationalEventKind.TRANSCRIPT,
    "conversation-update": ObservationalEventKind.CALL_UPDATED,
    "user-interrupted": ObservationalEventKind.STATUS,
}

MAP = FieldMap(
    {
        "source_call_id": ["message.call.id", "message.callId", "call.id"],
        "agent_id": ["message.call.assistantId", "message.assistant.id", "message.call.assistant.id"],
        "started_at": Ts("message.startedAt"),
        "ended_at": Ts("message.endedAt"),
        "cost": "message.cost",
    }
)


class VapiPlugin:
    API_VERSION = 1
    name = "vapi"
    display_name = "Vapi"
    DECODER_VERSION = DECODER_VERSION
    capabilities = frozenset(
        {Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION, Capability.REST_BACKFILL}
    )
    singleton_headers = frozenset(
        {
            b"x-vapi-secret",
            b"authorization",
            b"x-vapi-signature",
        }
    )
    manifest = PluginManifest(
        secret_fields=frozenset({"shared_secret", "hmac_secret", "bearer_token", "oauth_jwks"}),
        documentation_url="https://docs.vapi.ai/server-url/events",
    )
    fidelity = FidelityDeclaration(
        source_format="vapi.server-url.end-of-call-report",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(
            {MeasurementPlacement.UNPLACED, MeasurementPlacement.ANCHORED_DURATION}
        ),
        provides=frozenset(
            {
                Signal.STT_DURATION,
                Signal.LLM_DURATION,
                Signal.TTS_DURATION,
                Signal.E2E_DURATION,
                Signal.ENDPOINTING,
                Signal.INTERRUPTION_COUNT,
                Signal.STT_CONFIDENCE,
                Signal.TURN_INTERVALS,
                Signal.TRANSCRIPT,
                Signal.RECORDING,
                Signal.COST,
                Signal.HANGUP_REASON,
                Signal.GROUNDING_SYSTEM_PROMPT,
                Signal.GROUNDING_TOOL_RESULTS,
                Signal.GROUNDING_USER_TEXT,
                Signal.TOOL_PAYLOAD,
            }
        ),
        structurally_absent={
            Signal.STAGE_INTERVALS: "Vapi reports stage durations without timestamps, so no stage waterfall is drawn",
            Signal.TOOL_TIMING: "Vapi tool messages do not publish invocation duration",
        },
        schema_source="https://api.vapi.ai/api",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        mode = str(cfg.settings.get("auth_mode") or "legacy_secret")
        if mode == "legacy_secret":
            missing = require_secret(cfg.credentials.get("shared_secret"), name="shared_secret")
            if missing:
                return missing
            value, err = singleton_or_reject(headers, b"x-vapi-secret")
            if err:
                return err
            assert value is not None
            if not constant_time_eq(value.decode("latin-1"), cfg.credentials["shared_secret"]):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="X-Vapi-Secret mismatch")
            return VerifyResult(outcome=VerifyOutcome.OK)
        if mode == "bearer":
            missing = require_secret(cfg.credentials.get("bearer_token"), name="bearer_token")
            if missing:
                return missing
            value, err = singleton_or_reject(headers, b"authorization")
            if err:
                return err
            assert value is not None
            provided = value.decode("latin-1")
            if provided.lower().startswith("bearer "):
                provided = provided[7:]
            if not constant_time_eq(provided, cfg.credentials["bearer_token"]):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="bearer mismatch")
            return VerifyResult(outcome=VerifyOutcome.OK)
        if mode == "hmac":
            missing = require_secret(cfg.credentials.get("hmac_secret"), name="hmac_secret")
            if missing:
                return missing
            sig_header = str(cfg.settings.get("signature_header") or "x-vapi-signature").encode()
            algo = str(cfg.settings.get("algorithm") or "sha256")
            value, err = singleton_or_reject(headers, sig_header)
            if err:
                return err
            assert value is not None
            ts_header = cfg.settings.get("timestamp_header")
            message = raw
            event_ms = None
            if ts_header:
                ts_raw, ts_err = singleton_or_reject(headers, str(ts_header).encode())
                if ts_err:
                    return ts_err
                assert ts_raw is not None
                try:
                    event_ms = int(ts_raw.decode("latin-1"))
                except ValueError:
                    return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="bad timestamp")
                window = enforce_window(event_ms if event_ms > 1e12 else event_ms * 1000)
                if not window.ok:
                    return window
                canon = str(cfg.settings.get("canonicalization") or "body")
                if canon == "body.timestamp":
                    message = raw + ts_raw
            expected = hmac_hex(cfg.credentials["hmac_secret"], message, digest=algo)
            provided = value.decode("latin-1")
            if provided.startswith("sha256="):
                provided = provided[7:]
            if not constant_time_eq(expected, provided):
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="hmac mismatch")
            return VerifyResult(outcome=VerifyOutcome.OK, event_time_ms=event_ms)
        if mode == "oauth2":
            missing = require_secret(cfg.credentials.get("oauth_audience"), name="oauth_audience")
            if missing:
                return missing
            value, err = singleton_or_reject(headers, b"authorization")
            if err:
                return err
            assert value is not None
            token = value.decode("latin-1")
            if token.lower().startswith("bearer "):
                token = token[7:]
            try:
                jwt.decode(
                    token,
                    cfg.credentials.get("oauth_jwks") or cfg.credentials.get("oauth_secret") or "",
                    algorithms=["HS256", "RS256"],
                    audience=cfg.credentials["oauth_audience"],
                    options={"verify_signature": bool(cfg.credentials.get("oauth_jwks") or cfg.credentials.get("oauth_secret"))},
                )
            except jwt.PyJWTError as exc:
                return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail=str(exc))
            return VerifyResult(outcome=VerifyOutcome.OK)
        return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail=f"unknown auth_mode {mode}")

    def classify(self, raw: bytes) -> ObservationalEventKind:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return ObservationalEventKind.UNKNOWN_OBSERVATIONAL
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        event_type = as_str(message.get("type")) or ""
        if event_type in REJECTED_TYPES:
            return ObservationalEventKind.REJECTED_SYNCHRONOUS
        return OBSERVATIONAL.get(event_type, ObservationalEventKind.UNKNOWN_OBSERVATIONAL)

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        call = message.get("call") if isinstance(message.get("call"), dict) else {}
        call_id = as_str(call.get("id")) or as_str(message.get("callId"))
        event_type = as_str(message.get("type"))
        if call_id and event_type:
            return f"{event_type}:{call_id}"
        return None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return TombstoneHints()
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        call = message.get("call") if isinstance(message.get("call"), dict) else {}
        return TombstoneHints(source_call_id=as_str(call.get("id")) or as_str(message.get("callId")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"received":true}')

    def scan(self, cfg: ConnectionConfig, cursor: BackfillCursor) -> BackfillPage:
        _ = cfg
        return BackfillPage(items=[], next_cursor=cursor if cursor.token else None)

    def hydrate(self, cfg: ConnectionConfig, item: BackfillItem) -> RawEnvelope:
        _ = cfg
        return RawEnvelope(
            envelope_id=item.upstream_entity_id,
            org_id="",
            provider="vapi",
            connection_id="",
            object_key="",
            body=None,
            delivery_key=f"backfill:{item.upstream_entity_id}",
            received_at="1970-01-01T00:00:00+00:00",
        )

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        if not envelope.body:
            return []
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        if as_str(message.get("type")) in REJECTED_TYPES:
            return []
        extracted = MAP.extract(payload if "message" in payload else {"message": message})
        source_call_id = as_str(extracted.get("source_call_id"))
        if not source_call_id:
            return []
        call_obj = message.get("call") if isinstance(message.get("call"), dict) else {}
        direction = CallDirection.UNKNOWN
        call_type = as_str(call_obj.get("type")) or ""
        if "inbound" in call_type.lower():
            direction = CallDirection.INBOUND
        elif "outbound" in call_type.lower():
            direction = CallDirection.OUTBOUND
        events: list[NormalizedEvent] = [
            CallObserved(
                source_call_id=source_call_id,
                agent_id=as_str(extracted.get("agent_id")) or "unknown",
                direction=direction,
                started_at=extracted.get("started_at") or parse_datetime(call_obj.get("startedAt")),
                architecture=PipelineArchitecture.CASCADE,
                source_path="message.call.id",
            )
        ]
        events.extend(_grounding(message, call_obj))
        events.extend(_turns_and_tools(message))
        events.extend(_latency(message))
        events.extend(_interruptions(message))
        recording = _recording(message)
        if recording:
            events.append(recording)
        ended = as_str(message.get("endedReason")) or as_str(call_obj.get("endedReason"))
        if ended:
            reason, party = classify_ended_reason(ended)
            events.append(
                OutcomeObserved(
                    provider_code=ended,
                    reason=reason,
                    party=party,
                    ended_at=extracted.get("ended_at") or parse_datetime(call_obj.get("endedAt")),
                    cost=as_float(extracted.get("cost")),
                    source_path="message.endedReason",
                )
            )
            events.append(CallFinalized(reason="end-of-call-report"))
        return events


def _grounding(message: dict[str, Any], call_obj: dict[str, Any]) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    assistant = message.get("assistant") if isinstance(message.get("assistant"), dict) else {}
    if not assistant and isinstance(call_obj.get("assistant"), dict):
        assistant = call_obj["assistant"]
    model = assistant.get("model") if isinstance(assistant.get("model"), dict) else {}
    for item in model.get("messages") or []:
        if isinstance(item, dict) and item.get("role") == "system":
            text = as_str(item.get("content"))
            if text:
                events.append(
                    GroundingObserved(
                        kind=GroundingKind.SYSTEM_PROMPT,
                        content=text,
                        source_path="message.assistant.model.messages[system]",
                    )
                )
    return events


def _turns_and_tools(message: dict[str, Any]) -> list[NormalizedEvent]:
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    messages = artifact.get("messages") or message.get("messages") or []
    events: list[NormalizedEvent] = []
    user_text: list[str] = []
    index = 0
    started_base = parse_datetime(message.get("startedAt"))
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = as_str(raw.get("role")) or ""
        if role in {"tool_calls", "tool_call", "function"} or raw.get("toolCalls"):
            for item in raw.get("toolCalls") or raw.get("toolCallList") or []:
                if not isinstance(item, dict):
                    continue
                inner = item.get("function") if isinstance(item.get("function"), dict) else item
                name = as_str(inner.get("name")) or "unknown"
                args = inner.get("arguments") or inner.get("parameters") or {}
                events.append(
                    ToolObserved(
                        tool_id=as_str(item.get("id")) or name,
                        name=name,
                        args=args,
                        status=ToolStatus.PENDING,
                        source_path="artifact.messages[].toolCalls",
                    )
                )
            continue
        if role in {"tool_call_result", "tool"} or raw.get("toolCallId"):
            result = raw.get("result") or raw.get("content")
            events.append(
                ToolObserved(
                    tool_id=as_str(raw.get("toolCallId")) or "tool",
                    name=as_str(raw.get("name")) or "unknown",
                    result=result,
                    status=ToolStatus.ERROR if (isinstance(result, str) and "error" in result.lower()[:40]) else ToolStatus.SUCCESS,
                    source_path="artifact.messages[].toolCallId",
                )
            )
            if result:
                events.append(
                    GroundingObserved(
                        kind=GroundingKind.TOOL_RESULT,
                        content=result if isinstance(result, str) else canonical_json(result),
                        source_path="artifact.messages[].result",
                    )
                )
            continue
        speaker = speaker_from(role)
        if speaker is Speaker.SYSTEM:
            continue
        text = as_str(raw.get("message")) or as_str(raw.get("content")) or ""
        seconds = as_float(raw.get("secondsFromStart"))
        duration = ms_from_seconds(raw.get("duration"))
        started = parse_datetime(raw.get("time"))
        ended = None
        if started and duration is not None:
            from datetime import timedelta

            ended = started + timedelta(milliseconds=duration)
        elif started_base and seconds is not None:
            from datetime import timedelta

            started = started_base + timedelta(seconds=seconds)
            if duration is not None:
                ended = started + timedelta(milliseconds=duration)
        events.append(
            TurnObserved(
                turn_index=index,
                speaker=speaker,
                text=text,
                started_at=started,
                ended_at=ended,
                interrupted=bool(raw.get("interrupted")) or None,
                confidence=_word_confidence(raw),
                source_path="artifact.messages[].secondsFromStart",
            )
        )
        if speaker is Speaker.USER and text:
            user_text.append(text)
        index += 1
    if user_text:
        events.append(
            GroundingObserved(
                kind=GroundingKind.USER_TEXT,
                content="\n".join(user_text),
                source_path="artifact.messages[user]",
            )
        )
    transcript = as_str(artifact.get("transcript"))
    if transcript:
        events.append(
            EvidenceObserved(
                kind=EvidenceKind.TRANSCRIPT,
                uri=f"inline:transcript:{content_hash(transcript)[:16]}",
                metadata={"chars": len(transcript)},
                source_path="artifact.transcript",
            )
        )
    return events


def _word_confidence(raw: dict[str, Any]) -> float | None:
    words = raw.get("words")
    if not isinstance(words, list) or not words:
        return as_float(raw.get("confidence"))
    values = [as_float(w.get("confidence")) for w in words if isinstance(w, dict)]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _latency(message: dict[str, Any]) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    perf = message.get("performanceMetrics") or artifact.get("performanceMetrics") or {}
    if not isinstance(perf, dict):
        return events
    mapping = (
        (Stage.STT, Metric.DURATION, "transcriberLatency"),
        (Stage.LLM, Metric.DURATION, "modelLatency"),
        (Stage.TTS, Metric.DURATION, "voiceLatency"),
        (Stage.E2E, Metric.DURATION, "turnLatency"),
        (Stage.ENDPOINTING, Metric.DURATION, "endpointingLatency"),
    )
    turns = perf.get("turnLatencies") or []
    if isinstance(turns, list):
        for item in turns:
            if not isinstance(item, dict):
                continue
            idx = int(as_float(item.get("turn") or item.get("index")) or 0)
            for stage, metric, key in mapping:
                value = as_float(item.get(key))
                if value is None:
                    continue
                events.append(
                    StageObserved(
                        stage=stage,
                        metric=metric,
                        value_ms=value,
                        turn_index=idx,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"artifact.performanceMetrics.turnLatencies[].{key}",
                    )
                )
    return events


def _interruptions(message: dict[str, Any]) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    for key, kind in (
        ("numAssistantInterrupted", InterruptionKind.ASSISTANT),
        ("numUserInterrupted", InterruptionKind.USER),
    ):
        count = as_float(message.get(key)) or as_float(analysis.get(key)) or as_float(artifact.get(key))
        if count is None:
            continue
        events.append(
            InterruptionObserved(
                count=int(count),
                kind=kind,
                source_path=key,
            )
        )
    return events


def _recording(message: dict[str, Any]) -> EvidenceObserved | None:
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    recording = artifact.get("recording") if isinstance(artifact.get("recording"), dict) else {}
    uri = (
        as_str(recording.get("stereoUrl"))
        or as_str(recording.get("monoUrl"))
        or as_str(artifact.get("recordingUrl"))
        or as_str(artifact.get("stereoRecordingUrl"))
    )
    if not uri:
        return None
    return EvidenceObserved(
        kind=EvidenceKind.RECORDING,
        uri=uri,
        source_path="artifact.recording",
    )
