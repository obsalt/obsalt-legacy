# OTLP and semantic conventions

OTel GenAI conventions live in `open-telemetry/semantic-conventions-genai`
and are still marked `Development`. Adopt what is merged. Track what is
proposed. Own only what the spec explicitly leaves open.

How to point an exporter at obsalt:
[Connect your own agent](../connect-custom.md).

## Emit

Three tiers, in priority order:

1. **Merged and spec-blessed.** `gen_ai.conversation.id` as the
   preferred scoped call join key when a native conversation id exists;
   never synthesize it from a UUID, trace id, or content hash.
   `gen_ai.usage.audio.input_tokens` / `.output_tokens` /
   `.cache_read.input_tokens`. `gen_ai.output.type=speech`.
   `gen_ai.operation.name`, `gen_ai.provider.name`,
   `gen_ai.request.model`, `gen_ai.usage.{input,output}_tokens`,
   `gen_ai.tool.name`, `gen_ai.tool.call.id`, `error.type`.
2. **Proposed, behind a version flag.** `gen_ai.operation.name ∈
   {speech_to_text, text_to_speech, generate_live_content}`,
   `gen_ai.speech.voice`, `gen_ai.speech.input.language`,
   `gen_ai.agent.invocation.end_reason`, `gen_ai.token.modality`. Pin
   the exact proposal revision. Flag defaults off; flip when merged.
3. **`obsalt.*` for what the spec leaves open.** Barge-in vs client
   cancel, endpointing / VAD, STT confidence, perceived end-to-end
   latency, TTFA, transport legs, recording reference with channel
   layout, timeline fidelity, provenance.

## Accept

`gen_ai.*`, OpenInference (`openinference.span.kind`, `input.value`),
OpenLLMetry (`llm.*`, `traceloop.*`), LiveKit (`lk.*`), Pipecat
(`metrics.ttfb`, `turn.*`), `elevenlabs.*`, Azure Voice Live
(`gen_ai.voice.*`), and `obsalt.*`. Priority-ordered mapper registry,
`obsalt.*` highest.

Two collisions handled explicitly:

- LiveKit ships `gen_ai.usage.input_audio_tokens` while the merged spec
  says `gen_ai.usage.audio.input_tokens`. **Accept both.**
- Pipecat's docs say `gen_ai.system`; its code emits
  `gen_ai.provider.name`. Azure Voice Live uses `gen_ai.system`.
  **Accept both.** Instrument against code, not docs.

## Span names

Low-cardinality. Variable data belongs on attributes.

| Forbidden | Required | Attribute |
| --- | --- | --- |
| `turn.7` | `turn` | `turn.index=7` |
| `stt.provider.deepgram` | `stt.provider_attempt` | `stt.provider="deepgram"` |
| `stt.provider.fallback.azure` | `stt.provider_attempt` | `stt.provider="azure"`, `stt.fallback=true` |
| `llm.tool_call.create_booking` | `execute_tool` | `gen_ai.tool.name="create_booking"` |

## PII

Conversational content lives under `obsalt.pii.*`. Content never
appears in a span name. Default exporter config strips `obsalt.pii.*`.
Emitting it is opt-in via `OBSALT_EMIT_PII`. Received foreign OTLP may
place content in other attributes, so forwarding also applies a
configurable incoming attribute/content policy. The denylist is a test
assertion, not the mechanism.

## Receiver contract

- Parse with `opentelemetry-proto`. Do not hand-roll stubs.
- `Content-Type` must be `application/x-protobuf` or
  `application/json`; 415 otherwise.
- Respond with a serialized `ExportTraceServiceResponse`, not an empty
  200.
- Decode protobuf in a threadpool so it never blocks the event loop.
- Delivery identity is
  `(org_id, trace_id, span_id, content_fingerprint)`. Identical
  exporter retries dedupe. Different content for the same identity is a
  conflict unless a mapper supplies a comparable source revision.

## Export

Forward from the durable raw spine, preserving identity. obsalt-derived
metrics export alongside: `voice.call.duration`, `voice.stage.duration`
(by stage), `voice.turn.count`, `voice.interruption.count`,
`voice.tool.failures`, `voice.eval.failures`.

**Provider aggregate latency is exported as metrics, not spans.**
Vapi's `turnLatencyAverage` and Retell's `p50/p95/p99` become labelled
gauges / distribution summaries distinct from obsalt-computed
histograms. They do not become span widths. A waterfall bar still
requires real clocks.

## Vocabulary

| Layer | Word |
| --- | --- |
| Python package | `obsalt.otel` |
| Wire protocol / settings | `otlp` |
| HTTP path | `/v1/traces` |

## Next

Emitting from an agent: [Connect your own agent](../connect-custom.md).
Why clocks matter: [Architecture](../architecture.md).
