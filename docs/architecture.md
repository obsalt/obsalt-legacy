# Architecture

obsalt has two jobs:

1. Turn a voice call into an OpenTelemetry **trace** you can open in Tempo, Jaeger, or Honeycomb.
2. Keep an **evidence** record of that call — transcript, tools, hangup, hallucinations, evals — that you look up over HTTP.

Those jobs share one data model. They do **not** share one process.

## The two surfaces

```
  Your agent (Pipecat, LiveKit, custom)
        │
        │  VoiceCallTracer   ← spans emitted live
        ▼
     OTLP ──► Tempo / Jaeger / Honeycomb
              Prometheus

  Vapi / Retell / Bland / Realtime / CallRecorder
        │
        │  POST /v1/ingest/{provider}
        ▼
     obsalt HTTP server
        ├── adapters → CanonicalCall
        ├── analysis (latency, hangup, tools, hallucinations, evals)
        ├── evidence store  ──► GET /v1/calls/{id}
        └── reconstructed spans ──► OTLP (same tree as the SDK)
```

| Surface | What it is | What it is not |
| --- | --- | --- |
| **SDK** (`VoiceCallTracer`) | A library. You wrap STT / LLM / TTS in your agent process. Spans export over OTLP. | It does not store transcripts. It does not run evals. |
| **Server** (`obsalt` CLI) | An HTTP service. It accepts webhooks, stores evidence, reconstructs spans, runs analysis. | It is not a UI. It does not place calls. |

Use the SDK when you own the audio loop. Use the server when a hosted platform owns it. Use both when you want a live waterfall **and** searchable evidence.

## What a call is

Every provider is mapped onto one object: `CanonicalCall`.

| Field | Holds |
| --- | --- |
| `turns[]` | Speaker, text, per-turn STT / LLM / TTS / time-to-first-audio |
| `tools[]` | Name, payload **shape** (JSON types, not values), retries, status |
| `latency_samples[]` | Raw milliseconds so P50/P95 are computed from data |
| `hangup` | Normalized reason + party + customer-loss score |
| `hallucinations[]` | Ungrounded prices, IDs, phantom tool success, policy, commitments |
| `evals[]` | Rubrics written in plain English |
| `grounding` | System prompt, knowledge, tool results used as the fact base |

On spans this call becomes `call.lifecycle` → `turn.{i}` → STT / LLM / TTS / tools. See [Trace model](trace-model.md).

`workspace.id` on every span is the tenant (`org_id`). `call.id` joins Tempo to `GET /v1/calls/{id}`.

## Evidence vs telemetry

| | Traces (OTLP) | Evidence (store) |
| --- | --- | --- |
| Purpose | “Where did the 1.8s go?” | “What did the agent say?” |
| Contents | Span names, timings, join keys, debug attributes | Transcript, recording URL, tool args (redacted), analysis |
| Backend | Tempo, Jaeger, Honeycomb | obsalt HTTP API (in-memory today) |
| PII | Forbidden by default | Stored, redacted where possible |

Spans carry pointers (`evidence.transcript_id`, `evidence.recording_id`), not the transcript itself. That is why a live-instrumented call is invisible to `/v1/search` until you also ingest a snapshot.

## Ingest pipeline

On a **terminal** webhook (end-of-call, `call_ended`, Bland post-call, native `final: true`):

1. The provider **adapter** parses the payload into a `CanonicalCall`.
2. Live events for the same provider call id are **merged** (idempotent).
3. `finalize` runs: latency derivation, tool retries, hangup taxonomy, hallucination flags, rubric evals.
4. The call is written to the **store** and indexed for search.
5. `emit_call_trace` rebuilds the span tree using the call’s real timestamps, not “now”.

Non-terminal events (Vapi `status-update`, Bland live `category=latency`) merge and return without analysis.

Adapters never invent STT fallbacks. One hop in the payload is one `stt.provider.{name}` child.

## Analysis engines

All of these run against evidence, then attach `evaluation.assertion_check` spans to the same `call.lifecycle` root.

- **Latency** — STT, LLM, TTS, time-to-first-audio. Turn gaps fill TTFA when the provider omitted it.
- **Hangup** — Provider codes collapse to one taxonomy. Clusters group by reason, party, and last-utterance theme, and surface `lost_customer_call_id`.
- **Tools** — Success rate, consecutive-failure retries, payload JSON-type shape, time-to-tool.
- **Hallucinations** — Deterministic claim checks against grounding (prompt + knowledge + tool results + user text).
- **Evals** — `HeuristicJudge` interprets plain-English rubrics (`hallucination`, `latency`, `frustrated`, …). Swap in `LlmJudge` later on the same `Judge` protocol.

## Metrics

Exported over OTLP when `OBSALT_OTLP_ENDPOINT` is set. Labels are low cardinality: `agent`, `environment`, `stage`, `outcome`, `tool_name`. **Never** `call_id`. See [Metrics and Grafana](grafana.md).

## What this repository does not include

- A web UI. Use Grafana / Tempo / your OTLP vendor.
- Durable storage. `MemoryStore` dies with the process.
- A voice platform. obsalt observes calls; it does not dial them.
- An LLM judge or embedding API by default. Those are swap-in types (`LlmJudge`, `OpenAICompatEmbedder`).
