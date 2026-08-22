from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta

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
    NormalizedEvent,
    OutcomeObserved,
    SnapshotBoundaryObserved,
    StageObserved,
    ToolObserved,
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

_OBSERVATIONAL = {
    "call_started": ObservationalEventKind.CALL_STARTED,
    "call_ended": ObservationalEventKind.CALL_ENDED,
    "call_analyzed": ObservationalEventKind.CALL_ANALYZED,
    "transcript_updated": ObservationalEventKind.TRANSCRIPT,
    "transfer_started": ObservationalEventKind.STATUS,
    "transfer_bridged": ObservationalEventKind.STATUS,
    "transfer_cancelled": ObservationalEventKind.STATUS,
    "transfer_ended": ObservationalEventKind.STATUS,
}

_STAGE_KEYS = {
    "e2e": (Stage.E2E, Metric.DURATION),
    "asr": (Stage.STT, Metric.DURATION),
    "llm": (Stage.LLM, Metric.TTFT),
    "tts": (Stage.TTS, Metric.TTFB),
    "s2s": (Stage.GENERATION, Metric.DURATION),
    "knowledge_base": (Stage.LLM, Metric.DURATION),
    "llm_websocket_network_rtt": (Stage.TRANSPORT, Metric.DURATION),
}


class RetellPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "retell"
    display_name = "Retell"
    decoder_version = "retell/2"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-retell-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"api_key"}))
    fidelity = FidelityDeclaration(
        source_format="retell.call",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE, PipelineArchitecture.SPEECH_TO_SPEECH}),
        possible_placements=frozenset(
            {MeasurementPlacement.UNPLACED, MeasurementPlacement.COARSE_ANCHOR, MeasurementPlacement.ANCHORED_DURATION}
        ),
        provides=frozenset(
            {
                Signal.STT_DURATION,
                Signal.LLM_TTFT,
                Signal.TTS_TTFB,
                Signal.E2E_DURATION,
                Signal.TRANSPORT,
                Signal.TURN_INTERVAL,
                Signal.WORD_TIMING,
                Signal.TRANSCRIPT,
                Signal.TOOL_RESULT,
                Signal.GROUNDING_TOOLS,
                Signal.GROUNDING_KNOWLEDGE,
                Signal.GROUNDING_USER,
                Signal.RECORDING,
                Signal.COST,
                Signal.HANGUP,
            }
        ),
        structurally_absent={
            Signal.STAGE_INTERVAL: "Retell publishes call-level latency distributions and per-turn samples without stage timestamps",
            Signal.TOOL_TIMING: "Retell does not provide direct invocation/result timestamps; duration is not reported",
            Signal.VAD: "Retell does not publish VAD clocks",
        },
        schema_source="https://docs.retellai.com/api-references/get-call (Utterance.words in seconds; CallLatency in ms)",
        schema_revision="2026-08-22",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers: list[tuple[bytes, bytes]], cfg: ConnectionConfig) -> VerifyResult:
        api_key = cfg.secrets.get("api_key")
        if not api_key:
            return VerifyResult(outcome=VerifyOutcome.MISSING_CREDENTIAL, detail="api_key required")
        values = header_values(headers, "x-retell-signature")
        if not values:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="missing X-Retell-Signature")
        parsed = parse_kv_header(values[0])
        ts = parsed.get("v")
        digest = parsed.get("d")
        if not ts or not digest:
            return VerifyResult(outcome=VerifyOutcome.MALFORMED, detail="expected v={ms},d={hex}")
        window = enforce_window(float(ts), tolerance_seconds=300, unit="ms")
        if window is not None:
            return window
        # HMAC-SHA256(raw_body + timestamp) keyed by the API key. timestamp is the v= milliseconds string.
        expected = hmac_hex(api_key, raw + ts.encode("utf-8"))
        if not constant_time_eq(digest, expected):
            return VerifyResult(outcome=VerifyOutcome.BAD_SIGNATURE)
        return VerifyResult(outcome=VerifyOutcome.OK)

    def classify(self, raw: bytes) -> ObservationalEventKind:
        payload = _json(raw)
        event = as_str(payload.get("event")) or "call_ended"
        return _OBSERVATIONAL.get(event, ObservationalEventKind.UNKNOWN_OBSERVATIONAL)

    def delivery_key(self, raw: bytes, headers: list[tuple[bytes, bytes]]) -> str | None:
        payload = _json(raw)
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        call_id = as_str(blob.get("call_id"))
        event = as_str(payload.get("event")) or "call_ended"
        if call_id:
            return f"{call_id}:{event}"
        return None

    def tombstone_hints(self, raw: bytes) -> TombstoneHints:
        payload = _json(raw)
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        return TombstoneHints(source_call_id=as_str(blob.get("call_id")))

    def acknowledgement(self, kind: ObservationalEventKind) -> WebhookResponse:
        return WebhookResponse(status_code=200, body=b'{"ok":true}')

    def decode(self, envelope: RawEnvelope) -> Iterable[NormalizedEvent]:
        payload = _json(envelope.body or b"{}")
        event = as_str(payload.get("event")) or "call_ended"
        blob = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        call_id = as_str(blob.get("call_id"))
        if not call_id:
            return
        direction = CallDirection.UNKNOWN
        if blob.get("direction") == "inbound":
            direction = CallDirection.INBOUND
        elif blob.get("direction") == "outbound":
            direction = CallDirection.OUTBOUND
        cost = None
        cost_obj = blob.get("call_cost") if isinstance(blob.get("call_cost"), dict) else {}
        cents = as_float(cost_obj.get("combined_cost"))
        if cents is not None:
            cost = cents / 100.0
        yield CallObserved(
            source_call_id=call_id,
            agent_id=as_str(blob.get("agent_id")) or "unknown",
            direction=direction,
            from_number=as_str(blob.get("from_number")),
            to_number=as_str(blob.get("to_number")),
            started_at=parse_datetime(blob.get("start_timestamp")),
            ended_at=parse_datetime(blob.get("end_timestamp")),
            cost=cost,
            architecture=PipelineArchitecture.CASCADE,
            provenance_by_field={
                "source_call_id": ProvenanceStamp(provenance=Provenance.PROVIDER_REPORTED, source_path="call.call_id"),
            },
        )
        # §5.3: only ended/analyzed snapshots are authoritative. call_started
        # and transcript_updated are deltas and must not retract by omission.
        if event in {"call_ended", "call_analyzed"}:
            yield SnapshotBoundaryObserved(
                authoritative_domains=["turn_observed", "stage_observed", "aggregate_observed", "tool_observed"]
            )
        recording = as_str(blob.get("recording_url")) or as_str(blob.get("recording_multi_channel_url"))
        if recording:
            yield EvidenceObserved(
                kind=EvidenceKind.RECORDING,
                uri=recording,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="call.recording_url",
            )
        transcript = as_str(blob.get("transcript"))
        if transcript:
            yield EvidenceObserved(
                kind=EvidenceKind.TRANSCRIPT,
                uri="inline",
                metadata={"text": transcript},
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="call.transcript",
            )
        dynamic = blob.get("retell_llm_dynamic_variables")
        if isinstance(dynamic, dict) and dynamic:
            yield GroundingObserved(
                kind=GroundingKind.KNOWLEDGE,
                content=json.dumps(dynamic, sort_keys=True),
                provenance=Provenance.PROVIDER_REPORTED,
                source_path="call.retell_llm_dynamic_variables",
            )
        woven = blob.get("transcript_with_tool_calls") or blob.get("transcript_object") or []
        if isinstance(woven, list):
            yield from _turns_and_tools(woven, parse_datetime(blob.get("start_timestamp")))
        yield from _latency(blob.get("latency") if isinstance(blob.get("latency"), dict) else {})
        reason = as_str(blob.get("disconnection_reason"))
        if reason:
            yield OutcomeObserved(
                provider_code=reason,
                ended_at=parse_datetime(blob.get("end_timestamp")),
                provenance_by_field={
                    "provider_code": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED, source_path="call.disconnection_reason"
                    )
                },
            )
        if event in {"call_ended", "call_analyzed"}:
            yield CallFinalized(reason="provider")


def _turns_and_tools(items: list, call_started: datetime | None) -> Iterable[NormalizedEvent]:
    turn_index = 0
    pending: dict[str, str] = {}
    user_texts: list[str] = []
    last_anchor: datetime | None = None
    last_anchor_path: str | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        role = as_str(item.get("role")) or ""
        if role == "tool_call_invocation":
            tool_id = as_str(item.get("tool_call_id")) or "tool"
            name = as_str(item.get("name")) or "unknown"
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            pending[tool_id] = name
            provenance: dict[str, ProvenanceStamp] = {
                "name": ProvenanceStamp(
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="call.transcript_with_tool_calls[role=tool_call_invocation].name",
                )
            }
            if last_anchor is not None:
                provenance["started_at"] = ProvenanceStamp(
                    provenance=Provenance.OBSALT_DERIVED,
                    source_path=last_anchor_path,
                    derivation="coarse_anchor from preceding utterance words; duration not reported by Retell",
                )
            yield ToolObserved(
                tool_id=tool_id,
                name=name,
                turn_index=turn_index,
                started_at=last_anchor,
                status=ToolStatus.PENDING,
                args=args,
                provenance_by_field=provenance,
            )
            continue
        if role == "tool_call_result":
            tool_id = as_str(item.get("tool_call_id")) or "tool"
            success = item.get("successful")
            content = item.get("content")
            yield ToolObserved(
                tool_id=tool_id,
                name=pending.get(tool_id, "unknown"),
                started_at=last_anchor,
                status=ToolStatus.ERROR if success is False else ToolStatus.SUCCESS,
                result=content,
                error=None if success is not False else str(content),
                provenance_by_field={
                    "name": ProvenanceStamp(
                        provenance=Provenance.PROVIDER_REPORTED,
                        source_path="call.transcript_with_tool_calls[role=tool_call_result].name",
                    )
                },
            )
            if content:
                yield GroundingObserved(
                    kind=GroundingKind.TOOL_RESULT,
                    content=content if isinstance(content, str) else json.dumps(content),
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="call.transcript_with_tool_calls[role=tool_call_result].content",
                )
            continue
        speaker = speaker_from(role)
        words = item.get("words") if isinstance(item.get("words"), list) else []
        start_s = None
        end_s = None
        if words and isinstance(words[0], dict):
            start_s = as_float(words[0].get("start"))
            end_s = as_float(words[-1].get("end")) if isinstance(words[-1], dict) else None
        text = as_str(item.get("content")) or ""
        if not text and words:
            text = " ".join(as_str(w.get("word")) or "" for w in words if isinstance(w, dict)).strip()
        started = None
        ended = None
        if call_started is not None and start_s is not None:
            started = call_started + timedelta(seconds=start_s)
        if call_started is not None and end_s is not None:
            ended = call_started + timedelta(seconds=end_s)
        yield TurnObserved(
            turn_index=turn_index,
            speaker=speaker,
            text=text,
            started_at=started,
            ended_at=ended,
            provenance_by_field={
                "started_at": ProvenanceStamp(
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path="call.transcript_with_tool_calls[].words[].start (seconds)",
                )
            },
        )
        if speaker is Speaker.USER and text:
            user_texts.append(text)
        if ended is not None:
            last_anchor = ended
            last_anchor_path = "call.transcript_with_tool_calls[].words[].end (seconds)"
        elif started is not None:
            last_anchor = started
            last_anchor_path = "call.transcript_with_tool_calls[].words[].start (seconds)"
        turn_index += 1
    if user_texts:
        yield GroundingObserved(
            kind=GroundingKind.USER_TEXT,
            content="\n".join(user_texts),
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="call.transcript_with_tool_calls[role=user].content",
        )


def _latency(latency: dict) -> Iterable[NormalizedEvent]:
    for key, (stage, metric) in _STAGE_KEYS.items():
        block = latency.get(key)
        if not isinstance(block, dict):
            continue
        values = block.get("values")
        if isinstance(values, list):
            for index, value in enumerate(values):
                ms = as_float(value)
                if ms is None:
                    continue
                yield StageObserved(
                    stage=stage,
                    metric=metric,
                    value_ms=ms,
                    turn_index=index,
                    placement=MeasurementPlacement.UNPLACED,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"call.latency.{key}.values[{index}]",
                )
        for stat_name, statistic in (
            ("p50", Statistic.P50),
            ("p90", Statistic.P90),
            ("p95", Statistic.P95),
            ("p99", Statistic.P99),
            ("min", Statistic.MIN),
            ("max", Statistic.MAX),
        ):
            ms = as_float(block.get(stat_name))
            if ms is None:
                continue
            yield AggregateObserved(
                stage=stage,
                metric=metric,
                statistic=statistic,
                value_ms=ms,
                population=int(as_float(block.get("num")) or 0) or None,
                provenance=Provenance.PROVIDER_REPORTED,
                source_path=f"call.latency.{key}.{stat_name}",
            )


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
