# Connect your own agent

Use this when **your process** owns the pipeline — Pipecat, LiveKit, OpenAI
Realtime, Gemini Live, or anything you wrap with `VoiceCall`. obsalt
receives OTLP. It does not invent a private JSON envelope for you to POST.

```
Your agent process
    │  OpenTelemetry exporter
    ▼
POST /v1/traces          +  X-API-Key: <ingest-scoped key>
    │  raw archive + inbox
    ├── durable forward --> your Tempo / Datadog / Grafana
    └── worker maps spans --> console
```

Do not also fire a hosted-platform webhook for the same call. You will get
two records that do not join, and you will not know which clock won.

## 1. Install the mapper

```bash
pip install obsalt-pipecat            # or obsalt-livekit
pip install obsalt-openai-realtime    # speech-to-speech
pip install obsalt-gemini-live
obsalt doctor
```

The mapper package is how obsalt **recognizes** your spans. The tracer in
your process is how those spans **exist**.

## 2. Point the exporter at obsalt

Tenancy comes from the API key, not from a span attribute. `obsalt.org` may
corroborate. It cannot choose an organization.

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8080/v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="X-API-Key=${OBSALT_BOOTSTRAP_API_KEY:-dev-key}"
```

obsalt accepts `application/x-protobuf` and `application/json`. Gzip and
deflate are fine, within size limits. Malformed protobuf is 400. Capacity
pressure is 503 — retry the whole batch. A partial-success body means
"these records are permanently invalid"; clients must not retry that.

If you prefer the helper:

```python
from obsalt import setup_tracing

setup_tracing(
    otlp_endpoint="http://localhost:8080",
    api_key="dev-key",
)
```

Pass the service origin. The helper appends `/v1/traces` and sets
`X-API-Key`. Pass `emit_pii=True` only if you really want transcripts on
exported spans; the default strips `obsalt.pii.*`.

## 3. Emit low-cardinality spans

`VoiceCall` is a thin OpenTelemetry wrapper. Names stay stable. Variables
go on attributes. A 200-turn call must not mint 200 unique span names —
backends group by name.

```python
from obsalt import VoiceCall, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:8080", api_key="dev-key")

with VoiceCall.start(
    call_id="call-123",
    org_id="local",          # corroborates the key; cannot pick another org
    agent_id="support",
    conversation_id="conv-123",  # gen_ai.conversation.id when you have a native id
) as call:
    with call.turn(0, "user", "I need a refund") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(**{"obsalt.stt.confidence": 0.92})
        with turn.llm("gpt-4.1"):
            pass
        with turn.tool("create_booking", "tool-1"):
            pass
        with turn.tts("cartesia"):
            pass
```

Speech-to-speech (Realtime, Gemini Live) has no STT / LLM / TTS split. Do
not emit empty cascade stages. Use the stages that exist:

```python
with call.user_input():
    pass
with call.generation():
    pass
with call.playout():
    pass
```

Barge-in is taken only from an explicit interruption signal, never from
"the user spoke after the agent."

| Do not emit | Emit | Variable lives on |
| --- | --- | --- |
| `turn.7` | `turn` | `turn.index=7` |
| `stt.provider.deepgram` | `stt.provider_attempt` | `stt.provider="deepgram"` |
| `llm.tool_call.create_booking` | `execute_tool` | `gen_ai.tool.name="create_booking"` |

Transcript text goes on `obsalt.pii.user_transcript` /
`obsalt.pii.agent_transcript`. Never in the span name. Full table:
[OTLP reference](reference/otlp.md).

## Pipecat

A stock Pipecat app with tracing on and **no** obsalt-specific code should
produce a complete call with a real stage waterfall when the spans carry
real intervals. Install `obsalt-pipecat`. The mapper reads `metrics.ttfb`,
`turn.*`, and `gen_ai.provider.name` (some Pipecat docs say
`gen_ai.system`; instrument against the code).

Point the exporter as in step 2. Place a live call. Open `/v1/ui`.

## LiveKit

Install `obsalt-livekit`. We accept both
`gen_ai.usage.audio.input_tokens` (merged spec) and
`gen_ai.usage.input_audio_tokens` (what LiveKit ships). Room / conversation
id becomes the call join key.

## OpenAI Realtime and Gemini Live

These APIs have no post-call webhook. Telemetry exists only in your
process. Install the matching package and emit `user_input` / `generation`
/ `playout`. The console will not invent a cascade you do not have.

```python
# optional: wrap the vendor client
from obsalt.plugin.host import plugin_by_name
from obsalt.plugin.types import SdkConfig

plugin = plugin_by_name("openai_realtime").plugin
wrapped = plugin.instrument(my_realtime_client, SdkConfig(otlp_endpoint="http://localhost:8080"))
```

## Forwarding

Received OTLP is forwarded from the durable raw spine, identity preserved
(trace id, span id, parent, timestamps). Destination failures do not fail
the ingest ack. Provider aggregate latency is exported as **metrics**, not
as span widths.

Next: [The console](console.md). If traces never appear:
[Troubleshooting](troubleshooting.md).
