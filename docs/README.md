# obsalt documentation

obsalt is a self-hosted analytics and quality system for live AI voice
agents. You run it next to the stack you already have. Calls flow in.
You open a console to see what happened.

Start at [What obsalt does](product.md), then [Start](start.md).

## Using obsalt

| Page | What it covers |
| --- | --- |
| [What obsalt does](product.md) | Six capabilities, two ingest paths, source fidelity |
| [Start](start.md) | Install, sign in, empty console, sample calls |
| [Connect a hosted platform](connect-hosted.md) | Vapi, Retell, ElevenLabs, Cartesia webhooks |
| [Connect your own agent](connect-custom.md) | Pipecat, LiveKit, Realtime, Gemini Live via OTLP |
| [The console](console.md) | Seven screens, join view, chips vs waterfall |
| [Operate](operate.md) | Health, replay, deletion, and what to do when it looks empty |

## Running it

| Page | What it covers |
| --- | --- |
| [HTTP API](api.md) | `/v1` routes used by the console and scripts |
| [Configuration](reference/configuration.md) | Every `OBSALT_*` setting |
| [CLI](reference/cli.md) | All `obsalt` commands and flags |
| [OTLP](reference/otlp.md) | Span names, attributes, PII |
| [Security](reference/security.md) | Tenancy, redaction, deletion, egress |

## Changing the code

| Page | What it covers |
| --- | --- |
| [Develop](develop.md) | Repo layout, commands, adding a source |
| [Architecture](architecture.md) | Pipeline, stores, diagrams, load-bearing rules |
| [Conventions](conventions.md) | Vocabulary and rules a formatter cannot see |
| [Write a plugin](plugins.md) | Public plugin contract |
| [AGENTS.md](../AGENTS.md) | One-screen version for coding agents |
| [llms.txt](llms.txt) | Compact machine map |

obsalt is a **service** (HTTP API + worker) with a **web console**. If
you own the agent process, it also exposes a thin **`VoiceCall` tracer**.
Hosted platforms POST signed webhooks. Custom agents emit OpenTelemetry
(OTLP). Core ships no providers.
