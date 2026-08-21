# Documentation

obsalt records each voice call as an OpenTelemetry **trace** (where time went) and an **evidence** record (what was said). You run it yourself. You already have — or will add — an OTLP backend if you want waterfalls.

## Start here

1. **[Choose a path](choose-a-path.md)** — hosted platform vs Pipecat / your loop. Do this before installing anything.
2. **[What you can see](what-you-see.md)** — Grafana (fleet), `/v1/ui` (one call), HTTP API. Transcripts are not span attributes.
3. **[Glossary](glossary.md)** — OTLP is a protocol. `VoiceCallTracer` is a Python class. `obsalt serve` is the HTTP server.
4. **[Getting started](getting-started.md)** — `obsalt init`, first fixture, first instrumented turn.

## Path A — hosted (Vapi / Retell / Bland)

The vendor owns the audio loop. Point a webhook at `obsalt serve`.

- [Providers](providers/index.md)
- [Vapi](providers/vapi.md) · [Retell](providers/retell.md) · [Bland](providers/bland.md)
- [Ingest](ingest.md)

## Path B — custom agents (Pipecat / LiveKit)

You own STT / LLM / TTS. Wrap the session with `VoiceCall`.

- [Custom agents](custom-agents.md)
- [Instrument an agent](instrumentation.md) — span API reference
- [Native snapshots](providers/native.md) — the JSON packet
- Pipecat observer: `obsalt.integrations.pipecat.ObsaltObserver`

OpenAI Realtime event batches: [guide](providers/openai-realtime.md).

## How it is put together

| Page | Contents |
| --- | --- |
| [Architecture](architecture.md) | Process hierarchy, traces vs evidence |
| [Data model](data-model.md) | `CanonicalCall` |
| [Data flow](data-flow.md) | Sequences for Path A and Path B |
| [Trace model](trace-model.md) | Span tree, join keys, PII rules |
| [Scenarios](scenarios.md) | Worked examples |

## Operate it

| Page | Contents |
| --- | --- |
| [HTTP API](api.md) | Auth, ingest, lookup, search, rollups |
| [Metrics and Grafana](grafana.md) | Prometheus vs Tempo vs Loki vs the per-call join view |
| [Configuration](configuration.md) | `obsalt.toml`, env, production checklist |
