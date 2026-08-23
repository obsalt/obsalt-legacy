# obsalt documentation

obsalt is a self-hosted analytics and quality system for live AI voice
agents. You run it next to the stack you already have. Calls flow in.
You open a console to see what happened.

If you have never built a voice agent, start with
[Voice agents](concepts.md). If you already have one, start with
[Getting started](getting-started.md).

## Using obsalt

| Page | What it covers |
| --- | --- |
| [Voice agents](concepts.md) | What a voice agent is, how a call is built, and the words these docs use |
| [What obsalt does](product.md) | The six capabilities, how sources differ, and how obsalt compares to other tools |
| [Getting started](getting-started.md) | Install, sign in, confirm the empty console |
| [Connect a hosted platform](connect-hosted.md) | Vapi, Retell, ElevenLabs, or Cartesia webhooks |
| [Connect your own agent](connect-custom.md) | Pipecat, LiveKit, OpenAI Realtime, or Gemini Live via OTLP |
| [The console](console.md) | Screens, join view, and what each source can actually show |
| [Troubleshooting](troubleshooting.md) | Empty list, 401s, missing waterfall, evals, search |

## Running it

| Page | What it covers |
| --- | --- |
| [Operate](ops.md) | Health, replay, deletion, key rotation, retention, export |
| [Configuration](reference/configuration.md) | Every `OBSALT_*` setting |
| [CLI](reference/cli.md) | `serve`, `worker`, `doctor`, and the rest |
| [HTTP API](api.md) | `/v1` routes used by the console and scripts |
| [Security](reference/security.md) | Tenancy, redaction, deletion, egress |

## Reference

| Page | What it covers |
| --- | --- |
| [Glossary](reference/glossary.md) | Product and domain terms |
| [Domain](reference/domain.md) | Events, revisions, measurements, hangup reasons |
| [OTLP](reference/otlp.md) | Span names, attributes, and where transcript text may live |

## Changing the code

| Page | What it covers |
| --- | --- |
| [Developing](developing.md) | Repo layout, commands, adding a source |
| [Conventions](conventions.md) | Vocabulary and rules a formatter cannot see |
| [Architecture](architecture.md) | Pipeline, stores, and load-bearing rules |
| [Write a plugin](plugins.md) | Public plugin contract, fixtures, golden files |
| [AGENTS.md](../AGENTS.md) | One-screen version for coding agents |
| [llms.txt](llms.txt) | The same map, compacted for machines |

## In one paragraph

obsalt is a **service** (HTTP API + worker) with a **web console**. If
you build your own agent, it also exposes a small **`VoiceCall` tracer**.
It is not a hosted dashboard you log into, and it is not an SDK that
replaces your voice platform. Hosted platforms POST signed webhooks.
Custom agents emit OpenTelemetry (OTLP). The product is six capabilities:
latency, hangups, hallucination, tools, evals, and search.
