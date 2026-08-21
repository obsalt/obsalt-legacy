# Rewrite plan: Hamming 3-layer voice tracing

Source: [OpenTelemetry for AI Voice Agents](https://hamming.ai/resources/opentelemetry-voice-agents-tracing-guide) (June 2026).

The first slice treated obsalt as a **webhook warehouse that happens to emit OTel**. Hamming’s model is the inverse: **instrument the conversation as a trace**, keep evidence out of span attributes, and use Grafana/Tempo as the dashboard. Managed platforms (Vapi, Retell, Bland) still matter — but as **incomplete sources we reconstruct into the same tree**, not as the product.

## What Hamming says we got wrong

| First slice | Hamming |
| --- | --- |
| Root span `voice.call {agent}` | Root span `call.lifecycle` |
| Flat-ish `voice.stt` / `voice.llm` / `execute_tool` | Nested `turn.{i}` → `stt.transcription` → `stt.provider.{name}` (fallbacks), `llm.inference` → `llm.tool_call.{name}` |
| Transcripts and hangup text on spans | Join keys + **pointers**. Raw transcript/audio/tool payloads stay in an evidence store |
| Custom `voice.latency.*` metric names | Prometheus histograms `voice_response_latency_seconds{stage=...}` — **never** `call_id` as a label |
| Query API as the UI | Traces + metrics + Loki events. API is ingest + evidence lookup |
| Reconstruct spans after the call with “now” child timestamps | Historical `start_time`/`end` so the waterfall is the real call |
| OTel as an export afterthought | 3 layers: hierarchy, voice attributes, unified export with W3C `traceparent` |

Hamming’s skip filter still applies: if you only use Vapi’s dashboard and do not care about STT provider, fallbacks, or tool timing, you do not need this. The gap those dashboards leave **is** the product.

## Target architecture

```
 Live SDK / Pipecat / LiveKit          Vapi · Retell · Bland · Realtime webhooks
        │                                         │
        │  VoiceCallTracer (live spans)           │  reconstruct same tree
        └──────────────────┬──────────────────────┘
                           ▼
              call.lifecycle
                turn.{i}
                  vad.end_of_utterance
                  stt.transcription
                    stt.provider.{name}
                    stt.provider.fallback.{name}
                  llm.inference   (gen_ai.*)
                    llm.tool_call.{name}
                  tts.synthesis
                  audio.playout
                webhook.dispatch
                transcript.finalization
                evaluation.assertion_check
                           ▼
         OTLP ──► Tempo / Jaeger / Honeycomb     (traces)
         OTLP ──► Prometheus / Mimir             (low-cardinality metrics)
         JSON ──► Loki                           (searchable events)
         pointers ──► evidence store             (transcript, audio, payloads)
```

## Layer 1 — span hierarchy (required tests)

Every independently failing decision is a span. Parent/child must be real (`SpanContext`), not naming coincidence.

- Live `VoiceCallTracer` produces the tree in `tests/test_span_hierarchy.py`
- Provider ingest reconstructs the same names in `tests/test_otel.py`
- Reconstruction **must not** invent `stt.provider.fallback.*` when the payload has one hop

Optional cascade spans (`transcript.json_parse`, `transcript.merge.fallback`, `metric.score.write`) are only emitted when we have that signal — never as placeholders.

## Layer 2 — attributes (required tests)

The Hamming 12, plus join keys on **every** span: `call.id`, `workspace.id`, `agent.id`, `gen_ai.conversation.id`. `turn.index` on turn-scoped spans.

GenAI conventions on LLM/tool spans; Hamming names for STT/TTS/VAD/telephony (no stable OTel voice spec yet).

Default capture is reviewer-safe: no transcript, prompt, tool args, tool results, phone, email. Evidence pointers only: `evidence.transcript_id`, `evidence.recording_id`, `evidence.redaction_state=redacted`.

## Layer 3 — export

- Production: `BatchSpanProcessor` + OTLP (`OBSALT_OTLP_ENDPOINT`)
- Tests: `InMemorySpanExporter` / `InMemoryMetricReader` + `SimpleSpanProcessor`
- W3C `traceparent` inject/extract so a worker can continue a call started in another process (`tests/test_traceparent.py`)
- Metrics never include `call_id` labels (cardinality)
- Loki envelope `voice.turn.completed` keeps `canonical_call_id` in the JSON body (`tests/test_events.py`)

## Evidence vs telemetry

`CanonicalCall` stays, but as the **evidence packet**. Hangup clustering, hallucination, and evals run against evidence and emit `evaluation.assertion_check` children of the same `call.lifecycle`. The HTTP API is ingest + evidence lookup, not the dashboard.

## Docs to ship

- `docs/trace-model.md` — hierarchy, attributes, GenAI mapping
- `docs/instrumentation.md` — live SDK + `traceparent`
- `docs/provider-ingest.md` — how Vapi/Retell/Bland/Realtime become the same tree
- `docs/grafana.md` — metric/event/trace/evidence routing
