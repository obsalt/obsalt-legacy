# obsalt documentation

Start at the [root README](../README.md) if you have not already.

This tree has two front doors. Use the one that matches why you are here.

## I decide what we build (product, founder, QA lead)

1. [Product guide](product.md) — what it is, the six capabilities, what
   a week of adoption looks like, vs Grafana / Hamming / the vendor
   dashboard
2. [The console](console.md) — what you will **actually** see, provider
   by provider. Read this before you expect a Vapi waterfall.

Then hand the install to an engineer, or keep going with Getting started.

## I am going to run it

1. [Getting started](getting-started.md) — install, compose, sign in
2. Then **one** of:
   - [Connect a hosted platform](connect-hosted.md) — Vapi, Retell, ElevenLabs, Cartesia
   - [Connect your own agent](connect-custom.md) — Pipecat, LiveKit, Realtime, Gemini Live
3. [The console](console.md) — join view, provenance, fidelity
4. [Troubleshooting](troubleshooting.md) — empty list, 401s, missing waterfall

That is the whole end-to-end path.

## I am going to operate it

- [Operate](ops.md) — health, retention, deletion, rotation
- [Configuration](reference/configuration.md) — every `OBSALT_*` setting
- [CLI](reference/cli.md) — `obsalt serve`, `worker`, `doctor`, …
- [HTTP API](api.md) — `/v1` for scripts and the console
- [Security](reference/security.md) — tenancy, redaction, egress

## I am going to change it

- [Conventions](conventions.md) — vocabulary, tenets, tests, docs duty
- [Developing](developing.md) — repo layout, PR bar, adding a source
- [Architecture](architecture.md) — pipeline, stores, the rules we will not break
- [Write a plugin](plugins.md) — public contract, fixtures, golden files
- [Domain](reference/domain.md) — events, revisions, measurements
- [OTLP](reference/otlp.md) — span names, attributes, PII
- [Glossary](reference/glossary.md)
- [AGENTS.md](../AGENTS.md) — one-screen version for coding agents

## What this is, in one paragraph

obsalt is a self-hosted **service** (API + worker) with a **web console**.
If you build your own agent, it also exposes a small **`VoiceCall` tracer**.
It is not a hosted dashboard you log into, and it is not an SDK that
replaces your voice platform. You connect live agents; you look at calls.
The product is six capabilities: latency, hangups, hallucination, tools,
evals, and search. Everything else is in service of those.
