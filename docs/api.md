# HTTP API

The console is HTML over these routes. Scripts use `X-API-Key`. Paths live
under `/v1` because HTTP APIs need a prefix, not because the product has
a public version number.

Collection list endpoints need a bounded `start` and `end`. Cursors are
`{call_id}:{revision}`. Fleet responses carry one `as_of_generation`.
Cross-org identifiers return 404.

## Auth

| Who | How |
| --- | --- |
| Service / worker / exporter | `X-API-Key` |
| Browser | `POST /v1/ui/login` → HTTP-only session cookie + CSRF |

Scopes: `ingest`, `read`, `analyze`, `admin`.
Roles: `owner`, `admin`, `analyst`, `reviewer`.
There is no "auth off."

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

## Examples

```bash
export KEY="${OBSALT_BOOTSTRAP_API_KEY:-dev-key}"
BASE=http://localhost:8080

# List
curl -sS -H "X-API-Key: $KEY" \
  "$BASE/v1/calls?start=2026-08-01T00:00:00Z&end=2026-08-22T23:59:59Z"

# Search — the field is q, not query
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"q":"customers asking about refunds","start":"2026-08-01T00:00:00Z","end":"2026-08-22T23:59:59Z"}' \
  "$BASE/v1/search"

# Connect Vapi (ingest_key is shown once)
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","secrets":{"legacy_secret":"…"},"settings":{"auth_mode":"legacy_secret"}}' \
  "$BASE/v1/connections"

# Ask for an LLM eval of one call
curl -sS -X POST -H "X-API-Key: $KEY" \
  "$BASE/v1/calls/$CALL_ID/analyze"

# Plain-English rubric. Edits create a new version.
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"empathy","description":"Did the agent acknowledge frustration before proposing a fix?"}' \
  "$BASE/v1/rubrics"
```

Call detail includes provenance and coverage. Timeline includes
`timeline_fidelity` derived from the measurements that are actually
there. Quality views show completed and eligible denominators.
Missing judge output is never a pass.

Outbound webhook secrets use the `whsec_` prefix and are returned only
at creation.
