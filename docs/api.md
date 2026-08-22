# HTTP API

Versioned at `/v1`. Collection endpoints are paginated with cursors and require a
bounded time range.

```
POST   /v1/ingest/{provider}/{ingest_key}
POST   /v1/traces
GET    /v1/calls                 # start & end required
GET    /v1/calls/{id}
GET    /v1/calls/{id}/timeline
GET    /v1/calls/{id}/evidence/{ref}
POST   /v1/search
GET    /v1/latency
GET    /v1/hangups
GET    /v1/tools
GET    /v1/quality
POST   /v1/calls/{id}/analyze
CRUD   /v1/rubrics
CRUD   /v1/connections
POST   /v1/replay
POST   /v1/backfill
POST   /v1/privacy/deletion-requests
GET    /v1/plugins
GET    /health  /ready  /metrics
```

Service keys are hashed at rest, org-bound, and scoped (`ingest`, `read`, `analyze`,
`admin`). `require_auth=false` does not exist. Cross-org identifiers return 404.
