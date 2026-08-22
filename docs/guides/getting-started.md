# Getting started

This guide takes you from a clean checkout to a running obsalt with one
ingest path. It does not teach Vapi or Pipecat. It teaches obsalt.

## What you will have

- Postgres, ClickHouse, Redis, and MinIO on localhost
- `obsalt serve` on http://localhost:8080
- A bootstrap API key bound to one organization
- A place to paste a webhook URL or point an OTLP exporter

## Prerequisites

- Python 3.11 or newer
- Docker, for the durable stack
- A hosted voice provider **or** a custom agent that can emit OTLP

There is no SQLite mode. The supported path is compose.

## Install

From a clone:

```bash
python -m pip install -r requirements-dev.txt
docker compose up -d
obsalt init
obsalt doctor
obsalt serve
```

From packages, once they are on an index:

```bash
pip install "obsalt[vapi,retell]"
docker compose up -d
obsalt serve
```

`obsalt init` writes `.env.example`. Copy it to `.env` and replace every
`change-me` and `dev-key` before any network-exposed deploy.

`obsalt demo` is the same compose stack with a banner: **not for production,
data is not durable.**

## Bootstrap auth

Local bootstrap uses `OBSALT_BOOTSTRAP_API_KEY` (default `dev-key`) and
`OBSALT_BOOTSTRAP_ORG_ID` (default `local`). The key is hashed at rest and
org-bound. `require_auth=false` does not exist.

```bash
curl -sS -H "X-API-Key: dev-key" http://localhost:8080/ready
```

Open http://localhost:8080/v1/ui and sign in with the bootstrap key.

## Connect a hosted provider

Create a connection, then point the provider at obsalt. Secrets are
envelope-encrypted and never returned after creation.

```bash
curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "vapi",
    "secrets": {"shared_secret": "replace-me"}
  }'
```

The response includes `ingest_key` once. Store it. Point Vapi at:

```
POST http://<host>:8080/v1/ingest/vapi/<ingest_key>
```

Authentication is fail-closed. An empty secret is not "skip verification."
This endpoint accepts observational events only (`end-of-call-report`,
transcripts). Do not configure obsalt as the handler for `assistant-request`,
tool-calls, or transfer requests — those must reach your application.

Decode runs in a worker:

```bash
obsalt worker
```

Webhook acknowledgement does not wait on decode or analysis. If you only run
`obsalt serve` in development, some setups also drain the inbox on the
request path after ack; production is `serve` + `worker`.

Then place a call. Open `/v1/ui`, open the call, and read the provenance
panel. If Vapi stage durations appear as chips instead of a waterfall, that
is correct: they have no timestamps.

Provider-specific headers and units: [Hosted webhook](hosted-webhook.md).

## Instrument a custom agent

Custom agents emit OTLP from the process. `VoiceCall` is a thin tracer with
low-cardinality span names.

```python
from obsalt import VoiceCall, setup_tracing

setup_tracing(endpoint="http://localhost:8080/v1/traces")

with VoiceCall.start(call_id="c1", org_id="local", agent_id="support") as call:
    with call.turn(0, "user", "I need a refund") as turn:
        with turn.stt("deepgram"):
            pass
        with turn.llm("gpt-4.1"):
            pass
        with turn.tts("cartesia"):
            pass
```

Tenancy comes from the ingest API key on the exporter, not from `org_id` on
the span. `org_id` corroborates. It cannot choose an organization.

Full example: [Custom agent](custom-agent.md).

## What to look at first

1. `/v1/ui` call list — filter by agent and time.
2. Call detail — timeline, transcript, provenance.
3. `/ready` — inbox age, outbox depth, DLQ, orphan blobs.
4. `/v1/plugins` — installed plugins and their fidelity declarations.

If a signal is missing, the provenance panel says whether the provider did
not send it, we failed to decode it, or it was redacted. That is the product.

## Next

- [Choose a path](choose-a-path.md) if you are unsure webhook vs OTLP
- [Operate](operate.md) for retention, deletion, and rotation
- [HTTP API](../reference/api.md) for the full `/v1` surface
- [Configuration](../reference/configuration.md) for every `OBSALT_*` setting
