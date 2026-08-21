"""Record a call in-process with no OpenTelemetry and POST evidence to obsalt serve."""

from __future__ import annotations

from obsalt import CallRecorder, ObsaltClient

rec = CallRecorder(provider="openai-realtime", call_id="session-1", agent_id="concierge")
with rec.turn("user", "book Friday") as turn:
    turn.stt_ms = 120
with rec.tool("create_booking", {"night": "Friday"}) as tool:
    tool.set_result({"confirmation": "HTL-1"})
with rec.turn("assistant", "Booked HTL-1") as turn:
    turn.llm_ms = 300
    turn.tts_ms = 90

client = ObsaltClient(base_url="http://127.0.0.1:8080", api_key="change-me")
result = rec.send(client)
print(result)
print(client.get_call(result["call_id"])["transcript_text"])
