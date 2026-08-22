# obsalt documentation

Start at the [root README](../README.md) if you have not already. It says
what this is. These pages say how to use it, and how to change it.

## If you are going to run it

1. [Getting started](getting-started.md) — install, compose, sign in
2. Then **one** of:
   - [Connect a hosted platform](connect-hosted.md) — Vapi, Retell, ElevenLabs, Cartesia
   - [Connect your own agent](connect-custom.md) — Pipecat, LiveKit, Realtime, Gemini Live
3. [The console](console.md) — what appears after a live call, provider by provider

That is the whole end-to-end path. Everything else is reference.

## If you are going to operate it

- [Operate](ops.md) — health, retention, deletion, rotation
- [Configuration](reference/configuration.md) — every `OBSALT_*` setting
- [CLI](reference/cli.md) — `obsalt serve`, `worker`, `retain`, …
- [HTTP API](api.md) — `/v1` for scripts and the console
- [Security](reference/security.md) — tenancy, redaction, egress

## If you are going to change it

- [Architecture](architecture.md) — pipeline, stores, the rules we will not break
- [Write a plugin](plugins.md) — public contract, fixtures, golden files
- [Developing](developing.md) — repo layout, PR bar, how to add a source
- [Domain](reference/domain.md) — events, revisions, measurements
- [OTLP](reference/otlp.md) — span names, attributes, PII
- [Glossary](reference/glossary.md)

## What this is, in one paragraph

obsalt is a self-hosted **service** (API + worker) with a **web console**.
If you build your own agent, it also exposes a small **`VoiceCall` tracer**.
It is not a hosted dashboard you log into, and it is not an SDK that
replaces your voice platform. You connect live agents; you look at calls.
