from __future__ import annotations

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obsalt.integrations.pipecat import ObsaltObserver
from obsalt.tracing import conventions as c
from obsalt.tracing.setup import setup_tracing


class TranscriptionFrame:
    def __init__(self, text: str, confidence: float = 0.9) -> None:
        self.text = text
        self.confidence = confidence


class LLMFullResponseStartFrame:
    pass


class LLMTextFrame:
    def __init__(self, text: str) -> None:
        self.text = text


class LLMFullResponseEndFrame:
    pass


class TTSStartedFrame:
    pass


class TTSStoppedFrame:
    pass


class FunctionCallInProgressFrame:
    def __init__(self, function_name: str, arguments: dict, tool_call_id: str = "t1") -> None:
        self.function_name = function_name
        self.arguments = arguments
        self.tool_call_id = tool_call_id


class FunctionCallResultFrame:
    def __init__(self, result: dict) -> None:
        self.result = result


class EndFrame:
    pass


def test_observer_records_turns_tools_and_snapshot() -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(span_exporter=exporter, batch=False)
    observer = ObsaltObserver.start(
        call_id="pipe-1",
        workspace_id="acme",
        agent_id="concierge",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        llm_model="gpt-4o",
    )
    observer.handle_frame(TranscriptionFrame("book Friday", confidence=0.88))
    observer.handle_frame(LLMFullResponseStartFrame())
    observer.handle_frame(LLMTextFrame("Booked "))
    observer.handle_frame(LLMTextFrame("HTL-1"))
    observer.handle_frame(FunctionCallInProgressFrame("create_booking", {"night": "Friday"}))
    observer.handle_frame(FunctionCallResultFrame({"confirmation": "HTL-1"}))
    observer.handle_frame(LLMFullResponseEndFrame())
    observer.handle_frame(TTSStartedFrame())
    observer.handle_frame(TTSStoppedFrame())
    snapshot = observer.call.snapshot() if observer.call else {}
    observer.close()

    assert "book Friday" in snapshot["transcript_text"]
    assert any(t["speaker"] == "user" for t in snapshot["turns"])
    assert any(t["name"] == "create_booking" for t in snapshot["tools"])
    names = {s.name for s in exporter.get_finished_spans()}
    assert c.SPAN_CALL in names
    assert c.SPAN_STT in names
    assert c.SPAN_LLM in names
    assert any(n.startswith("llm.tool_call.") for n in names)
    assert c.SPAN_TTS in names
