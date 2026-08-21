from __future__ import annotations

from typing import Any

from obsalt.adapters.base import AdapterResult, empty_call, speaker_from, transcript_from_turns
from obsalt.domain.enums import CallDirection, CallStatus, LatencyComponent, Provider, Speaker, ToolStatus
from obsalt.domain.models import Hangup, LatencySample, ToolInvocation, Turn
from obsalt.domain.redact import payload_shape, preview_text
from obsalt.hangup.taxonomy import annotate_hangup, classify_provider_reason
from obsalt.tools.telemetry import parse_arguments
from obsalt.util import as_float, as_str, canonical_json, duration_ms, parse_datetime, sha256_text


def _events_of(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if isinstance(payload.get("events"), list):
        session = as_str(payload.get("session_id") or payload.get("call_id") or payload.get("provider_call_id"))
        return session, [e for e in payload["events"] if isinstance(e, dict)]
    if payload.get("type"):
        session = as_str(payload.get("session_id") or payload.get("call_id"))
        return session, [payload]
    return None, []


class OpenAIRealtimeAdapter:
    provider = Provider.OPENAI_REALTIME

    def parse(self, payload: dict[str, Any], *, org_id: str) -> AdapterResult | None:
        session_id, events = _events_of(payload)
        if payload.get("provider") == "openai_realtime" and isinstance(payload.get("call"), dict):
            # Already-normalized native snapshot is handled by NativeAdapter.
            return None
        session_id = session_id or as_str(payload.get("session_id"))
        if not session_id and events:
            session_id = as_str(events[0].get("session_id")) or as_str(events[0].get("event_id"))
        if not session_id:
            return None

        agent_id = as_str(payload.get("agent_id")) or as_str(payload.get("model")) or "openai-realtime"
        call = empty_call(
            org_id=org_id,
            provider=self.provider,
            provider_call_id=session_id,
            agent_id=agent_id,
        )
        call.agent_name = as_str(payload.get("agent_name"))
        direction = as_str(payload.get("direction"))
        if direction == "inbound":
            call.direction = CallDirection.INBOUND
        elif direction == "outbound":
            call.direction = CallDirection.OUTBOUND
        call.raw_event_type = as_str(payload.get("event_type")) or "realtime_events"

        builder = _RealtimeBuilder(call)
        for event in events:
            builder.consume(event)
        builder.flush()
        if not call.transcript_text:
            call.transcript_text = transcript_from_turns(call)

        hangup_reason = as_str(payload.get("hangup_reason")) or builder.end_reason
        terminal = builder.terminal or bool(payload.get("final")) or hangup_reason is not None
        if hangup_reason:
            reason, party = classify_provider_reason("native", hangup_reason)
            call.hangup = annotate_hangup(call, Hangup(reason=reason, party=party, provider_reason=hangup_reason, signal=hangup_reason))
            call.status = CallStatus.ENDED
        elif terminal:
            call.status = CallStatus.ENDED
            call.hangup = annotate_hangup(
                call,
                Hangup(reason=classify_provider_reason("native", "completed")[0], party=classify_provider_reason("native", "completed")[1], provider_reason="session_end"),
            )
        return AdapterResult(call=call, terminal=terminal, event_type=call.raw_event_type or "realtime_events")


class _RealtimeBuilder:
    def __init__(self, call) -> None:
        self.call = call
        self.speech_started: dict[str, float] = {}
        self.speech_stopped: dict[str, float] = {}
        self.response_created: dict[str, float] = {}
        self.first_audio: dict[str, float] = {}
        self.response_done: dict[str, float] = {}
        self.transcription_done: dict[str, float] = {}
        self.pending_user: dict[str, Turn] = {}
        self.pending_agent: dict[str, Turn] = {}
        self.tools: dict[str, ToolInvocation] = {}
        self.turn_index = 0
        self.terminal = False
        self.end_reason: str | None = None
        self.t0: float | None = None

    def _t(self, event: dict[str, Any]) -> float:
        explicit = as_float(event.get("t_ms")) or as_float(event.get("obsalt_t_ms"))
        if explicit is not None:
            return explicit
        dt = parse_datetime(event.get("obsalt_received_at") or event.get("received_at"))
        if dt is not None:
            ms = dt.timestamp() * 1000.0
            if self.t0 is None:
                self.t0 = ms
            return ms - self.t0
        return 0.0

    def consume(self, event: dict[str, Any]) -> None:
        etype = as_str(event.get("type")) or ""
        t_ms = self._t(event)
        if etype == "session.created" or etype == "session.updated":
            session = event.get("session") if isinstance(event.get("session"), dict) else {}
            instructions = as_str(session.get("instructions"))
            if instructions:
                self.call.grounding.system_prompt = instructions
            model = as_str(session.get("model"))
            if model:
                self.call.agent_id = self.call.agent_id if self.call.agent_id != "openai-realtime" else model
                self.call.metadata["model"] = model
            return
        if etype == "input_audio_buffer.speech_started":
            item_id = as_str(event.get("item_id")) or "user"
            start = as_float(event.get("audio_start_ms")) or t_ms
            self.speech_started[item_id] = start
            # Barge-in: mark last agent turn interrupted
            if self.call.turns:
                last_agent = next((t for t in reversed(self.call.turns) if t.speaker == Speaker.AGENT), None)
                if last_agent:
                    last_agent.interrupted = True
            return
        if etype == "input_audio_buffer.speech_stopped":
            item_id = as_str(event.get("item_id")) or "user"
            stop = as_float(event.get("audio_end_ms")) or t_ms
            self.speech_stopped[item_id] = stop
            return
        if etype in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.delta",
        } and etype.endswith("completed"):
            item_id = as_str(event.get("item_id")) or "user"
            transcript = as_str(event.get("transcript")) or ""
            start = self.speech_started.get(item_id)
            stop = self.speech_stopped.get(item_id) or t_ms
            stt_ms = None
            if stop is not None:
                stt_ms = max(0.0, t_ms - stop)
            turn = Turn(
                index=self.turn_index,
                speaker=Speaker.USER,
                text=transcript,
                seconds_from_start=(start / 1000.0) if start is not None else None,
                duration_ms=(stop - start) if start is not None and stop is not None else None,
                stt_ms=stt_ms,
            )
            self.turn_index += 1
            self.call.turns.append(turn)
            self.pending_user[item_id] = turn
            if stt_ms is not None:
                self.call.latency_samples.append(
                    LatencySample(component=LatencyComponent.STT, duration_ms=stt_ms, turn_index=turn.index, source="derived")
                )
            return
        if etype == "response.created":
            response_id = as_str(event.get("response", {}).get("id") if isinstance(event.get("response"), dict) else None) or as_str(event.get("response_id")) or "resp"
            self.response_created[response_id] = t_ms
            return
        if etype in {"response.audio.delta", "response.output_audio.delta", "response.audio_transcript.delta"}:
            response_id = as_str(event.get("response_id")) or "resp"
            self.first_audio.setdefault(response_id, t_ms)
            return
        if etype in {"response.audio_transcript.done", "response.output_audio_transcript.done"}:
            response_id = as_str(event.get("response_id")) or "resp"
            text = as_str(event.get("transcript")) or ""
            created = self.response_created.get(response_id)
            first = self.first_audio.get(response_id)
            llm_ttft = (first - created) if created is not None and first is not None else None
            user_stop = next(reversed(self.speech_stopped.values()), None) if self.speech_stopped else None
            ttfa = (first - user_stop) if first is not None and user_stop is not None else None
            turn = Turn(
                index=self.turn_index,
                speaker=Speaker.AGENT,
                text=text,
                seconds_from_start=(first / 1000.0) if first is not None else None,
                llm_ttft_ms=llm_ttft,
                llm_ms=llm_ttft,
                tts_ttfb_ms=llm_ttft,
                time_to_first_audio_ms=ttfa,
            )
            self.turn_index += 1
            self.pending_agent[response_id] = turn
            self.call.turns.append(turn)
            if llm_ttft is not None:
                self.call.latency_samples.append(
                    LatencySample(component=LatencyComponent.LLM, duration_ms=llm_ttft, turn_index=turn.index, ttft_ms=llm_ttft, source="derived")
                )
                self.call.latency_samples.append(
                    LatencySample(component=LatencyComponent.TTS, duration_ms=llm_ttft, turn_index=turn.index, ttfb_ms=llm_ttft, source="derived")
                )
            if ttfa is not None:
                self.call.latency_samples.append(
                    LatencySample(component=LatencyComponent.TTFA, duration_ms=ttfa, turn_index=turn.index, source="derived")
                )
                self.call.latency_samples.append(
                    LatencySample(component=LatencyComponent.E2E, duration_ms=ttfa, turn_index=turn.index, source="derived")
                )
            return
        if etype in {"response.audio.done", "response.output_audio.done", "response.done"}:
            response_id = as_str(event.get("response_id")) or as_str(
                event.get("response", {}).get("id") if isinstance(event.get("response"), dict) else None
            ) or "resp"
            self.response_done[response_id] = t_ms
            created = self.response_created.get(response_id)
            first = self.first_audio.get(response_id)
            turn = self.pending_agent.get(response_id)
            if turn and first is not None:
                turn.tts_ms = max(0.0, t_ms - first)
                turn.ended_at = None
            if etype == "response.done":
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                output = response.get("output") if isinstance(response.get("output"), list) else event.get("output") or []
                self._ingest_output_items(output, t_ms, created)
            return
        if etype == "response.function_call_arguments.done":
            call_id = as_str(event.get("call_id")) or as_str(event.get("item_id")) or "tool"
            name = as_str(event.get("name")) or "unknown"
            args = parse_arguments(event.get("arguments"))
            self.tools[call_id] = ToolInvocation(
                id=call_id,
                name=name,
                started_at=parse_datetime(event.get("obsalt_received_at")),
                status=ToolStatus.PENDING,
                payload_shape=payload_shape(args),
                argument_hash=sha256_text(canonical_json(args)),
                metadata={"arguments": args},
            )
            return
        if etype in {"conversation.item.created", "conversation.item.done"}:
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if as_str(item.get("type")) == "function_call_output":
                call_id = as_str(item.get("call_id")) or ""
                tool = self.tools.get(call_id)
                if tool:
                    output = item.get("output")
                    tool.result_preview = preview_text(output)
                    tool.status = ToolStatus.ERROR if _looks_error(output) else ToolStatus.SUCCESS
                    tool.ended_at = parse_datetime(event.get("obsalt_received_at"))
                    if tool.started_at and tool.ended_at:
                        tool.duration_ms = duration_ms(tool.started_at, tool.ended_at)
            return
        if etype in {"obsalt.session_end", "session.ended"}:
            self.terminal = True
            self.end_reason = as_str(event.get("reason")) or as_str(event.get("payload", {}).get("reason") if isinstance(event.get("payload"), dict) else None)
            return

    def _ingest_output_items(self, output: list[Any], t_ms: float, created: float | None) -> None:
        for item in output:
            if not isinstance(item, dict):
                continue
            if as_str(item.get("type")) in {"function_call", "function_call_arguments"}:
                call_id = as_str(item.get("call_id")) or as_str(item.get("id")) or "tool"
                name = as_str(item.get("name")) or "unknown"
                args = parse_arguments(item.get("arguments"))
                self.tools.setdefault(
                    call_id,
                    ToolInvocation(
                        id=call_id,
                        name=name,
                        status=ToolStatus.PENDING,
                        payload_shape=payload_shape(args),
                        metadata={"arguments": args},
                    ),
                )
            if as_str(item.get("type")) == "message" and not self.pending_agent:
                text_parts = []
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        text_parts.append(as_str(part.get("transcript") or part.get("text")) or "")
                text = " ".join(p for p in text_parts if p)
                if text:
                    turn = Turn(index=self.turn_index, speaker=Speaker.AGENT, text=text)
                    self.turn_index += 1
                    self.call.turns.append(turn)

    def flush(self) -> None:
        self.call.tools = list(self.tools.values())
        if self.call.started_at is None and self.call.turns:
            pass
        if self.call.turns:
            first = self.call.turns[0].seconds_from_start
            last = self.call.turns[-1]
            if first is not None and last.seconds_from_start is not None:
                self.call.duration_ms = (last.seconds_from_start - first) * 1000.0 + (last.duration_ms or 0)


def _looks_error(output: Any) -> bool:
    text = str(output).lower()
    return text.startswith('{"error') or text.startswith("error") or '"ok": false' in text or '"ok":false' in text
