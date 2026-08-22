# Product spec

obsalt is a self-hosted call analytics and quality system for AI voice agents.
It owns the call record and the analysis on top of it. It is the thing an
engineer opens when a call went wrong, and the thing a product owner opens to
ask which agent is losing customers.

Every other design decision depends on this definition. If a feature does not
make one of the six capabilities true, it is out of scope.

## The six capabilities

| Capability | What it must actually deliver |
| --- | --- |
| **Latency breakdown** | Native pipeline stages isolated per call where the source supplies them. P50/P95 per agent across the fleet, without inventing cascade stages for speech-to-speech systems. |
| **Hangup analyzer** | Cluster calls by why they ended. Surface the call that lost the customer. |
| **Hallucination detection** | Flag agent claims not grounded in prompt, knowledge, tool results, or the caller. |
| **Function-call telemetry** | Every tool invocation: success rate, retries, payload shape, time-to-tool. |
| **Custom evals** | Quality rubrics in plain English, judged by an LLM against every sampled call. |
| **Semantic search** | Find calls by meaning. "Customers asking about refunds" works. |

Latency and tool metrics can be graphed in Grafana. The product as a whole
cannot. Hangup clusters, hallucination flags, eval results, semantic search, and
the per-call transcript-plus-timing join are domain-specific views over a
domain-specific aggregate. So obsalt ships a real UI, scoped to exactly those
surfaces.

## What you look at

| Surface | Purpose |
| --- | --- |
| **Call list** | Filter by agent, outcome, time, latency threshold, flag, eval result. |
| **Call detail** | Timeline on the left, transcript on the right, tools, flags, evals, and an explicit provenance/coverage panel. |
| **Latency** | Stage distributions and percentiles, by agent, over time. |
| **Hangups** | Clusters with drill-through. |
| **Quality** | Eval results and hallucination flags, with a review queue. |
| **Search** | Semantic + filtered. |
| **Settings** | Provider connections, rubrics, retention, plugins, keys. |

No general charting. No custom dashboards. Fleet-wide infrastructure
correlation is a link out to your OTLP backend.

The **provenance panel** on call detail is the differentiating surface. For
every signal it shows reported values with source path, derived values with
derivation, and coverage status — absent, unsupported, redacted, or decode
failed — with a reason. Examples: "Retell does not timestamp tool utterances"
and "Vapi reports stage durations without timestamps, so no stage waterfall is
drawn."

| Question | Where |
| --- | --- |
| Where did time go **and** what was said? | `/v1/ui` per-call join + provenance panel |
| Fleet waterfalls / P95 of **real** spans | Your Tempo / Grafana |
| Hangup clusters, evals, search | obsalt HTTP API and UI |

obsalt-derived metrics (`voice.call.duration`, `voice.stage.duration`, …)
export alongside forwarded OTLP. Provider aggregate latency (Retell p50/p95) is
exported as labelled gauges, **not** as span widths.

## Who it is for

1. **Teams on one hosted platform** (Vapi, Retell, ElevenLabs, Cartesia) who
   want a call console they own, with analysis their provider's dashboard does
   not do.
2. **Teams building custom agents** (Pipecat, LiveKit, OpenAI Realtime, Gemini
   Live) who want voice-aware call analysis and want their spans in their
   existing observability backend.

Multi-workspace operation is a **constraint** — the data model, auth, and
per-tenant provider credentials must be correct from day one — but
agency/reseller features are not a v2 goal.

## What obsalt is not

Stating this precisely is what makes the UI and storage scope decidable.

- **Not a general APM or tracing backend.** We export OTLP to Grafana / Tempo /
  Datadog / Honeycomb. We do not try to be a better Tempo.
- **Not a testing or simulation platform.** Generating synthetic callers, load
  testing, and pre-launch regression suites are Hamming / Coval / Cekura
  territory. obsalt observes production. Production-to-test replay is a
  plausible later adjacency, explicitly out of v2 scope.
- **Not a dashboard builder.** We ship the views the six capabilities need, not
  a query builder.
- **Not a prompt management or agent-building tool.**

## Explicit non-goals for v2

Simulation and synthetic testing. Production-to-test replay. Custom dashboard
building. Non-voice channels. A hosted control plane. Deepgram (the
`StreamSource` contract is declared so it is additive later). Bland as a
first-party plugin (see [decisions](decisions.md#open-questions)).

## Two sources, one product

Hosted platforms POST signed webhooks. Custom agents emit OTLP from the
process. Both become the same `CallRevision`. Mixing both on the same call is
almost always a mistake.

See [Choose a path](guides/choose-a-path.md).
