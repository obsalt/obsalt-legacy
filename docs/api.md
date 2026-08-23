# HTTP API

The console is HTML over these routes. Scripts use `X-API-Key`.
Interactive OpenAPI (same service): http://localhost:8080/docs.

Collection list endpoints need a bounded `start` and `end`. Cursors are
`{call_id}:{revision}`. Fleet responses carry one `as_of_generation`.
Cross-org identifiers return **404**, not 403.

## Auth

| Who | How |
| --- | --- |
| Service / exporter / scripts | `X-API-Key: <key>` |
| Browser | `POST /v1/ui/login` → HTTP-only session cookie + CSRF cookie |

Scopes on the key: `ingest`, `read`, `analyze`, `admin`.
Roles on the session / key: `owner`, `admin`, `analyst`, `reviewer`.

There is no “auth off.” An empty secret is `missing_credential`.

| Action | Minimum role | Typical scope |
| --- | --- | --- |
| Read calls, search, fleet, plugins | reviewer | `read` |
| Run evals, write rubrics | analyst | `analyze` |
| Connections, replay, backfill, deletion, webhooks, export | admin | `admin` |
| Create users, rotate keys | owner | `admin` |

Owner vs admin cannot be inferred from the four scopes alone. The
bootstrap key is owner. See [security](reference/security.md).

## Surface

```
POST   /v1/ingest/{provider}/{ingest_key}   observational webhook
POST   /v1/traces                           OTLP HTTP (protobuf or JSON)

GET    /v1/calls                            start & end required
GET    /v1/calls/{id}
GET    /v1/calls/{id}/timeline
GET    /v1/calls/{id}/evidence/{ref}

POST   /v1/search                           body: q, start, end, optional filters
GET    /v1/latency
GET    /v1/hangups
GET    /v1/tools
GET    /v1/quality
POST   /v1/calls/{id}/analyze
POST   /v1/quality/review
POST   /v1/rubrics/{id}/calibrate

CRUD   /v1/rubrics
CRUD   /v1/connections                      ingest_key returned only at creation
GET/POST /v1/outbound-webhooks              Standard Webhooks; secret at creation only
POST   /v1/outbound-webhooks/{id}/rotate
POST   /v1/keys/rotate
GET/POST/DELETE /v1/users

POST   /v1/replay
POST   /v1/backfill
POST   /v1/privacy/deletion-requests
GET    /v1/retention
POST   /v1/export
GET    /v1/plugins

GET    /health  /ready  /metrics
GET/POST /v1/ui/login
GET    /v1/ui  /v1/ui/calls/{id}  /v1/ui/latency  /v1/ui/hangups
GET    /v1/ui/quality  /v1/ui/search  /v1/ui/settings
```

## Worked examples

```bash
export KEY="${OBSALT_BOOTSTRAP_API_KEY:-dev-key}"
BASE=http://localhost:8080
```

### List calls

```bash
curl -sS -H "X-API-Key: $KEY" \
  "$BASE/v1/calls?start=2026-08-01T00:00:00Z&end=2026-08-22T23:59:59Z&limit=50"
```

Optional filters: `agent_id`, `outcome`, `source`, `latency_ms`, `flag`,
`eval_result`, `cursor`.

```json
{
  "items": [
    {
      "id": "…",
      "revision": "…",
      "source": "vapi",
      "agent_id": "support",
      "status": "ended",
      "started_at": "2026-08-22T14:01:00+00:00",
      "timeline_fidelity": "turn_level",
      "pipeline_architecture": "cascade",
      "hangup": "user_hangup",
      "decoder_version": "vapi/1"
    }
  ],
  "as_of_generation": "…",
  "next_cursor": "call-id:revision"
}
```

`GET /v1/calls/{id}` is the full `CallRevision` plus `analysis[]`.
`GET /v1/calls/{id}/timeline` is the join-view payload: whether a stage
waterfall may be drawn, unplaced chips, aggregates, and the reason.

### Search

The field is `q`, not `query`.

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"q":"customers asking about refunds","start":"2026-08-01T00:00:00Z","end":"2026-08-22T23:59:59Z","agent_id":"support"}' \
  "$BASE/v1/search"
```

Optional filters: `source`, `hangup_reason`.

### Connections

`ingest_key` is shown **once**.

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","secrets":{"legacy_secret":"…"},"settings":{"auth_mode":"legacy_secret"}}' \
  "$BASE/v1/connections"
```

```json
{
  "connection_id": "…",
  "ingest_key": "…",
  "provider": "vapi"
}
```

Webhook URL: `$BASE/v1/ingest/vapi/<ingest_key>`.
Provider-specific secrets: [connect-hosted](connect-hosted.md).

`POST /v1/calls/{id}/analyze` evaluates **every** rubric in the org
(or hallucination entailment if none exist) and returns
`{"items": [AnalysisResult, …]}`. A `$0` monthly budget blocks paid
judges; the free heuristic may still run.

### Rubrics and evals

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"empathy","description":"Did the agent acknowledge frustration before proposing a fix?","threshold":0.7}' \
  "$BASE/v1/rubrics"

curl -sS -X POST -H "X-API-Key: $KEY" \
  "$BASE/v1/calls/$CALL_ID/analyze"
```

`PUT /v1/rubrics/{id}` creates a new version. Historical results keep
the version they were judged under.

Analyze may return `state: "budget_blocked"` when
`OBSALT_LLM_MONTHLY_BUDGET_USD` is exhausted (or still `0`). Missing
judge output is never a pass.

Calibrate a rubric with labeled calls:

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"labeled":[{"call_id":"…","expected_pass":true}]}' \
  "$BASE/v1/rubrics/$RUBRIC_ID/calibrate"
```

### Fleet

`GET /v1/latency`, `/v1/hangups`, `/v1/tools`, `/v1/quality` all require
`start` and `end`. Each body includes `as_of_generation`. Quality
exposes completed and eligible denominators.

### Replay, backfill, deletion

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","source_call_id":"call_123"}' \
  "$BASE/v1/replay"

curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","connection_id":"…"}' \
  "$BASE/v1/backfill"

curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"call_id":"…"}' \
  "$BASE/v1/privacy/deletion-requests"
```

Deletion also accepts `source_call_id`, `caller` / `caller_token`, or
`start`/`end`. Status `accepted` is not done. Done means `completed_at`
is set. See [Operate](ops.md).

### Keys and outbound webhooks

```bash
curl -sS -X POST -H "X-API-Key: $KEY" \
  "$BASE/v1/keys/rotate"
# → { "key": "…", "overlap_seconds": 86400 }

curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/hooks/obsalt","event_type":"call.finalized"}' \
  "$BASE/v1/outbound-webhooks"
# → { "id": "…", "url": "…", "secret": "whsec_…" }   # secret once
```

Outbound webhook secrets use the `whsec_` prefix. Localhost HTTP is
denied unless `allow_http_localhost` is true (dev only).

### Health

`GET /health` — liveness (`status`, `version`).
`GET /ready` — plugins, `insecure_defaults`, inbox age, outbox depth,
DLQ, orphan blobs, deletion backlog.
`GET /metrics` — Prometheus.

## Ingest notes

- Webhook: raw bytes first. Ack is provider-specific. Decode is a worker.
- OTLP: `Content-Type` `application/x-protobuf` or `application/json`.
  415 otherwise. 503 = retry the batch. Partial-success protobuf = do
  **not** retry those records.
- Unknown plugin: 404 `plugin not installed`.

Call detail includes provenance and coverage. Timeline includes
`timeline_fidelity` derived from the measurements that are actually
there.

## Next

What those payloads mean: [What obsalt does](product.md) and
[the console](console.md). Operating the box: [Operate](ops.md).
