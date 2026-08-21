# HTTP API

Base URL: the host you pass to `obsalt --port` (default `http://localhost:8080`).

Interactive docs: `GET /docs`. This API is ingest and evidence lookup. It is not a dashboard.

## Auth

Every `/v1/*` route reads a secret and maps it to `org_id`.

| Header | Value |
| --- | --- |
| `X-API-Key` | the secret from `OBSALT_API_KEYS` |
| `Authorization` | `Bearer <secret>` |

`OBSALT_API_KEYS` format: `org:secret,org2:secret2`.

- Valid secret → that org.
- `OBSALT_REQUIRE_AUTH=true` and missing/invalid secret → `401`.
- `REQUIRE_AUTH` off and no key → first configured org, or `demo`.

Provider webhook HMAC is checked in addition to this. See [Ingest webhooks](ingest.md).

## Ingest

`POST /v1/ingest/{vapi,retell,bland,openai-realtime,native}`

Body: JSON object (the provider’s webhook, or a `CallRecorder` snapshot).

| Field | Type | Meaning |
| --- | --- | --- |
| `call_id` | string | obsalt id (stable `uuid5` of org + provider + provider call id) |
| `provider_call_id` | string | id from the provider |
| `status` | `accepted` \| `merged` \| `finalized` | live vs terminal |
| `created` | bool | first time this provider call was seen |
| `finalized` | bool | analysis has run |

Errors: `400` invalid JSON, `401` auth/HMAC, `422` payload missing a call id.

## Calls

`GET /v1/calls?agent_id=`

List summaries for the org, newest first: id, provider, agent, status, duration, hangup reason, loss score, hallucination count, tool count.

`GET /v1/calls/{call_id}`

Full `CanonicalCall` JSON: turns, tools, latency samples, hangup, hallucinations, evals, grounding, recording URL, transcript.

`404` if the id is missing or belongs to another org.

## Search

`POST /v1/search` `{ "query": "customers asking about refunds", "limit": 10 }`

`GET /v1/search?q=refund&limit=10`

Hybrid lexical + hashing-trick semantic search over finalized transcripts. Hits: `call_id`, `score`, `snippet`.

## Rollups

`GET /v1/latency?agent_id=` — P50/P95/P99 per component (`stt`, `llm`, `tts`, `e2e`, `ttfa`, `tool`, …).

`GET /v1/hangups` — clusters by reason, party, last-utterance theme. Each cluster includes `lost_customer_call_id`.

`GET /v1/tools?agent_id=` — per-tool invocations, success rate, retry rate, P50/P95, payload shapes.

## Evals

Default rubrics are seeded per org on first list: grounded claims, latency budget, customer kept, tools succeed.

`GET /v1/evals` — enabled rubrics.

`POST /v1/evals/rubrics`

```json
{ "name": "No invented IDs", "description": "The agent never invents order numbers.", "threshold": 0.9 }
```

The default `HeuristicJudge` scores from words in `description` (`hallucin`, `latency`, `tool`, `hangup`, `interrupt`, …).

`POST /v1/evals/run/{call_id}` — re-finalize the call (re-run analysis + all rubrics).

## Health

`GET /health` → `{ "status": "ok", "service": "obsalt" }` (no auth).
