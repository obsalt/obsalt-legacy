# Documentation

obsalt records each voice call as an OpenTelemetry **trace** (where time went) and an **evidence** record (what was said). These pages assume you will run obsalt yourself and already have — or will add — an OTLP backend.

## Start here

1. [Getting started](getting-started.md) — install, `obsalt init`, first webhook, first instrumented turn.
2. Pick a path:
   - Hosted platform → [Providers](providers/index.md)
   - You own the audio loop → [Instrument an agent](instrumentation.md)
3. [Configuration](configuration.md) before you expose a port.

## How it is put together

| Page | Contents |
| --- | --- |
| [Architecture](architecture.md) | Client vs server, traces vs evidence, what is *not* obsalt |
| [Data model](data-model.md) | `CanonicalCall`, turns, tools, hangups, evals |
| [Data flow](data-flow.md) | Ingest pipeline, live merge, reconstruct, join keys |
| [Trace model](trace-model.md) | Span tree, attributes, PII rules |
| [Scenarios](scenarios.md) | End-to-end stories with the actual fields you would see |

## Operate it

| Page | Contents |
| --- | --- |
| [HTTP API](api.md) | Auth, ingest, lookup, search, rollups, evals |
| [Metrics and Grafana](grafana.md) | What belongs in Prometheus vs Tempo vs Loki vs evidence |
| [Ingest overview](ingest.md) | Running the server; native snapshots |

## Provider setup

Step-by-step dashboard instructions, headers, and what each payload can reconstruct:

- [Vapi](providers/vapi.md)
- [Retell](providers/retell.md)
- [Bland](providers/bland.md)
- [OpenAI Realtime](providers/openai-realtime.md)
- [Native snapshots](providers/native.md) (`CallRecorder` + `ObsaltClient`)
- [Custom agents](providers/custom-agent.md) (Pipecat, LiveKit, your loop)
