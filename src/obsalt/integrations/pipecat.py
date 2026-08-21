"""Pipecat pipeline observer. Optional — does not import pipecat at module load.

Attach to ``PipelineTask(..., observers=[observer])`` when ``pipecat-ai`` is
installed, or call ``handle_frame`` from any frame callback. Close the observer
when the pipeline ends so the evidence snapshot is POSTed.
"""

from __future__ import annotations

import time
from typing import Any

from obsalt.client import ObsaltClient
from obsalt.domain.enums import Provider
from obsalt.session import VoiceCall, _BoundSpan, _BoundTurn

try:  # pragma: no cover - exercised only when pipecat is installed
    from pipecat.observers.base_observer import BaseObserver as _PipecatBaseObserver
except ImportError:  # pragma: no cover

    class _PipecatBaseObserver:  # type: ignore[no-redef]
        pass


def _frame_name(frame: Any) -> str:
    return type(frame).__name__


class ObsaltObserver(_PipecatBaseObserver):
    """Record a Pipecat-shaped pipeline into a ``VoiceCall``.

    Usage::

        observer = ObsaltObserver.start(
            call_id=room_id,
            workspace_id="acme",
            agent_id="support",
            client=ObsaltClient(api_key="secret"),
        )
        task = PipelineTask(pipeline, observers=[observer])
        # ... run ...
        observer.close()
    """

    def __init__(
        self,
        *,
        call_id: str,
        workspace_id: str,
        agent_id: str,
        client: ObsaltClient | None = None,
        provider: Provider | str = Provider.NATIVE,
        system_prompt: str = "",
        llm_model: str | None = None,
        stt_provider: str = "unknown",
        tts_provider: str = "unknown",
        ingest_on_exit: bool | None = None,
    ) -> None:
        super().__init__()
        self._call_kwargs = dict(
            call_id=call_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            client=client,
            provider=provider,
            system_prompt=system_prompt,
            ingest_on_exit=ingest_on_exit,
        )
        self.llm_model = llm_model
        self.stt_provider = stt_provider
        self.tts_provider = tts_provider
        self.call: VoiceCall | None = None
        self._turn_index = 0
        self._user_speech_t0: float | None = None
        self._agent_turn: _BoundTurn | None = None
        self._llm_span: _BoundSpan | None = None
        self._tts_span: _BoundSpan | None = None
        self._closed = False

    @classmethod
    def start(cls, **kwargs: Any) -> ObsaltObserver:
        observer = cls(**kwargs)
        observer.open()
        return observer

    def open(self) -> VoiceCall:
        if self.call is None:
            self.call = VoiceCall.start(**self._call_kwargs)
            self.call.__enter__()
        return self.call

    async def on_push_frame(self, data: Any) -> None:
        frame = getattr(data, "frame", data)
        self.handle_frame(frame)

    def handle_frame(self, frame: Any) -> None:
        if self.call is None:
            self.open()
        name = _frame_name(frame)
        text = getattr(frame, "text", None) or getattr(frame, "content", None) or ""

        if name in {"UserStoppedSpeakingFrame", "VADUserStoppedSpeakingFrame"}:
            self._user_speech_t0 = time.perf_counter()
            return
        if name in {"TranscriptionFrame"}:
            stt_ms = None
            if self._user_speech_t0 is not None:
                stt_ms = (time.perf_counter() - self._user_speech_t0) * 1000.0
                self._user_speech_t0 = None
            self._close_agent_turn()
            assert self.call is not None
            with self.call.turn(self._turn_index, "user", text=str(text or "")) as turn:
                with turn.stt(self.stt_provider) as stt:
                    if stt_ms is not None:
                        stt.set(latency_ms=stt_ms)
                    conf = getattr(frame, "confidence", None)
                    if conf is not None:
                        stt.set(confidence=float(conf))
            self._turn_index += 1
            return
        if name in {"LLMFullResponseStartFrame", "LLMMessagesFrame"}:
            self._open_agent_turn()
            assert self._agent_turn is not None
            if self._llm_span is None:
                self._llm_span = self._agent_turn.llm(self.llm_model).__enter__()
            return
        if name in {"LLMTextFrame", "TextFrame", "LLMFullResponseTextFrame"}:
            self._open_agent_turn()
            assert self._agent_turn is not None
            if text:
                self._agent_turn.text = (self._agent_turn.text + str(text)).strip()
            return
        if name in {"LLMFullResponseEndFrame"}:
            if self._llm_span is not None:
                self._llm_span.__exit__(None, None, None)
                self._llm_span = None
            return
        if name in {"TTSStartedFrame", "BotStartedSpeakingFrame"}:
            self._open_agent_turn()
            assert self._agent_turn is not None
            if self._tts_span is None:
                self._tts_span = self._agent_turn.tts(self.tts_provider).__enter__()
            return
        if name in {"TTSStoppedFrame", "BotStoppedSpeakingFrame"}:
            if self._tts_span is not None:
                self._tts_span.__exit__(None, None, None)
                self._tts_span = None
            self._close_agent_turn()
            return
        if name in {"FunctionCallInProgressFrame"}:
            self._open_agent_turn()
            assert self._agent_turn is not None
            fn = getattr(frame, "function_name", None) or getattr(frame, "tool_name", None) or "tool"
            args = getattr(frame, "arguments", None)
            tool_id = getattr(frame, "tool_call_id", None)
            if self._llm_span is None:
                self._llm_span = self._agent_turn.llm(self.llm_model).__enter__()
            self._pending_tool = self._llm_span.tool(str(fn), arguments=args, call_id=tool_id).__enter__()
            return
        if name in {"FunctionCallResultFrame"}:
            pending = getattr(self, "_pending_tool", None)
            if pending is not None:
                result = getattr(frame, "result", None)
                pending.set_result(result)
                pending.__exit__(None, None, None)
                self._pending_tool = None
            return
        if name in {"EndFrame", "CancelFrame", "StopFrame"}:
            self.close()
            return
        if name in {"ErrorFrame", "ErrorLogFrame"}:
            if self.call is not None:
                err = getattr(frame, "error", None) or getattr(frame, "message", None) or "error"
                self.call.set_call_outcome(status="error", error_type=str(err)[:80])
            return

    def _open_agent_turn(self) -> None:
        if self._agent_turn is not None:
            return
        assert self.call is not None
        self._agent_turn = self.call.turn(self._turn_index, "agent", text="").__enter__()

    def _close_agent_turn(self) -> None:
        if self._llm_span is not None:
            self._llm_span.__exit__(None, None, None)
            self._llm_span = None
        if self._tts_span is not None:
            self._tts_span.__exit__(None, None, None)
            self._tts_span = None
        pending = getattr(self, "_pending_tool", None)
        if pending is not None:
            pending.__exit__(None, None, None)
            self._pending_tool = None
        if self._agent_turn is not None:
            self._agent_turn.__exit__(None, None, None)
            self._agent_turn = None
            self._turn_index += 1

    def close(self, hangup_reason: str = "completed") -> None:
        if self._closed:
            return
        self._closed = True
        self._close_agent_turn()
        if self.call is not None:
            self.call.set_call_outcome(status="ended")
            if self.call._client is not None and not self.call._sent:
                self.call.send(hangup_reason=hangup_reason)
            self.call.__exit__(None, None, None)

    def __enter__(self) -> ObsaltObserver:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
