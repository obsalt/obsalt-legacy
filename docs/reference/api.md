# HTTP API

Versioned at `/v1`. Collection endpoints are paginated with cursors and
require a bounded time range. Unbounded `list_calls` was the shape of bug
that only appears in production.

Call-list cursors are `{call_id}:{revision}`. Fleet responses include
`as_of_generation`. Neither endpoint mixes serving generations inside one
page. Cross-org identifiers return 404.

## Authentication

Send `X-API-Key` for service principals. Browser users use
`GET/POST /v1/ui/login` and an HTTP-only session cookie plus CSRF.

Scopes: `ingest`, `read`, `analyze`, `admin`. Roles: `owner`, `admin`,
`analyst`, `reviewer`. `require_auth=false` does not exist.

## Endpoints

```
POST   /v1/ingest/{provider}/{ingest_key}   observational webhook
POST   /v1/traces                           OTLP HTTP (proto + JSON)

GET    /v1/calls                            start & end required
GET    /v1/calls/{id}                       aggregate + provenance
GET    /v1/calls/{id}/timeline              per-call derived fidelity
GET    /v1/calls/{id}/evidence/{ref}        transcript / tool payload / recording

POST   /v1/search                           hybrid semantic + filter
GET    /v1/latency                          precomputed rollups
GET    /v1/hangups                          precomputed clusters
GET    /v1/tools                            precomputed rollups
GET    /v1/quality                          evals + flags
POST   /v1/calls/{id}/analyze               request tier-2 analysis
POST   /v1/quality/review                   human agree / disagree
POST   /v1/rubrics/{id}/calibrate           labelled calibration run

CRUD   /v1/rubrics                          versioned
CRUD   /v1/connections                      provider connections + secrets
GET/POST /v1/outbound-webhooks              Standard Webhooks; secret at creation only
POST   /v1/outbound-webhooks/{id}/rotate
POST   /v1/keys/rotate
GET/POST/DELETE /v1/users

POST   /v1/replay                           reprocess by filter (admin)
POST   /v1/backfill                         provider pull (admin)
POST   /v1/privacy/deletion-requests
GET    /v1/retention
POST   /v1/export
GET    /v1/plugins                          installed plugins + fidelity

GET    /health  /ready  /metrics
GET/POST /v1/ui/login
GET    /v1/ui  /v1/ui/calls/{id}  /v1/ui/latency  /v1/ui/hangups
GET    /v1/ui/quality  /v1/ui/search  /v1/ui/settings
```

## Examples

List calls in a window:

```bash
curl -sS -H "X-API-Key: $KEY" \
  "http://localhost:8080/v1/calls?start=2026-08-01T00:00:00Z&end=2026-08-22T00:00:00Z"
```

Search:

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"query":"customers asking about refunds","start":"2026-08-01T00:00:00Z","end":"2026-08-22T00:00:00Z"}' \
  http://localhost:8080/v1/search
```

Request analysis:

```bash
curl -sS -X POST -H "X-API-Key: $KEY" \
  http://localhost:8080/v1/calls/$CALL_ID/analyze
```

Create a rubric (editing creates a version; results reference the version
they were judged under):

```bash
curl -sS -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"empathy","description":"Did the agent acknowledge the caller frustration before proposing a fix?"}' \
  http://localhost:8080/v1/rubrics
```

## Response contracts

Call detail includes provenance and coverage for every signal. Timeline
includes `timeline_fidelity` derived from the measurements actually present.

Fleet endpoints (`/v1/latency`, `/v1/hangups`, `/v1/tools`, `/v1/quality`)
return one `as_of_generation`. Approximate percentiles from
`quantileTDigestState` are labelled as approximations.

Quality views select an explicit published analyzer/rubric version, show
completed and eligible denominators, and separate unbiased baseline-sample
statistics from trigger-biased review queues. Missing output is never a
pass.

Outbound webhook secrets use the `whsec_` prefix and are returned only at
creation.
