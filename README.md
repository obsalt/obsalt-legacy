# obsalt

Voice-agent observability using Hamming’s [3-layer OpenTelemetry model](https://hamming.ai/resources/opentelemetry-voice-agents-tracing-guide): a conversation-shaped span tree, voice-specific attributes, and one export plane (`traceparent` + OTLP).

This is **not** a dashboard product. Grafana / Tempo / Prometheus / Loki are the UI. obsalt is the voice span SDK, the provider-webhook reconstruction into that same tree, and an evidence store (transcripts, recordings, hangup/hallucination/eval analysis) joined by `call.id`.

Purpose-built for **Vapi**, **Retell**, **Bland**, OpenAI Realtime, and custom stacks (Pipecat, LiveKit).

## Why not Langfuse / generic GenAI tracing?

A voice turn is not a chat completion. Time-to-first-audio is VAD + STT + LLM TTFT + TTS TTFB. Hamming’s point: LLM dashboards stay green while users hear silence, because the failure was an STT fallback, a language-routing decision, or a transcript that never finalized.

OpenTelemetry GenAI conventions cover `llm.inference` and tools. They do **not** standardize STT, TTS, VAD, barge-in, or SIP. obsalt uses GenAI names on LLM/tool spans and Hamming’s voice names everywhere else.

## Three layers

```
 Live VoiceCallTracer          Vapi / Retell / Bland / Realtime webhooks
        │                                    │
        │  live spans                        │  reconstruct the same tree
        └────────────────┬───────────────────┘
                         ▼
              call.lifecycle
                turn.{i}
                  vad.end_of_utterance
                  stt.transcription
                    stt.provider.{name}
                    stt.provider.fallback.{name}
                  llm.inference          gen_ai.operation.name=chat
                    llm.tool_call.{name} gen_ai.operation.name=execute_tool
                  tts.synthesis
                  audio.playout
                webhook.dispatch
                transcript.finalization
                evaluation.assertion_check
                         ▼
         OTLP ──► Tempo / Jaeger / Honeycomb
         OTLP ──► Prometheus  (no call_id labels)
         JSON ──► Loki        (canonical_call_id in the body)
         pointers ──► evidence store
```

Join keys on every span: `call.id`, `workspace.id`, `agent.id`, `gen_ai.conversation.id`. Turn-scoped spans also carry `turn.index`.

**Hamming 12** (debugging attributes, not PII): `stt.provider` `stt.confidence` `stt.latency_ms` `llm.model` `llm.ttft_ms` `llm.tokens.input` `llm.tokens.output` `tts.provider` `tts.synthesis_ms` `tool.name` `tool.execution_ms` `call.duration_ms`.

Spans are reviewer-safe by default. Transcripts, prompts, tool arguments/results, emails, and phone numbers stay in the evidence packet. Spans carry `evidence.transcript_id` / `evidence.recording_id` / `evidence.redaction_state=redacted`.

Docs: [trace model](docs/trace-model.md) · [live instrumentation](docs/instrumentation.md) · [provider ingest](docs/provider-ingest.md) · [Grafana routing](docs/grafana.md) · [rewrite plan](docs/rewrite-plan.md)

## Quick start

```bash
pip install -e ".[dev]"
OBSALT_OTLP_ENDPOINT=http://localhost:4318 obsalt --port 8080
```

Point Vapi’s Server URL at `POST /v1/ingest/vapi`, Retell at `/v1/ingest/retell`, Bland at `/v1/ingest/bland`. For OpenAI Realtime, stamp `t_ms` on each event and `POST /v1/ingest/openai-realtime`.

Auth: `OBSALT_API_KEYS=acme:secret` (format `org:secret,...`). Webhook HMAC: `OBSALT_VAPI_SECRET`, `OBSALT_RETELL_SECRET`, `OBSALT_BLAND_SECRET`.

The HTTP API is ingest + evidence lookup (`/v1/calls/{id}`, search, hangups, latency rollups). The waterfall lives in Tempo.

## Live SDK (you own STT/LLM/TTS)

```python
from obsalt.tracing import VoiceCallTracer, inject_traceparent, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")  # BatchSpanProcessor

with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support") as call:
    headers = inject_traceparent({})  # W3C traceparent for the next process
    with call.turn(0, "user") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(confidence=0.91, latency_ms=412)
            with stt.provider_attempt("deepgram"):
                pass
        with turn.llm("gpt-4o", provider="openai") as llm:
            llm.set(ttft_ms=340, tokens_in=200, tokens_out=80, finish_reason="stop")
            with llm.tool("lookup_order") as tool:
                tool.set(execution_ms=120, status_code=200)
        with turn.tts("elevenlabs") as tts:
            tts.set(synthesis_ms=290, first_audio_ms=70)
    call.set_call_outcome(duration_ms=45_000, status="ended")
```

A worker continues the same trace:

```python
with VoiceCallTracer.start(call_id="c1", workspace_id="acme", agent_id="support", headers=headers) as call:
    with call.evaluate("grounded-claims") as ev:
        ev.set(**{"assertion.result": "pass", "assertion.score": 1.0})
```

Production uses `BatchSpanProcessor` (Hamming measures 1–3% added latency). Tests use `setup_tracing(..., batch=False)` with in-memory exporters.

## Evidence SDK (native ingest without owning OTel)

`VoiceTracer` still builds a `CanonicalCall` snapshot you can POST to `/v1/ingest/native`. The pipeline reconstructs the Hamming tree from that packet.

```python
from obsalt.sdk import VoiceTracer
from obsalt.domain.enums import Speaker

tracer = VoiceTracer(provider="openai_realtime", call_id=session_id, agent_id="concierge")
with tracer.turn(Speaker.USER, transcript) as turn:
    turn.stt_ms = stt_ms
with tracer.tool("create_booking", {"night": "Friday"}) as tool:
    tool.set_result(result)
payload = tracer.snapshot(hangup_reason="user_hangup")
```

## Analysis (same `call.lifecycle`)

After a terminal webhook, obsalt runs latency breakdown, hangup taxonomy, tool telemetry, hallucination detection, and English-language evals **against the evidence packet**, then attaches `evaluation.assertion_check` spans to the reconstructed root. Search stays on the evidence store, not on span attributes.

## Metrics

Prometheus-safe, **never** `call_id` as a label:

- `voice_calls_total{agent,environment,outcome}`
- `voice_response_latency_seconds{agent,environment,stage}`
- `voice_tool_failures_total{agent,tool_name,failure_type}`
- `voice_low_confidence_turns_total{agent,stt_provider}`
- `voice_assertion_failures_total{agent,assertion_type}`

## Tests

```bash
python3 -m pytest
```

Coverage includes Hamming span parenting, reviewer-safe attributes, W3C `traceparent` continuation, provider reconstruction with historical timestamps (no invented fallbacks), metric cardinality, Loki event envelopes, adapters, hangup/hallucination/evals, and hybrid search.

## Roadmap (not in this slice)

- Postgres + object storage for the evidence packet
- Tail sampling that does not drop silent quality degradations
- Claude/GPT judge wired to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
- Streaming OTLP from the Realtime WebSocket without a sidecar
