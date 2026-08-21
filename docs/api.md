# HTTP API

Base URL: the host you pass to `obsalt serve` (default `http://localhost:8080`).

Interactive docs: `GET /docs`. This API is ingest, evidence lookup, and the per-call join view. Grafana remains the fleet dashboard.

Python: `ObsaltClient` (`from obsalt import ObsaltClient`) wraps these routes.

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
- Any unknown secret → `401` even if `REQUIRE_AUTH` is off.

Provider webhook HMAC is checked in addition to this. See [Ingest webhooks](ingest.md) and each [provider page](providers/index.md).

## Ingest

`POST /v1/ingest/{vapi,retell,bland,openai-realtime,native}`

Body: JSON object (the provider’s webhook, or a `NativeSnapshot` / `VoiceCall.snapshot()`).

| Field | Type | Meaning |
| --- | --- | --- |
| `call_id` | string | obsalt id (stable `uuid5` of org + provider + provider call id) |
| `provider_call_id` | string | id from the provider |
| `status` | `accepted` \| `merged` \| `finalized` | live vs terminal |
| `created` | bool | first time this provider call was seen |
| `finalized` | bool | analysis has run |

Errors: `400` invalid JSON, `401` auth/HMAC, `422` payload missing a call id.

Tool argument **values** are redacted before the call is stored. Shapes remain.

## Calls

`GET /v1/calls?agent_id=&provider_call_id=&provider=`

List summaries for the org, newest first: id, provider, agent, status, duration, hangup reason, loss score, hallucination count, tool count, plus `coverage` (signals, gap ids, completeness) and `view_path`.

Pass `provider_call_id` (and `provider` when you know it) to find a call by the room / SIP / Vapi id you already have.

`GET /v1/calls/{call_id}`

Full `CanonicalCall` JSON: turns, tools, latency samples, hangup, hallucinations, evals, grounding, recording URL, transcript. See [Data model](data-model.md).

`404` if the id is missing or belongs to another org.

## Join view

`GET /v1/calls/{call_id}/view`

JSON packet for one call: a conversation-shaped span tree **and** the evidence it joins to, without putting transcripts on span attributes.

| Field | Contents |
| --- | --- |
| `join` | `call.id`, `call.provider_id`, `workspace.id`, `agent.id`, `gen_ai.conversation.id` |
| `trace.spans` | Span names (`call.lifecycle`, `turn.N`, `stt.transcription`, …). Timings and join keys only |
| `trace.source` | `live` if the agent already exported OTLP (`spans_exported`), else `reconstructed` |
| `evidence` | turns + transcript, recording URL, tools (shapes, not secrets), hangup, evals, hallucinations |
| `coverage` | Which signals are present, and whether a gap is **structural** (this path cannot produce it) or **missing** (this call did not) |
| `links` | `call`, `view`, `ui` |

`GET /v1/ui` and `GET /v1/calls/{call_id}/ui` render the same packet as HTML (API key required when `OBSALT_REQUIRE_AUTH=true`).

Python: `ObsaltClient.view_call(call_id)`.

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

`GET /health` (no auth):

```json
{
  "status": "ok",
  "service": "obsalt",
  "version": "0.1.0",
  "otlp_configured": true,
  "require_auth": false,
  "environment": "dev",
  "store": "memory"
}
```
