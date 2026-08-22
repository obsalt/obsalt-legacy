# obsalt

Self-hosted **call analytics and quality** for AI voice agents.

obsalt is the system you open when a call went wrong, and the system a product
owner opens to ask which agent is losing customers. It owns the call record and
the analysis on top of it. Traces still go to your OpenTelemetry backend.
obsalt does not try to be a better Tempo.

```
Hosted platform  ──signed webhook──►  obsalt  ──OTLP forward──►  Grafana / Tempo / Datadog
Custom agent     ──OTLP───────────►
```

## What it does

| Capability | What you actually get |
| --- | --- |
| **Latency breakdown** | Native pipeline stages, isolated per call. P50/P95 per agent. No invented waterfall for speech-to-speech systems. |
| **Hangup analyzer** | Clusters by why the call ended. Surfaces the call that lost the customer. |
| **Hallucination detection** | Flags agent claims not grounded in prompt, knowledge, tool results, or the caller. |
| **Function-call telemetry** | Every tool: success rate, retries, payload shape, time-to-tool. |
| **Custom evals** | Quality rubrics in plain English, judged by an LLM against sampled calls. |
| **Semantic search** | Find calls by meaning. "Customers asking about refunds" works. |

## What it is not

- Not a general APM. Infrastructure correlation stays in Grafana / Tempo / Datadog.
- Not a testing or simulation platform. obsalt observes production.
- Not a dashboard builder. It ships the views the six capabilities need.
- Not a prompt-management or agent-building tool.

## Who it is for

1. Teams on one hosted platform (Vapi, Retell, ElevenLabs, Cartesia) who want a
   call console they own, with analysis the provider dashboard does not do.
2. Teams building custom agents (Pipecat, LiveKit, OpenAI Realtime, Gemini Live)
   who want voice-aware analysis and still keep spans in their existing backend.

## 60-second start

```bash
git clone https://github.com/coder-with-a-bushido/obsalt.git
cd obsalt
python -m pip install -r requirements-dev.txt
docker compose up -d
obsalt init
obsalt serve
```

Open http://localhost:8080/v1/ui. Local bootstrap uses `OBSALT_BOOTSTRAP_API_KEY`
(default `dev-key`). Change it before any network-exposed deploy.

Connect a hosted provider, then point that provider at:

```
POST /v1/ingest/{provider}/{ingest_key}
```

`ingest_key` is a per-connection secret. Each tenant has its own credentials.
Decode runs in a worker. The webhook acknowledgement does not wait on analysis.

Full walkthrough: [Getting started](docs/guides/getting-started.md).
Which ingest path to use: [Choose a path](docs/guides/choose-a-path.md).

## Two ingest paths

| You run | obsalt receives | Guide |
| --- | --- | --- |
| Vapi, Retell, ElevenLabs, Cartesia | Signed webhook | [Hosted webhook](docs/guides/hosted-webhook.md) |
| Pipecat, LiveKit, OpenAI Realtime, Gemini Live | OTLP from the process | [Custom agent](docs/guides/custom-agent.md) |

Hosted platforms do not push standard OTLP to an arbitrary collector. Their
webhook is the ingest path that exists. Custom agents have real clocks; their
spans are forwarded with identity preserved.

A stage waterfall is drawn only from measurements with real start and end
timestamps. Vapi and Retell stage durations are stored as unplaced measurements
and shown as chips — never as invented span positions.

## Documentation

| I am… | Start here |
| --- | --- |
| Evaluating the product | [Product spec](docs/product.md) |
| Installing or operating it | [Getting started](docs/guides/getting-started.md) · [Operate](docs/guides/operate.md) |
| Connecting a provider | [Choose a path](docs/guides/choose-a-path.md) |
| Understanding the system | [Architecture](docs/architecture.md) · [Storage](docs/storage.md) |
| Writing a plugin | [Write a plugin](docs/guides/write-a-plugin.md) |
| Contributing | [Contributing](docs/contributing.md) · [Style](docs/style.md) |

The full index is [docs/README.md](docs/README.md).

## Repository

```
packages/obsalt            core: domain, ingest, assembly, analysis, API, UI
packages/obsalt-testkit    conformance tests for plugin authors
packages/obsalt-*          first-party source plugins (not bundled into core)
tests/                     unit / integration / blackbox / contract / conformance
docs/                      product, architecture, guides, reference
```

Core ships **no** providers. Install the plugins you need:

```bash
pip install "obsalt[vapi,retell]"
# or everything first-party:
pip install obsalt-providers-all
```

## Develop

```bash
make install
make test-unit          # fast path
make ci                 # lint + typecheck + full tests
```

## Status

v2.0. Clean rewrite; no compatibility with the unreleased v0.1 in-memory
prototype. Bland is not a committed first-party plugin. Deepgram is designed
for via `StreamSource` and is not implemented. Production-release drills
(restore-from-raw, delete-by-caller through backups, 1M-call load) are
documented and exercisable; they are the remaining release gate.

License: Apache-2.0
