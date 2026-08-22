# obsalt documentation

obsalt is a self-hosted call analytics and quality system for AI voice agents.
This tree is the operator- and contributor-facing documentation for v2.

If you have five minutes, read the [product spec](product.md) and
[choose a path](guides/choose-a-path.md). If you have an hour, read
[architecture](architecture.md), [storage](storage.md), and [tenets](tenets.md).

## By audience

### Product and engineering leads

1. [Product spec](product.md) — what it is, who it is for, what it refuses to be
2. [Architecture](architecture.md) — how a call becomes a revision you can trust
3. [Decisions](decisions.md) — why this shape, and what was rejected

### Operators

1. [Getting started](guides/getting-started.md)
2. [Choose a path](guides/choose-a-path.md)
3. [Hosted webhook](guides/hosted-webhook.md) or [Custom agent](guides/custom-agent.md)
4. [Operate](guides/operate.md) — restore, deletion, rotation, retention, alerts
5. [Configuration](reference/configuration.md) · [HTTP API](reference/api.md) · [CLI](reference/cli.md)

### Plugin authors

1. [Write a plugin](guides/write-a-plugin.md)
2. [Testing](reference/testing.md)
3. [Providers](reference/providers.md) — fidelity, units, auth schemes
4. [Style](style.md)

### Contributors

1. [Contributing](contributing.md)
2. [Style](style.md)
3. [Tenets](tenets.md) — load-bearing claims; attack these first
4. [Domain model](reference/domain.md)

## Map

| Page | What it answers |
| --- | --- |
| [Product spec](product.md) | What obsalt is and is not |
| [Architecture](architecture.md) | Receive → decode → redact → assemble → analyze |
| [Storage](storage.md) | Why Postgres, ClickHouse, and object storage |
| [Tenets](tenets.md) | T1–T10, with the evidence that forced them |
| [Decisions](decisions.md) | ADRs, rejected alternatives, open questions |
| [Contributing](contributing.md) | Local setup, PR bar, roadmap |
| [Style](style.md) | Naming, imports, plugins, tests |
| [Getting started](guides/getting-started.md) | Install, compose, first call |
| [Choose a path](guides/choose-a-path.md) | Webhook vs OTLP |
| [Hosted webhook](guides/hosted-webhook.md) | Vapi, Retell, ElevenLabs, Cartesia |
| [Custom agent](guides/custom-agent.md) | Pipecat, LiveKit, Realtime, Gemini Live |
| [Write a plugin](guides/write-a-plugin.md) | Public contract, fixtures, golden files |
| [Operate](guides/operate.md) | Restore, deletion, rotation, retention |
| [HTTP API](reference/api.md) | `/v1` surface |
| [CLI](reference/cli.md) | `obsalt` commands |
| [Configuration](reference/configuration.md) | `OBSALT_*` settings |
| [Providers](reference/providers.md) | Fidelity, auth, units |
| [Domain](reference/domain.md) | Events, revisions, measurements |
| [OTLP](reference/otlp.md) | Span names, attributes, PII |
| [Security](reference/security.md) | Tenancy, redaction, retention |
| [Testing](reference/testing.md) | Conformance, fixtures, markers |
| [Glossary](reference/glossary.md) | Terms |

v0.1 docs (in-memory store, span synthesis, global webhook secrets) are obsolete.
The rewrite plan that specified v2 has been extracted into this tree.
