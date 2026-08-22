# Custom agent

Use this guide when your process owns the pipeline. obsalt receives OTLP,
persists the raw export, maps spans to `NormalizedEvent`s, and forwards the
original trace identity to your existing backend.

```
Your agent  --OTLP/HTTP-->  POST /v1/traces  -->  raw archive + inbox
                                            -->  durable forward --> Tempo / Datadog
```

Do not POST an obsalt-invented JSON snapshot. That was the v0.1 SDK shape
and it is gone.

## Configure the exporter

Point your OpenTelemetry exporter at obsalt with an ingest-scoped API key:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8080/v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=$OBSALT_BOOTSTRAP_API_KEY"
```

obsalt accepts `application/x-protobuf` and `application/json`. Gzip and
deflate are supported, with compressed and expanded size limits. Malformed
protobuf is 400, not 500. Transient capacity pressure is 503 — retry the
whole batch. A populated partial-success response is for permanently invalid
records; OTLP clients must not retry it.

Tenancy comes from the key. Span attributes such as `obsalt.org` may
corroborate. They cannot choose an organization. Mixed-org assertions in one
batch are rejected.

## VoiceCall

`VoiceCall` is a thin OpenTelemetry wrapper. Span names are low-cardinality.

```python
from obsalt import VoiceCall, setup_tracing

setup_tracing()

with VoiceCall.start(
    call_id="call-123",
    org_id="acme",
    agent_id="support-v3",
    conversation_id="conv-123",  # gen_ai.conversation.id when you have a native id
) as call:
    with call.turn(0, "user", "hi") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(**{"obsalt.stt.confidence": 0.92})
        with turn.llm("gpt-4.1"):
            pass
        with turn.tool("create_booking", "tool-1"):
            pass
        with turn.tts("cartesia"):
            pass

    # Speech-to-speech: do not emit STT/LLM/TTS.
    with call.user_input():
        pass
    with call.generation():
        pass
    with call.playout():
        pass
```

`org_id` is the tenant identifier. It is written to `obsalt.org` for
corroboration only.

## Span names

| Do not emit | Emit | Variable lives on |
| --- | --- | --- |
| `turn.7` | `turn` | `turn.index=7` |
| `stt.provider.deepgram` | `stt.provider_attempt` | `stt.provider="deepgram"` |
| `llm.tool_call.create_booking` | `execute_tool` | `gen_ai.tool.name="create_booking"` |

A 200-turn call must not mint 200 unique span names. Backends group by name.

Conversational content lives under `obsalt.pii.*`
(`obsalt.pii.user_transcript`, `obsalt.pii.tool.arguments`). Default export
strips that prefix. Content never appears in a span name.

Full convention table: [OTLP reference](../reference/otlp.md).

## Pipecat

A stock Pipecat app with `enable_tracing=True` and no obsalt-specific code
should produce a complete call with a true `stage_level` waterfall when the
spans carry real intervals. Install `obsalt-pipecat`. The mapper reads
`metrics.ttfb`, `turn.*`, and `gen_ai.provider.name` (Pipecat docs sometimes
say `gen_ai.system`; instrument against code, not docs).

## LiveKit

Install `obsalt-livekit`. Accept both `gen_ai.usage.audio.input_tokens`
(merged spec) and `gen_ai.usage.input_audio_tokens` (what LiveKit ships).

## OpenAI Realtime and Gemini Live

These are bidirectional streaming APIs with no post-call webhook. Use the
matching package (`obsalt-openai-realtime`, `obsalt-gemini-live`) and the
S2S span shape: `user_input`, `generation`, `playout`. Do not invent empty
STT / LLM / TTS stages. Barge-in comes from real signals, not from "agent
turn followed by any user speech."

## Forwarding

OTLP received from custom agents is forwarded from the durable raw spine
through a per-destination queue. Trace ids, span ids, parents, links,
resources, and measured timestamps are preserved. Destination failures do
not block other destinations and never fail the ingest acknowledgement.

Provider aggregate latency is exported as metrics, not spans.
