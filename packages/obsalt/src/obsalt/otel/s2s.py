"""Speech-to-speech span decode shared by OpenAI Realtime and Gemini Live mappers.

Native stages are user_input / generation / playout. Cascade STT/LLM/TTS stages
do not exist on these sources and must never be emitted, even as empty rows.
Barge-in is taken only from explicit interruption signals, never from the
heuristic "any user speech after an agent turn" — that is a false positive.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from obsalt.analysis.hangup import party_for_reason
from obsalt.domain.enums import (
    GroundingKind,
    HangupReason,
    MeasurementPlacement,
    Metric,
    PipelineArchitecture,
    Provenance,
    Speaker,
    Stage,
)
from obsalt.domain.events import (
    CallObserved,
    GroundingObserved,
    InterruptionObserved,
    NormalizedEvent,
    OutcomeObserved,
    StageObserved,
    TurnObserved,
)
from obsalt.domain.models import ProvenanceStamp
from obsalt.otel.conventions import (
    AGENT_ID,
    CONVERSATION_ID,
    ERROR_TYPE,
    GENAI_END_REASON,
    OBSALT_BARGE_IN,
    OBSALT_HANGUP_PROVIDER_CODE,
    OBSALT_HANGUP_REASON,
    PII_AGENT_TRANSCRIPT,
    PII_USER_TRANSCRIPT,
    PROVIDER_CALL_ID,
    SPAN_GENERATION,
    SPAN_LLM,
    SPAN_PLAYOUT,
    SPAN_STT,
    SPAN_STT_PROVIDER_ATTEMPT,
    SPAN_STT_SELECTION,
    SPAN_TTS,
    SPAN_USER_INPUT,
    TURN_INDEX,
    agent_id_from_attrs,
)
from obsalt.otel.span_time import valid_span_interval
from obsalt.plugin.types import InstrumentedClient, ReadableSpan, SdkConfig

S2S_SPAN_TO_STAGE = {
    SPAN_USER_INPUT: Stage.USER_INPUT,
    SPAN_GENERATION: Stage.GENERATION,
    SPAN_PLAYOUT: Stage.PLAYOUT,
}

# Never map these to S2S output. Speech-to-speech has no cascade split.
CASCADE_SPAN_NAMES = frozenset(
    {
        SPAN_STT,
        SPAN_LLM,
        SPAN_TTS,
        SPAN_STT_PROVIDER_ATTEMPT,
        SPAN_STT_SELECTION,
        "stt",
        "llm",
        "tts",
    }
)

_TRUTHY = {True, "1", "true", "True", "yes", "YES"}

_BARGE_IN_ATTRS = (
    OBSALT_BARGE_IN,
    "conversation.interrupted",
    "openai.realtime.interrupted",
    "gemini.interrupted",
    "interrupted",
)

_BARGE_IN_EVENTS = frozenset(
    {
        "interrupted",
        "barge_in",
        "obsalt.barge_in",
        "speech_interrupted",
        "conversation.interrupted",
    }
)

_CANCEL_INTERRUPT_REASONS = frozenset(
    {
        "turn_detected",
        "interrupted",
        "barge_in",
        "client_interrupted",
        "user_interruption",
    }
)


def _truthy(value: object) -> bool:
    return value in _TRUTHY


def is_real_barge_in(span: ReadableSpan) -> bool:
    """True only for explicit interruption signals, not follow-on user speech."""

    attrs = span.attributes or {}
    for key in _BARGE_IN_ATTRS:
        if _truthy(attrs.get(key)):
            return True
    event_name = str(attrs.get("openai.realtime.event") or "")
    if event_name == "conversation.interrupted":
        return True
    reason = str(
        attrs.get("response.status_details.reason")
        or attrs.get("status_details.reason")
        or attrs.get("openai.realtime.cancel_reason")
        or ""
    ).lower()
    status = str(attrs.get("response.status") or attrs.get("status") or "").lower()
    cancelled = status in {"cancelled", "canceled"} or event_name == "response.cancelled"
    if cancelled and reason in _CANCEL_INTERRUPT_REASONS:
        return True
    for event in span.events or []:
        name = str(event.get("name") or "").lower()
        if name in _BARGE_IN_EVENTS:
            return True
    return False


def _turn_index(span: ReadableSpan) -> int | None:
    raw = (span.attributes or {}).get(TURN_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def decode_s2s_spans(
    spans: Sequence[ReadableSpan],
    *,
    architecture: PipelineArchitecture = PipelineArchitecture.SPEECH_TO_SPEECH,
) -> Iterable[NormalizedEvent]:
    if not spans:
        return
    conv = None
    agent_id = None
    agent_key = None
    outcome = None
    for span in spans:
        attrs = span.attributes or {}
        conv = attrs.get(CONVERSATION_ID) or attrs.get(PROVIDER_CALL_ID) or conv
        if agent_id is None:
            found = agent_id_from_attrs(attrs)
            if found:
                agent_id = found
                agent_key = (
                    AGENT_ID
                    if attrs.get(AGENT_ID) not in (None, "")
                    else next(
                        (
                            key
                            for key in ("obsalt.agent.id", "gen_ai.agent.id")
                            if attrs.get(key) not in (None, "")
                        ),
                        AGENT_ID,
                    )
                )
        found_outcome = outcome_from_span_attrs(attrs)
        if found_outcome is not None:
            outcome = found_outcome
    provenance: dict[str, ProvenanceStamp] = {}
    if agent_id and agent_key:
        provenance["agent_id"] = ProvenanceStamp(
            provenance=Provenance.PROVIDER_REPORTED, source_path=agent_key
        )
    if conv:
        yield CallObserved(
            source_call_id=str(conv),
            agent_id=agent_id,
            architecture=architecture,
            provenance_by_field=provenance,
        )
    if outcome is not None:
        yield outcome
    next_turn = 0
    for span in spans:
        if span.name in CASCADE_SPAN_NAMES:
            continue
        if is_real_barge_in(span):
            yield InterruptionObserved(turn_index=_turn_index(span), count=1, kind="barge_in")
        turn = _turn_from_span(span, next_turn)
        if turn is not None:
            yield turn
            if turn.speaker is Speaker.USER and turn.text:
                yield GroundingObserved(
                    kind=GroundingKind.USER_TEXT,
                    content=turn.text,
                    provenance=Provenance.PROVIDER_REPORTED,
                    source_path=f"span.attributes.{PII_USER_TRANSCRIPT}",
                )
            next_turn = max(next_turn, turn.turn_index + 1)
        stage = S2S_SPAN_TO_STAGE.get(span.name)
        if stage is None:
            continue
        if not valid_span_interval(span):
            continue
        yield StageObserved(
            stage=stage,
            metric=Metric.DURATION,
            value_ms=(span.end_unix_nano - span.start_unix_nano) / 1e6,
            turn_index=_turn_index(span),
            placement=MeasurementPlacement.INTERVAL,
            started_at=datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC),
            ended_at=datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC),
            provenance=Provenance.PROVIDER_REPORTED,
            source_path=f"span:{span.name}",
        )


def outcome_from_span_attrs(attrs: Mapping[str, object]) -> OutcomeObserved | None:
    reason_raw = attrs.get(OBSALT_HANGUP_REASON) or attrs.get(GENAI_END_REASON)
    code_raw = attrs.get(OBSALT_HANGUP_PROVIDER_CODE) or reason_raw or attrs.get(ERROR_TYPE)
    if reason_raw in (None, "") and attrs.get(ERROR_TYPE) in (None, ""):
        return None
    code = str(code_raw or reason_raw or "")
    if not code:
        return None
    parsed: HangupReason | None = None
    if reason_raw not in (None, ""):
        try:
            parsed = HangupReason(str(reason_raw))
        except ValueError:
            parsed = None
    stamp = ProvenanceStamp(
        provenance=Provenance.PROVIDER_REPORTED,
        source_path=(
            OBSALT_HANGUP_REASON
            if attrs.get(OBSALT_HANGUP_REASON) not in (None, "")
            else GENAI_END_REASON
            if attrs.get(GENAI_END_REASON) not in (None, "")
            else ERROR_TYPE
        ),
    )
    if parsed is not None:
        return OutcomeObserved(
            provider_code=code,
            reason=parsed,
            party=party_for_reason(parsed),
            provenance_by_field={"provider_code": stamp},
        )
    return OutcomeObserved(provider_code=code, provenance_by_field={"provider_code": stamp})


def _turn_from_span(span: ReadableSpan, fallback_index: int) -> TurnObserved | None:
    attrs = span.attributes or {}
    user_text = attrs.get(PII_USER_TRANSCRIPT)
    agent_text = attrs.get(PII_AGENT_TRANSCRIPT)
    speaker: Speaker | None = None
    text = ""
    if span.name == SPAN_USER_INPUT or (
        user_text not in (None, "") and span.name != SPAN_GENERATION
    ):
        if user_text not in (None, ""):
            speaker = Speaker.USER
            text = str(user_text)
    if speaker is None and agent_text not in (None, ""):
        speaker = Speaker.AGENT
        text = str(agent_text)
    if speaker is None or not text:
        return None
    index = _turn_index(span)
    started = None
    ended = None
    if span.start_unix_nano:
        started = datetime.fromtimestamp(span.start_unix_nano / 1e9, tz=UTC)
    if valid_span_interval(span):
        ended = datetime.fromtimestamp(span.end_unix_nano / 1e9, tz=UTC)
    return TurnObserved(
        turn_index=index if index is not None else fallback_index,
        speaker=speaker,
        text=text,
        started_at=started,
        ended_at=ended,
    )


class InstrumentedVoiceClient:
    """Thin OTel wrapper. Own process emits S2S spans; we do not invent cascade stages."""

    def __init__(self, client: object, *, provider: str, service_name: str = "voice-agent") -> None:
        self.client = client
        self.provider = provider
        self.service_name = service_name
        self.emitted: list[ReadableSpan] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self.client, name)

    def record_user_input(
        self,
        conversation_id: str,
        start_unix_nano: int,
        end_unix_nano: int,
        *,
        attributes: dict[str, object] | None = None,
    ) -> ReadableSpan:
        return self._emit(
            SPAN_USER_INPUT, conversation_id, start_unix_nano, end_unix_nano, attributes
        )

    def record_generation(
        self,
        conversation_id: str,
        start_unix_nano: int,
        end_unix_nano: int,
        *,
        attributes: dict[str, object] | None = None,
    ) -> ReadableSpan:
        return self._emit(
            SPAN_GENERATION, conversation_id, start_unix_nano, end_unix_nano, attributes
        )

    def record_playout(
        self,
        conversation_id: str,
        start_unix_nano: int,
        end_unix_nano: int,
        *,
        attributes: dict[str, object] | None = None,
    ) -> ReadableSpan:
        return self._emit(SPAN_PLAYOUT, conversation_id, start_unix_nano, end_unix_nano, attributes)

    def record_barge_in(
        self,
        conversation_id: str,
        start_unix_nano: int,
        end_unix_nano: int,
        *,
        turn_index: int | None = None,
    ) -> ReadableSpan:
        attrs: dict[str, object] = {OBSALT_BARGE_IN: True}
        if turn_index is not None:
            attrs[TURN_INDEX] = turn_index
        return self._emit(SPAN_GENERATION, conversation_id, start_unix_nano, end_unix_nano, attrs)

    def _emit(
        self,
        name: str,
        conversation_id: str,
        start_unix_nano: int,
        end_unix_nano: int,
        attributes: dict[str, object] | None,
    ) -> ReadableSpan:
        attrs: dict[str, str | bool | int | float] = {
            CONVERSATION_ID: conversation_id,
            "gen_ai.provider.name": self.provider,
        }
        if attributes:
            for key, value in attributes.items():
                if isinstance(value, (str, bool, int, float)):
                    attrs[key] = value
        span = ReadableSpan(
            name=name,
            trace_id="0" * 32,
            span_id=f"{len(self.emitted):016x}",
            parent_span_id=None,
            start_unix_nano=start_unix_nano,
            end_unix_nano=end_unix_nano,
            attributes=attrs,
        )
        self.emitted.append(span)
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer(self.service_name)
            with tracer.start_as_current_span(name) as otel_span:
                for key, value in attrs.items():
                    otel_span.set_attribute(key, value)
        except Exception:
            pass
        return span


def instrument_s2s(
    client: object, cfg: SdkConfig, *, provider: str, notes: str
) -> InstrumentedClient:
    wrapper = InstrumentedVoiceClient(client, provider=provider, service_name=cfg.service_name)
    return InstrumentedClient(client=wrapper, notes=notes)
