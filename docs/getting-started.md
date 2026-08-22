# Getting started

This gets obsalt running on your machine. It does not connect a voice
platform — that is the next page, once you know which kind of agent you
have.

You will end with:

- Postgres, ClickHouse, Redis, and MinIO on localhost
- the service on http://localhost:8080
- a worker draining the outbox
- a browser session in the console

## Prerequisites

- Python 3.11 or newer
- Docker, for the durable stack
- A hosted voice platform **or** an agent process that can emit OTLP

There is no SQLite mode. Compose is the supported path.

## Install and run

From a clone:

```bash
python -m pip install -r requirements-dev.txt
docker compose up -d
obsalt init
obsalt doctor
obsalt serve
```

In a second terminal:

```bash
obsalt worker
```

`serve` is the API and the console. `worker` decodes. Webhook
acknowledgement never waits on decode. In development, `serve` also drains
the inbox after ack so a single process can demo; production is both.

From packages, once they are on an index:

```bash
pip install "obsalt[vapi,retell]"
docker compose up -d
obsalt serve
```

`obsalt init` writes `.env.example`. Copy it to `.env`. Replace every
`change-me` and `dev-key` before the box is reachable from a network you
do not trust.

`obsalt demo` is the same compose stack with a loud banner: **not for
production, data is not durable.**

## Sign in

Local bootstrap uses `OBSALT_BOOTSTRAP_API_KEY` (default `dev-key`) and
`OBSALT_BOOTSTRAP_ORG_ID` (default `local`). The key is hashed at rest and
bound to that org. There is no "auth off" switch.

```bash
curl -sS -H "X-API-Key: dev-key" http://localhost:8080/ready
```

Open http://localhost:8080/v1/ui, paste the same key, continue.

You should see an empty call list, plus Latency / Hangups / Quality /
Search / Settings in the header. Settings lists installed plugins. If the
plugin you need is missing, you installed core without it.

## What to do next

obsalt is running. It has nothing to look at until a **live** agent sends
it a call.

| I run… | Next page |
| --- | --- |
| Vapi, Retell, ElevenLabs, or Cartesia | [Connect a hosted platform](connect-hosted.md) |
| Pipecat, LiveKit, OpenAI Realtime, or Gemini Live | [Connect your own agent](connect-custom.md) |

After the first call lands, [The console](console.md) is the page that
tells you what you are looking at — and what that provider will never
send.
