# obsalt

Observability for voice AI agents.

obsalt records each call as an OpenTelemetry **trace** (where time went) and an **evidence** record (what was said). It is both:

1. A **Python SDK** you call from agent code.
2. An **HTTP server** you point provider webhooks at.

It is **not** a dashboard. Traces and metrics go to Grafana, Tempo, Jaeger, Honeycomb, or any OpenTelemetry backend. The HTTP API is ingest plus lookup.

## Which integration?

| You run | Use |
| --- | --- |
| Your own STT / LLM / TTS loop (Pipecat, LiveKit, custom) | [Instrument the agent](docs/instrumentation.md) with `VoiceCallTracer` |
| **Vapi**, **Retell**, or **Bland** | [Run the ingest server](docs/ingest.md) and point the provider's webhook at it |
| OpenAI Realtime (event batch with `t_ms`) | `POST /v1/ingest/openai-realtime` |
| The loop, but you do not want OpenTelemetry in-process | [`CallRecorder`](docs/ingest.md#native-snapshots) → `POST /v1/ingest/native` |

The SDK emits spans as the conversation happens. The server reconstructs the **same** span tree from webhooks after the call, stores the transcript, and runs hangup / hallucination / eval analysis.

Live spans never land in the evidence store on their own. If you want both a live waterfall **and** search/evals, instrument with `VoiceCallTracer` and also POST a `CallRecorder` snapshot.

## Install

```bash
pip install -e ".[dev]"
```

Requires Python 3.11+.

## Quick start: instrument an agent

Use this when **your process** owns STT, the LLM, tools, and TTS.

```python
from obsalt.tracing import VoiceCallTracer, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
    with call.turn(0, "user") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80)
            with llm.tool("lookup_order") as tool:
                tool.set(execution_ms=120, status_code=200)
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

Do not put transcript text, prompts, or tool payloads on spans. Those belong in evidence. Full guide: [Instrument an agent](docs/instrumentation.md).

## Quick start: ingest Vapi / Retell / Bland

Use this when a **hosted platform** runs the call and you receive webhooks.

```bash
export OBSALT_OTLP_ENDPOINT=http://localhost:4318
export OBSALT_API_KEYS=acme:secret
obsalt --port 8080
```

| Provider | Webhook URL |
| --- | --- |
| Vapi | `POST /v1/ingest/vapi` |
| Retell | `POST /v1/ingest/retell` |
| Bland | `POST /v1/ingest/bland` |
| OpenAI Realtime | `POST /v1/ingest/openai-realtime` |

Auth: `X-API-Key: secret` (or `Authorization: Bearer secret`). HMAC: `OBSALT_VAPI_SECRET`, `OBSALT_RETELL_SECRET`, `OBSALT_BLAND_SECRET`.

A terminal webhook stores the call, rebuilds the span tree with historical timestamps, and runs analysis. Then:

```bash
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls/{id}
```

The waterfall still lives in Tempo. Full guide: [Ingest webhooks](docs/ingest.md).

## What you get

**Traces** — one `call.lifecycle` root per call, nested turns, STT attempts (including fallbacks), LLM, tools, TTS. Join every span to evidence with `call.id`. See [Trace model](docs/trace-model.md).

**Metrics** — Prometheus-safe histograms and counters. Never `call_id` as a label. See [Metrics and Grafana](docs/grafana.md).

**Evidence** — transcript, recording URL, tool payload *shapes* (not secrets), hangup taxonomy, hallucination flags, eval scores. Query via the [HTTP API](docs/api.md).

**Why not Langfuse / generic GenAI tracing?** A voice turn is not a chat completion. Time-to-first-audio is VAD + STT + LLM TTFT + TTS TTFB. LLM dashboards stay green while the caller hears silence because STT fell back, a tool timed out, or the transcript never finalized. OpenTelemetry GenAI conventions cover LLM and tools. They do not standardize STT, TTS, VAD, barge-in, or SIP.

## Documentation

| Guide | When to read it |
| --- | --- |
| [Architecture](docs/architecture.md) | How the SDK, server, traces, and evidence store fit together |
| [Instrument an agent](docs/instrumentation.md) | `VoiceCallTracer` in your process |
| [Ingest webhooks](docs/ingest.md) | Run the server; Vapi / Retell / Bland / Realtime / native |
| [Trace model](docs/trace-model.md) | Span names, attributes, PII rules |
| [HTTP API](docs/api.md) | Auth, endpoints, responses |
| [Metrics and Grafana](docs/grafana.md) | What belongs in Prometheus vs Tempo vs Loki |

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `OBSALT_OTLP_ENDPOINT` | OTLP HTTP base, e.g. `http://localhost:4318` | unset (no export) |
| `OBSALT_API_KEYS` | `org:secret,org2:secret2` | `demo:demo-secret` |
| `OBSALT_REQUIRE_AUTH` | Reject requests without a valid key | `false` |
| `OBSALT_VAPI_SECRET` | Vapi `x-vapi-secret` | empty (skip check) |
| `OBSALT_RETELL_SECRET` | Retell HMAC | empty (skip check) |
| `OBSALT_BLAND_SECRET` | Bland webhook secret | empty (skip check) |

Interactive API: `http://localhost:8080/docs`.

## Tests

```bash
python3 -m pytest
```

## Status

v0.1. Evidence is in-memory (a restart loses calls). Default evals are heuristic, not an LLM judge. There is no hosted UI.
