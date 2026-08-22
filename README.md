# obsalt

Self-hosted **call analytics and quality** for AI voice agents. obsalt owns the
call record and the analysis on top of it.

It is the thing an engineer opens when a call went wrong, and the thing a
product owner opens to ask which agent is losing customers.

## What it delivers

| Capability | What you actually get |
| --- | --- |
| Latency breakdown | Native pipeline stages, isolated per call when the source supplies them. P50/P95 per agent. Cascade stages are never invented for speech-to-speech systems. |
| Hangup analyzer | Cluster calls by why they ended. Surface the call that lost the customer. |
| Hallucination detection | Flag agent claims not grounded in prompt, knowledge, tool results, or the caller. |
| Function call telemetry | Every tool invocation: success, retries, payload shape, time-to-tool. |
| Custom evals | Plain-English rubrics, judged by an LLM on a sampled or triggered subset. |
| Semantic search | Find calls by meaning. |

A timeline is only drawn where real timestamps exist. Durations without
timestamps are measurements — never span positions.

## What it is not

Not a general APM. Not a testing platform. Not a dashboard builder. Not a
prompt manager. Fleet infrastructure correlation is a link out to your OTLP
backend.

## Packages

Core ships **no providers**. First-party plugins use the same public
`obsalt.plugins` entry-point as third-party ones.

```
pip install "obsalt[vapi,retell]"
docker compose up
```

```
obsalt                      core
obsalt-testkit              conformance tests for plugin authors
obsalt-vapi / obsalt-retell / obsalt-elevenlabs / obsalt-cartesia
obsalt-openai-realtime / obsalt-gemini-live
obsalt-pipecat / obsalt-livekit
```

Plugins are trusted, operator-installed code. They are not a security sandbox.

## Ingest

```
POST /v1/ingest/{provider}/{ingest_key}
POST /v1/traces
```

`ingest_key` is a per-connection identifier. Tenant identity comes only from
authenticated credentials — never from payload fields.

Webhook authentication is a plugin capability and **fails closed** when
credentials are missing.

## Storage

Postgres (inbox, tenants, active revisions, search) + ClickHouse (immutable
call revisions and measurements) + object storage (raw payloads, evidence).
`docker compose up` is the supported path. `obsalt demo` is an ephemeral stack
with a loud not-for-production banner. There is no SQLite or Postgres-only
production mode.

Raw payloads are unredacted, encrypted, and short-lived (default 30 days).
Queryable content is redacted at one choke point before it is stored.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The rewrite plan that this tree implements is [`docs/rewrite-plan.md`](docs/rewrite-plan.md).
