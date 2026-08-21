# obsalt

Production-grade observability for voice AI. Purpose-built for **Vapi**, **Retell**, **Bland**, and **OpenAI Realtime**.

Helicone and Langfuse see prompts and responses. Voice agents have transcripts, audio, function calls, latency budgets, and hangup signals. obsalt sees all of it.

## Why not generic LLM tracing?

A voice turn is not a chat completion. Time-to-first-audio is STT + endpointing + LLM TTFT + TTS TTFB. A user hangup after a failed tool is a lost customer, not an HTTP 200. An agent saying "I've refunded order ORD-99999" after `lookup_order` returned `not_found` is a hallucination, not a successful generation.

OpenTelemetry is the export plane. obsalt is the voice-specific data model, provider adapters, and analysis engines that generic GenAI instrumentation does not have.

## Architecture

```
 Vapi / Retell / Bland webhooks          OpenAI Realtime sidecar / SDK
              │                                      │
              ▼                                      ▼
     POST /v1/ingest/{provider}              POST /v1/ingest/native
              │                                      │
              └──────────────┬───────────────────────┘
                             ▼
                    AdapterRegistry
                    (normalize → CanonicalCall)
                             ▼
                    IngestPipeline.finalize
          ┌──────────┬──────────┬──────────┬──────────┐
          ▼          ▼          ▼          ▼          ▼
      latency    hangup     tools    hallucination  evals
      STT/LLM    taxonomy   shape     claim         heuristic
      TTS/TTFA   loss score retries   grounding     or LLM judge
          └──────────┴──────────┴──────────┴──────────┘
                             ▼
              MemoryStore + hybrid search index
                             ▼
          OpenTelemetry spans & metrics (OTLP)
          voice.*  +  gen_ai.*  conventions
```

### Canonical call

Every provider is mapped onto one model:

| Field | What it captures |
| --- | --- |
| `turns[]` | Speaker, transcript, per-turn STT / LLM / TTS / time-to-first-audio |
| `tools[]` | Name, payload **shape** (not secrets), retries, success, time-to-tool |
| `latency_samples[]` | Raw samples so agent P50/P95 are computed from data, not averaged P50s |
| `hangup` | Normalized reason + party + customer-loss score |
| `hallucinations[]` | Ungrounded prices, IDs, phantom tool success, policy, commitments |
| `evals[]` | Rubrics written in plain English |
| `grounding` | System prompt, knowledge, tool results used as the fact base |

### OpenTelemetry

obsalt emits OTel GenAI attributes where they apply (`gen_ai.operation.name=invoke_agent`, `execute_tool {name}`) and **voice extensions** where GenAI conventions stop:

- Spans: `voice.call`, `voice.turn`, `voice.stt`, `voice.llm`, `voice.tts`, `execute_tool {tool}`
- Metrics: `voice.latency.{stt,llm,tts,e2e,ttfa}`, `voice.hangup.count`, `voice.tool.invocations`, `voice.hallucination.count`, `voice.eval.score`
- Export: set `OBSALT_OTLP_ENDPOINT` (Tempo, Honeycomb, Grafana, Datadog, …)

Histogram buckets follow voice SLAs (200ms–8s), not generic HTTP buckets.

## Features

**Latency breakdown** — STT, LLM, TTS isolated per call. P50/P95 across every agent. Time-to-first-audio is first-class. Retell `latency.*.values` are ingested as raw samples. Vapi turn metadata / `performanceMetrics` are used when present. Bland live `LLM: 266ms` lines and transcript timestamp gaps fill the rest. OpenAI Realtime derives STT from `speech_stopped → transcription.completed` and TTFA from `speech_stopped → first audio delta`.

**Hangup analyzer** — Provider codes collapse into one taxonomy (`user_hangup`, `error_llm`, `voicemail`, …). Clusters group by reason, party, and last-utterance theme. Each cluster surfaces `lost_customer_call_id` — the call that most likely lost the customer.

**Hallucination detection** — Deterministic claim extraction against a grounding corpus (system prompt + knowledge + tool results + user text). Flags phantom tool success, fabricated IDs, ungrounded prices, policy claims, and private-knowledge leaks. Swap in an LLM judge later without changing the data model.

**Function call telemetry** — Every tool invocation: success rate, retries (consecutive failures of the same name), payload JSON-type shape, time-to-tool. PII in arguments is redacted; we store shape + hash, not emails.

**Custom evals** — `POST /v1/evals/rubrics` with a plain-English description. The default `HeuristicJudge` interprets words like “hallucination”, “latency”, “frustrated”. Production can swap `LlmJudge` (Claude/GPT JSON) on the same `Judge` protocol.

**Semantic search** — Hybrid index: hashing-trick embeddings + lexical overlap. `"customers asking about refunds"` matches meaning, not an exact keyword. Replace `HashingEmbedder` with `OpenAICompatEmbedder` when you have an API key.

## Quick start

```bash
pip install -e ".[dev]"
obsalt --port 8080
```

Point Vapi's Server URL at `POST /v1/ingest/vapi`, Retell's webhook at `/v1/ingest/retell`, Bland's post-call webhook at `/v1/ingest/bland`. For OpenAI Realtime, run a sidecar that stamps `t_ms` on each event and `POST /v1/ingest/openai-realtime`.

```bash
curl -s localhost:8080/v1/latency -H "X-API-Key: demo-secret"
curl -s localhost:8080/v1/hangups -H "X-API-Key: demo-secret"
curl -s "localhost:8080/v1/search?q=customers+asking+about+refunds" -H "X-API-Key: demo-secret"
```

Auth: `OBSALT_API_KEYS=acme:secret` (format `org:secret,...`). Webhook HMAC: `OBSALT_VAPI_SECRET`, `OBSALT_RETELL_SECRET`, `OBSALT_BLAND_SECRET`.

## SDK (Realtime and custom stacks)

```python
from obsalt.sdk import VoiceTracer
from obsalt.domain.enums import Speaker

tracer = VoiceTracer(provider="openai_realtime", call_id=session_id, agent_id="concierge")
with tracer.turn(Speaker.USER, transcript) as turn:
    turn.stt_ms = stt_ms
with tracer.tool("create_booking", {"night": "Friday"}) as tool:
    tool.set_result(result)
with tracer.turn(Speaker.AGENT, reply) as turn:
    turn.llm_ttft_ms, turn.tts_ttfb_ms = ttft, ttfb
payload = tracer.snapshot(hangup_reason="user_hangup")
# POST payload to /v1/ingest/native
```

## Tests

```bash
pytest
```

Coverage includes provider adapters (real webhook shapes), latency derivation, hangup taxonomy + clustering, hallucination grounding, tool retries/shapes, evals, hybrid search, OTel span/metric export, webhook signatures, org isolation, and an HTTP ingest → search → hangup path.

## Roadmap (not in this slice)

- Postgres + pgvector store implementing the same `Store` protocol
- Live dashboard (call waterfall: STT → LLM → TTS)
- Audio object storage and waveform alignment
- Claude/GPT judge wired to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
- Streaming OTLP from the Realtime WebSocket without a sidecar
