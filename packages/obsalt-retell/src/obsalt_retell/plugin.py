"""Retell webhook source.

Signature: ``X-Retell-Signature: v={unix_ms},d={hex}``;
``HMAC-SHA256(raw_body + timestamp)`` keyed by the API key; ±5 min.

``words[].start/end`` are seconds. Latency ``values`` are samples;
``p50/p95/p99`` are AggregateMeasurements and never enter sample rollups.
Tool duration is unsupported.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from obsalt.auth.primitives import (
    constant_time_eq,
    enforce_window,
    hmac_hex,
    parse_kv_header,
    require_secret,
    singleton_or_reject,
)
from obsalt.domain.enums import (
    Capability,
    CallDirection,
    EvidenceKind,
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
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    ToolObserved,
    TurnObserved,
)
from obsalt.domain.fieldmap import as_float, as_str
from obsalt.domain.identity import canonical_json
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

DECODER_VERSION = "retell/2"

REASONS: dict[str, tuple[HangupReason, HangupParty]] = {
    "user_hangup": (HangupReason.USER_HANGUP, HangupParty.USER),
    "agent_hangup": (HangupReason.AGENT_HANGUP, HangupParty.AGENT),
    "call_transfer": (HangupReason.TRANSFER, HangupParty.AGENT),
    "transfer_bridged": (HangupReason.TRANSFER, HangupParty.AGENT),
    "transfer_cancelled": (HangupReason.CANCELLED, HangupParty.SYSTEM),
    "voicemail_reached": (HangupReason.VOICEMAIL, HangupParty.SYSTEM),
    "inactivity": (HangupReason.INACTIVITY, HangupParty.SYSTEM),
    "max_duration_reached": (HangupReason.MAX_DURATION, HangupParty.SYSTEM),
    "concurrency_limit_reached": (HangupReason.CONCURRENCY, HangupParty.SYSTEM),
    "dial_busy": (HangupReason.BUSY, HangupParty.USER),
    "dial_failed": (HangupReason.DIAL_FAILED, HangupParty.SYSTEM),
    "dial_no_answer": (HangupReason.NO_ANSWER, HangupParty.USER),
    "error_llm_websocket_open": (HangupReason.ERROR_LLM, HangupParty.SYSTEM),
    "error_asr": (HangupReason.ERROR_STT, HangupParty.SYSTEM),
    "error_no_audio_received": (HangupReason.ERROR_TTS, HangupParty.SYSTEM),
    "error_unknown": (HangupReason.ERROR_UNKNOWN, HangupParty.SYSTEM),
}

LATENCY_STAGE = {
    "e2e": Stage.E2E,
    "asr": Stage.STT,
    "llm": Stage.LLM,
    "tts": Stage.TTS,
    "s2s": Stage.GENERATION,
    "knowledge_base": Stage.LLM,
}


class RetellPlugin:
    API_VERSION = 1
    name = "retell"
    display_name = "Retell"
    DECODER_VERSION = DECODER_VERSION
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-retell-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"api_key"}))
    fidelity = FidelityDeclaration(
        source_format="retell.call_ended",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset(
            {MeasurementPlacement.UNPLACED, MeasurementPlacement.COARSE_ANCHOR}
        ),
        provides=frozenset(
            {
                Signal.STT_DURATION,
                Signal.LLM_DURATION,
                Signal.TTS_DURATION,
                Signal.E2E_DURATION,
                Signal.TURN_INTERVALS,
                Signal.WORD_TIMINGS,
                Signal.TRANSCRIPT,
                Signal.RECORDING,
                Signal.COST,
                Signal.HANGUP_REASON,
                Signal.GROUNDING_KNOWLEDGE,
                Signal.GROUNDING_TOOL_RESULTS,
                Signal.GROUNDING_USER_TEXT,
                Signal.TOOL_PAYLOAD,
            }
        ),
        structurally_absent={
            Signal.STAGE_INTERVALS: "Retell publishes call-level distributions and approximate word intervals",
            Signal.TOOL_TIMING: "Retell does not timestamp tool utterances; duration is not reported by Retell",
        },
        schema_source="https://docs.retellai.com/api-references/get-call",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        missing = require_secret(cfg.credentials.get("api_key"), name="api_key")
        if missing:
            return missing
        value, err = singleton_or_reject(headers, b"x-retell-signature")
        if err:
            return err
        assert value is not None
        parsed = parse_kv_header(value.decode("latin-1"))
        timestamp = parsed.get("v")
        digest = parsed.get("d")
        if not timestamp or not digest:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="expected v={ms},d={hex}")
        try:
            event_ms = int(timestamp)
        except ValueError:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="timestamp is not an integer")
        window = enforce_window(event_ms, tolerance=timedelta(minutes=5))
        if not window.ok:
            return window
        expected = hmac_hex(cfg.credentials["api_key"], raw + timestamp.encode("utf-8"))
        if not constant_time_eq(expected, digest):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE, detail="retell hmac mismatch")
        return VerifyResult(outcome=VerifyOutcome.OK, event_time_ms=event_ms)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return ObservationalEventKind.UNKNOWN_OBSERVATIONAL
        event = as_str(payload.get("event")) or ""
        return {
            "call_started": ObservationalEventKind.CALL_STARTED,
            "call_ended": ObservationalEventKind.CALL_ENDED,
            "call_analyzed": ObservationalEventKind.CALL_ANALYZED,
            "transcript_updated": ObservationalEventKind.TRANSCRIPT,
        }.get(event, ObservationalEventKind.UNKNOWN_OBSERVATIONAL)

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        call_id = as_str(blob.get("call_id"))
        event = as_str(payload.get("event"))
        if call_id and event:
            return f"{event}:{call_id}"
        return None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return TombstoneHints()
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        return TombstoneHints(source_call_id=as_str(blob.get("call_id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=204, body=b"")

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        if not envelope.body:
            return []
        try:
            payload = json.loads(envelope.body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        source_call_id = as_str(blob.get("call_id"))
        if not source_call_id:
            return []
        direction = CallDirection.UNKNOWN
        if blob.get("direction") == "inbound":
            direction = CallDirection.INBOUND
        elif blob.get("direction") == "outbound":
            direction = CallDirection.OUTBOUND
        events: list[NormalizedEvent] = [
            CallObserved(
                source_call_id=source_call_id,
                agent_id=as_str(blob.get("agent_id")) or "unknown",
                direction=direction,
                started_at=parse_datetime(blob.get("start_timestamp")),
                architecture=PipelineArchitecture.CASCADE,
                from_number=as_str(blob.get("from_number")),
                to_number=as_str(blob.get("to_number")),
                source_path="call.call_id",
            )
        ]
        dynamic = blob.get("retell_llm_dynamic_variables")
        if isinstance(dynamic, dict) and dynamic:
            events.append(
                GroundingObserved(
                    kind=GroundingKind.KNOWLEDGE,
                    content=canonical_json(dynamic),
                    source_path="call.retell_llm_dynamic_variables",
                )
            )
        events.extend(_turns_and_tools(blob))
        events.extend(_latency(blob.get("latency") if isinstance(blob.get("latency"), dict) else {}))
        recording = as_str(blob.get("recording_url")) or as_str(blob.get("recording_multi_channel_url"))
        if recording:
            events.append(
                EvidenceObserved(kind=EvidenceKind.RECORDING, uri=recording, source_path="call.recording_url")
            )
        reason = as_str(blob.get("disconnection_reason"))
        if reason:
            mapped = REASONS.get(reason, (HangupReason.UNKNOWN, HangupParty.UNKNOWN))
            if reason.startswith("error_llm"):
                mapped = (HangupReason.ERROR_LLM, HangupParty.SYSTEM)
            cost = None
            block = blob.get("call_cost") if isinstance(blob.get("call_cost"), dict) else {}
            cents = as_float(block.get("combined_cost"))
            if cents is not None:
                cost = cents / 100.0
            events.append(
                OutcomeObserved(
                    provider_code=reason,
                    reason=mapped[0],
                    party=mapped[1],
                    ended_at=parse_datetime(blob.get("end_timestamp")),
                    cost=cost,
                    source_path="call.disconnection_reason",
                )
            )
        if as_str(payload.get("event")) in {"call_ended", "call_analyzed"}:
            events.append(CallFinalized(reason=as_str(payload.get("event")) or "call_ended"))
        return events


def _turns_and_tools(blob: dict[str, Any]) -> list[NormalizedEvent]:
    items = blob.get("transcript_with_tool_calls") or blob.get("transcript_object") or []
    events: list[NormalizedEvent] = []
    user_text: list[str] = []
    index = 0
    started = parse_datetime(blob.get("start_timestamp"))
    for item in items:
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("role")) or ""
        if role == "tool_call_invocation":
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            events.append(
                ToolObserved(
                    tool_id=as_str(item.get("tool_call_id")) or "tool",
                    name=as_str(item.get("name")) or "unknown",
                    args=args,
                    status=ToolStatus.PENDING,
                    source_path="call.transcript_with_tool_calls[tool_call_invocation]",
                )
            )
            continue
        if role == "tool_call_result":
            content = item.get("content")
            success = item.get("successful")
            events.append(
                ToolObserved(
                    tool_id=as_str(item.get("tool_call_id")) or "tool",
                    name="unknown",
                    result=content,
                    status=ToolStatus.ERROR if success is False else ToolStatus.SUCCESS,
                    source_path="call.transcript_with_tool_calls[tool_call_result]",
                )
            )
            if content:
                events.append(
                    GroundingObserved(
                        kind=GroundingKind.TOOL_RESULT,
                        content=content if isinstance(content, str) else canonical_json(content),
                        source_path="call.transcript_with_tool_calls[tool_call_result].content",
                    )
                )
            continue
        speaker = speaker_from(role)
        words = item.get("words") if isinstance(item.get("words"), list) else []
        start_s = as_float(words[0].get("start")) if words and isinstance(words[0], dict) else None
        end_s = as_float(words[-1].get("end")) if words and isinstance(words[-1], dict) else None
        text = as_str(item.get("content")) or ""
        turn_start = None
        turn_end = None
        if started and start_s is not None:
            turn_start = started + timedelta(seconds=start_s)
        if started and end_s is not None:
            turn_end = started + timedelta(seconds=end_s)
        events.append(
            TurnObserved(
                turn_index=index,
                speaker=speaker,
                text=text,
                started_at=turn_start,
                ended_at=turn_end,
                source_path="call.transcript_with_tool_calls[].words.start",
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
                source_path="call.transcript_with_tool_calls[user]",
            )
        )
    return events


def _latency(latency: dict[str, Any]) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    for key, stage in LATENCY_STAGE.items():
        block = latency.get(key)
        if not isinstance(block, dict):
            continue
        values = block.get("values")
        if isinstance(values, list):
            for i, value in enumerate(values):
                ms = as_float(value)
                if ms is None:
                    continue
                events.append(
                    StageObserved(
                        stage=stage,
                        metric=Metric.DURATION,
                        value_ms=ms,
                        turn_index=i,
                        placement=MeasurementPlacement.UNPLACED,
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path=f"call.latency.{key}.values[]",
                    )
                )
        population = int(as_float(block.get("num")) or 0) or None
        for stat_name, stat in (("p50", Statistic.P50), ("p90", Statistic.P90), ("p95", Statistic.P95), ("p99", Statistic.P99)):
            ms = as_float(block.get(stat_name))
            if ms is None:
                continue
            events.append(
                AggregateObserved(
                    stage=stage,
                    metric=Metric.DURATION,
                    statistic=stat,
                    value_ms=ms,
                    population=population,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"call.latency.{key}.{stat_name}",
                )
            )
    rtt = as_float(latency.get("llm_websocket_network_rtt"))
    if rtt is not None:
        events.append(
            StageObserved(
                stage=Stage.TRANSPORT,
                metric=Metric.DURATION,
                value_ms=rtt,
                placement=MeasurementPlacement.UNPLACED,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="call.latency.llm_websocket_network_rtt",
            )
        )
    return events
