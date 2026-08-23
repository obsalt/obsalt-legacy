# Glossary

Short definitions. For a walkthrough of the domain, start at
[Voice agents](../concepts.md).

## Voice-agent terms

- **Voice agent** — Software that talks to a person on a live phone
  call or in a browser. Not a text chatbot.
- **Cascade** — The usual pipeline: speech-to-text → language model →
  text-to-speech.
- **Speech-to-speech (S2S)** — One model hears audio and produces
  audio (OpenAI Realtime, Gemini Live). No STT / LLM / TTS split.
- **STT** — Speech-to-text. Turns caller audio into words.
- **LLM** — The model that decides the next reply and may call tools.
- **TTS** — Text-to-speech. Turns the reply into audio.
- **Turn** — One caller utterance plus the agent’s reply.
- **Tool / function call** — An API the agent invokes mid-call
  (lookup, book, refund).
- **Webhook** — HTTP POST from a hosted platform to obsalt with a call
  payload.
- **OTLP** — OpenTelemetry protocol. Custom agents emit spans to
  `/v1/traces`. Not an obsalt server.
- **Hosted platform** — Vapi, Retell, ElevenLabs, Cartesia. They POST
  a webhook. You do not import `VoiceCall`.
- **Barge-in / interruption** — The caller spoke over the agent. Taken
  only from an explicit signal, never inferred from “user spoke after
  agent.”
- **TTFB / TTFA** — Time to first byte / time to first audio. Often
  sent as a duration without stage clocks.

## Product terms

- **Call** — One live conversation. The console row you click.
- **Chip** — A duration we *measured* but cannot place on a timeline
  (no start/end clocks). Not a waterfall bar.
- **Console** — The web UI at `/v1/ui`. Seven screens, no query builder.
- **Eval / rubric** — A plain-English quality question (“did the agent
  acknowledge frustration?”) judged against a call.
- **Grounding** — The prompt, knowledge, tool results, and caller text
  that hallucination detection is allowed to trust.
- **Hangup** — Why the call ended, in a stable taxonomy (`user_hangup`,
  `silence_timeout`, …), plus the original provider code.
- **Ingest key** — The secret in the webhook URL. Shown once. Per
  connection, per tenant.
- **Plugin** — A separately installed package that knows one source.
  Core ships none.
- **Provenance** — Where a number came from: the provider sent it, or
  obsalt derived it, or it is absent / unsupported / redacted / failed
  to decode.
- **Revision** — An immutable snapshot of a call. Late events create a
  new one. The console shows the active revision.
- **Waterfall** — Stage bars drawn only from real start and end
  timestamps. Hosted platforms usually cannot supply this.

## Implementation terms

- **AggregateMeasurement** — A provider-published statistic (p50, p95,
  …). Never mixed into sample-derived percentile rollups.
- **as_of_generation** — Serving generation for a fleet response. One
  generation per response; never mixed.
- **CallRevision** — A complete immutable snapshot. Postgres points at
  the active one. ClickHouse keeps every revision.
- **decoder_version** — Plugin identity plus revision, e.g. `vapi/3`.
- **delivery key** — Transport identity for dedupe. Different from call
  identity.
- **fact_id** — Deterministic identity of a source fact. Makes fold
  associative, commutative, and idempotent.
- **FidelityDeclaration** — What a plugin *can* produce. Actual call
  fidelity is derived from decode output.
- **ingest_key** — Per-connection identifier in the webhook URL; hashed
  at rest.
- **INTERVAL** — A `MeasurementPlacement` with real start and end. The
  only placement that may draw a waterfall bar.
- **NormalizedEvent** — A small fact a decoder emits (`TurnObserved`,
  `StageObserved`, …).
- **obsalt.pii.*** — The only place conversational content may appear
  on spans. Stripped by default.
- **org_id** — Tenant boundary. Comes only from authenticated
  credentials.
- **Provenance** — `provider_reported` or `obsalt_derived`, with source
  path or derivation.
- **RawEnvelope** — Verbatim inbound bytes plus inbox metadata.
- **SignalCoverage** — Per-call presence / absence / redaction / decode
  status for a signal, with a reason.
- **source_revision** — Ordered provider sequence, revision, or
  documented update timestamp. A content hash is not a source revision.
- **StageMeasurement** — An individual measured duration, with
  placement and provenance.
- **TimelineFidelity** — Derived from the measurements actually
  present: `stage_level`, `turn_level`, `message_level`, `call_level`,
  `none`.
- **unplaced** — A duration without timestamps. Shown as a chip, never
  as a span position.
- **VoiceCall** — A library class in your agent process. Emits OTLP.
  Not used for hosted-platform webhooks.

## Next

Still deciding: [What obsalt does](../product.md). Opening the UI:
[The console](../console.md). Changing code:
[Domain](domain.md).
