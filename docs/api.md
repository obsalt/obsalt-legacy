# HTTP API

Versioned at `/v1`. Collection endpoints are paginated and require a bounded
time range. `require_auth=false` does not exist.

```
POST   /v1/ingest/{provider}/{ingest_key}
POST   /v1/traces
GET    /v1/calls
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

Authentication: hashed, org-bound, scoped service keys; browser sessions with
secure cookies. Cross-org identifiers return 404.

Fleet responses include `as_of_generation`. Call-detail reads the Postgres
active-revision pointer, then the exact ClickHouse revision.
