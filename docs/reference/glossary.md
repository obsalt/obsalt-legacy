# Glossary

## If you are not an engineer

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
- **Hosted platform** — Vapi, Retell, ElevenLabs, Cartesia. They POST a
  webhook. You do not import `VoiceCall`.
- **Ingest key** — The secret in the webhook URL. Shown once. Per
  connection, per tenant.
- **Provenance** — Where a number came from: the provider sent it, or
  obsalt derived it, or it is absent / unsupported / redacted / failed
  to decode.
- **Revision** — An immutable snapshot of a call. Late events create a
  new one. The console shows the active revision.
- **Waterfall** — Stage bars drawn only from real start and end
  timestamps. Hosted platforms usually cannot supply this.

## If you are going to change the code

- **AggregateMeasurement** — A provider-published statistic (p50, p95, …).
  Never mixed into sample-derived percentile rollups.
- **as_of_generation** — Serving generation for a fleet response. One
  generation per response; never mixed.
- **CallRevision** — A complete immutable snapshot. Postgres points at the
  active one. ClickHouse keeps every revision.
- **decoder_version** — Plugin identity plus revision, e.g. `vapi/3`.
- **delivery key** — Transport identity for dedupe. Different from call
  identity.
- **fact_id** — Deterministic identity of a source fact. Makes fold
  associative, commutative, and idempotent.
- **FidelityDeclaration** — What a plugin *can* produce. Actual call
  fidelity is derived from decode output.
- **Grounding** — System prompt, knowledge, tool results, and caller text
  used by hallucination detection.
- **ingest_key** — Per-connection identifier in the webhook URL; hashed at
  rest.
- **INTERVAL** — A `MeasurementPlacement` with real start and end. The only
  placement that may draw a waterfall bar.
- **NormalizedEvent** — A small fact a decoder emits (`TurnObserved`,
  `StageObserved`, …).
- **obsalt.pii.*** — The only place conversational content may appear on
  spans. Stripped by default.
- **org_id** — Tenant boundary. Comes only from authenticated credentials.
- **OTLP** — A protocol, not an obsalt server. The HTTP path is
  `/v1/traces`.
- **Provenance** — `provider_reported` or `obsalt_derived`, with source
  path or derivation.
- **RawEnvelope** — Verbatim inbound bytes plus inbox metadata.
- **SignalCoverage** — Per-call presence / absence / redaction / decode
  status for a signal, with a reason.
- **source_revision** — Ordered provider sequence, revision, or documented
  update timestamp. A content hash is not a source revision.
- **StageMeasurement** — An individual measured duration, with placement
  and provenance.
- **TimelineFidelity** — Derived from the measurements actually present:
  `stage_level`, `turn_level`, `message_level`, `call_level`, `none`.
- **unplaced** — A duration without timestamps. Shown as a chip, never as a
  span position.
- **VoiceCall** — A library class in your agent process. Emits OTLP. Not
  used for hosted-platform webhooks.
