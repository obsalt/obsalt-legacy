# Native snapshots

A **native snapshot** is a JSON document. It is obsalt’s own call envelope — the same *job* as a Vapi `end-of-call-report`, for when **you** own the audio loop.

Typed as `NativeSnapshot`. Built by `VoiceCall.snapshot()` / `CallRecorder.snapshot()`. POSTed to `POST /v1/ingest/native`.

## Why hosted providers do not send this

They already POST *their* JSON. Adapters turn that into `CanonicalCall`. A snapshot is how Path B produces the same `CanonicalCall` without a vendor.

```
Path A:  Vapi/Retell/Bland JSON  --adapter--> CanonicalCall
Path B:  this document           --NativeAdapter--> CanonicalCall
```

You should never expect a `CallRecorder` snapshot from the Vapi dashboard. [Choose a path](../choose-a-path.md).

## What it looks like

Minimal, annotated. A fuller fixture: [`tests/fixtures/native_snapshot.json`](../../tests/fixtures/native_snapshot.json).

```json
{
  "call_id": "room-42",
  "provider": "native",
  "agent_id": "support",
  "started_at": "2026-08-21T14:00:00+00:00",
  "ended_at": "2026-08-21T14:00:18+00:00",
  "duration_ms": 18000,
  "hangup_reason": "completed",
  "final": true,
  "spans_exported": false,
  "traceparent": null,
  "transcript_text": "user: book Friday\nagent: Booked HTL-1.",
  "grounding": {
    "system_prompt": "Rooms are $189. Confirmation codes come from the booking tool.",
    "knowledge": []
  },
  "turns": [
    {
      "index": 0,
      "speaker": "user",
      "text": "book Friday",
      "stt_ms": 120,
      "confidence": 0.91
    },
    {
      "index": 1,
      "speaker": "agent",
      "text": "Booked HTL-1.",
      "llm_ms": 300,
      "llm_ttft_ms": 220,
      "tts_ms": 90,
      "tts_ttfb_ms": 40,
      "time_to_first_audio_ms": 40
    }
  ],
  "tools": [
    {
      "id": "tool-1",
      "name": "create_booking",
      "duration_ms": 45,
      "status": "success",
      "payload_shape": { "email": "string", "night": "string" },
      "result_preview": "{'confirmation': 'HTL-1'}",
      "metadata": {
        "arguments": { "night": "Friday", "email": "<string:redacted>" }
      }
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `call_id` | **Your** id (room, SIP, session). Becomes `provider_call_id`. obsalt `call.id` is uuid5(org, provider, this) |
| `provider` | `native` unless you are wrapping another id (`openai_realtime`, …) |
| `final` | `true` (default) runs analysis. `false` merges as a live event |
| `spans_exported` | `true` when `VoiceCall` already sent OTLP. Server skips a second `call.lifecycle` |
| `traceparent` | W3C header so eval spans attach to the live root |
| `turns[].text` | Evidence. Never copied onto spans |
| `tools[].metadata.arguments` | Values, secrets redacted. `payload_shape` is types only |

`org_id` is **not** in the body. The API key is the tenant. `CallRecorder(org_id=...)` only affects the client-side uuid if you inspect `recorder.call.id` before send; the server recomputes from the key.

## VoiceCall (production Path B)

Prefer this over assembling `CallRecorder` and `VoiceCallTracer` yourself.

```python
from obsalt import VoiceCall, ObsaltClient, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")
client = ObsaltClient(api_key="secret")

with VoiceCall.start(call_id="room-42", workspace_id="acme", agent_id="support", client=client) as call:
    with call.turn(0, "user", text="book Friday") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(latency_ms=120, confidence=0.91)
```

The snapshot sent on exit has `spans_exported: true` and a `traceparent`. [Custom agents](../custom-agents.md).

## CallRecorder only (no OTel in-process)

The server reconstructs the waterfall after ingest (`spans_exported` stays false).

```python
from obsalt import CallRecorder, ObsaltClient

rec = CallRecorder(provider="native", call_id="room-42", agent_id="support")
with rec.turn("user", "book Friday") as turn:
    turn.stt_ms = 120
    turn.confidence = 0.91
with rec.tool("create_booking", {"night": "Friday", "email": "ada@example.com"}) as tool:
    tool.set_result({"confirmation": "HTL-1"})
with rec.turn("assistant", "Booked HTL-1.") as turn:
    turn.llm_ms = 300
    turn.tts_ms = 90

with ObsaltClient(base_url="http://127.0.0.1:8080", api_key="secret") as client:
    result = rec.send(client)
    print(client.get_call(result["call_id"])["transcript_text"])
```

`turn("assistant", …)` is accepted (`assistant` → agent). Tool values are redacted before the snapshot leaves the process.

Runnable: [examples/record_and_ingest.py](../../examples/record_and_ingest.py).

## When to use which class

| Goal | Class |
| --- | --- |
| Live Tempo **and** search / evals | `VoiceCall` + `client=` |
| Live Tempo only | `VoiceCall` without `client` (or `VoiceCallTracer`) |
| Search / evals, no OTel in-process | `CallRecorder` + server |
| Debug a vendor JSON file | `obsalt parse` (no SDK) |
