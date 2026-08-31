# Start

This page gets you a signed-in console on your machine. It does **not**
connect a voice platform — that is the next page.

An empty call list at the end is success. Fixtures in this repo are for
tests. The console fills from live traffic (or from `obsalt seed` in
dev).

## What you will have

- Postgres, ClickHouse, Redis, and MinIO on localhost
- the service on http://localhost:8080
- a worker that decodes incoming calls
- a browser session in the console

There is no SQLite mode. Compose is the supported path.

## Install and run

One command from a clone:

```bash
docker compose up -d                            # or: make up
```

That starts the stores, `obsalt serve` (API + console on :8080), and
the worker. Follow logs with `docker compose logs -f obsalt worker`.
Stop with `docker compose down`.

To run the processes on your host (common while developing):

```bash
uv sync --all-packages
docker compose up -d postgres clickhouse redis minio
uv run obsalt init --write-env
uv run obsalt doctor
uv run obsalt serve
```

In a second terminal: `uv run obsalt worker`.

`serve` is the API and the console. `worker` decodes. Webhook
acknowledgement never waits on decode; production is both.

`obsalt doctor` probes Postgres, ClickHouse, object storage, and Redis.
Exit 2 means a required store is down. Exit 1 means no plugins loaded.
Redis is optional.

Replace every `change-me` and `dev-key` before the box is reachable
from a network you do not trust. Every `OBSALT_*` key:
[Configuration](reference/configuration.md).

## Sign in

Local bootstrap uses `OBSALT_BOOTSTRAP_API_KEY` (default `dev-key`) and
`OBSALT_BOOTSTRAP_ORG_ID` (default `local`). There is no “auth off”
switch.

```bash
curl -sS -H "X-API-Key: dev-key" http://localhost:8080/ready
```

Open http://localhost:8080/v1/ui, paste the same key, continue.

You should see an empty call list, plus Latency / Hangups / Quality /
Search / Settings. Settings is where you create a hosted connection
(ingest URL shown once) or copy the OTLP endpoint.

| Check | What “good” looks like |
| --- | --- |
| Process | `curl -sS http://localhost:8080/health` → `{"status":"ok",…}` |
| Stack + plugins | `obsalt doctor` and `GET /ready` |
| Console | `/v1/ui` after login |
| Interactive API | http://localhost:8080/docs |

## Fill it locally (dev only)

The empty Calls page **Load sample calls**, or:

```bash
uv run obsalt seed
```

Both replay vendored fixtures the way a provider would. Both refuse
`OBSALT_ENVIRONMENT=production`. See [Develop](develop.md).

## Words you will trip over

| Word | Means |
| --- | --- |
| **Chip** | A duration we have, but cannot place on a timeline. |
| **Waterfall** | Stage bars drawn only from real start and end timestamps. |
| **Revision** | An immutable snapshot of a call. Late events create a new one. |
| **Ingest key** | Secret in the webhook URL. Shown once. |
| **Provenance** | Where a number came from — or why it is missing. |
| **Grounding** | Prompt, knowledge, tool results, and caller text hallucination checks may trust. |

## Next

| I run… | Next page |
| --- | --- |
| Vapi, Retell, ElevenLabs, or Cartesia | [Connect a hosted platform](connect-hosted.md) |
| Pipecat, LiveKit, OpenAI Realtime, or Gemini Live | [Connect your own agent](connect-custom.md) |

After the first call lands, [the console](console.md) tells you what
you are looking at.
