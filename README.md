# obsalt

Self-hosted **call analytics and quality** for AI voice agents. obsalt owns the call
record and the analysis on top of it. Traces still go to your OpenTelemetry backend;
obsalt does not try to be a better Tempo.

The product is six capabilities: latency breakdown, hangup analyzer, hallucination
detection, function-call telemetry, custom evals, and semantic search.

## Install

Python 3.11+. Core ships **no providers**.

```bash
pip install obsalt obsalt-vapi obsalt-retell
# or: pip install "obsalt[vapi,retell]" once those extras resolve from an index
```

Supported path:

```bash
docker compose up -d
obsalt serve
```

`obsalt demo` launches the same stack with a loud not-for-production banner. There is
no SQLite / Postgres-only production mode.

## Two sources

Hosted platforms (Vapi, Retell, ElevenLabs, Cartesia) POST signed webhooks:

```
POST /v1/ingest/{provider}/{ingest_key}
```

`ingest_key` is a per-connection secret so each tenant has its own provider credentials.
Decode runs in a worker. The webhook acknowledgement does not wait on analysis.

Custom agents (Pipecat, LiveKit, OpenAI Realtime, Gemini Live) emit OTLP from the
process. `VoiceCall` is a thin tracer with low-cardinality span names (`turn`,
`stt.provider_attempt`, `execute_tool`). Conversational content lives under
`obsalt.pii.*` and is stripped by default.

## What you look at

Open `/v1/ui` for the call list and the per-call join view: timeline on the left,
transcript on the right, and a provenance panel that says whether each signal was
provider-reported, derived, unsupported, redacted, absent, or failed to decode.

A stage waterfall is drawn only from measurements with real start and end timestamps.
Vapi and Retell stage durations are stored as unplaced measurements and shown as chips.

## Documentation

Start with [docs/README.md](docs/README.md). The architecture this tree implements is
[docs/rewrite-plan.md](docs/rewrite-plan.md).

## Tests

```bash
pip install -e packages/obsalt -e packages/obsalt-testkit -e packages/obsalt-example \
  -e packages/obsalt-vapi -e packages/obsalt-retell
pytest
```

## Status

v2.0. Clean rewrite; no compatibility with the unreleased v0.1 in-memory prototype.
Bland is not a committed first-party plugin. Deepgram is designed for via `StreamSource`
and is not implemented.
