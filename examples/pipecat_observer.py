"""Pipecat observer sketch. Install ``pipecat-ai`` to attach this to a PipelineTask.

    observer = ObsaltObserver.start(
        call_id=room.id,
        workspace_id="acme",
        agent_id="support",
        client=ObsaltClient(api_key="change-me"),
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        llm_model="gpt-4o",
    )
    task = PipelineTask(pipeline, observers=[observer])
    await task.queue_frames([EndFrame()])
    observer.close()
"""

from __future__ import annotations

from obsalt import ObsaltClient, setup_tracing
from obsalt.integrations.pipecat import ObsaltObserver

setup_tracing(otlp_endpoint="http://localhost:4318")


def build_observer(room_id: str) -> ObsaltObserver:
    return ObsaltObserver.start(
        call_id=room_id,
        workspace_id="acme",
        agent_id="support",
        client=ObsaltClient(api_key="change-me"),
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        llm_model="gpt-4o",
        system_prompt="Never invent order numbers.",
    )


if __name__ == "__main__":
    observer = build_observer("room-demo")

    class TranscriptionFrame:
        text = "book Friday"
        confidence = 0.9

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

    class EndFrame:
        pass

    observer.handle_frame(TranscriptionFrame())
    observer.handle_frame(LLMFullResponseStartFrame())
    observer.handle_frame(LLMTextFrame("Booked."))
    observer.handle_frame(LLMFullResponseEndFrame())
    observer.handle_frame(TTSStartedFrame())
    observer.handle_frame(TTSStoppedFrame())
    observer.handle_frame(EndFrame())
    print(observer.call.obsalt_call_id if observer.call else "closed")
