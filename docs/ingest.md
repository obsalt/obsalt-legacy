# Ingest

`POST /v1/ingest/{provider}/{ingest_key}` receives observational webhooks.
`ingest_key` resolves the tenant connection *before* authentication.

Order is non-negotiable: raw bytes → resolve key → authenticate (fail closed)
→ classify observational-only → delivery key → object store → Postgres
inbox/dedupe/outbox → provider-specific ack. Decode runs in a worker.

Synchronous Vapi events (`assistant-request`, tool execution, transfer,
knowledge-base) are rejected. obsalt is not their handler.

`POST /v1/traces` accepts OTLP/HTTP protobuf and proto3-JSON. Tenancy comes
from an ingest-scoped API key. Resource attributes may corroborate an org;
they never select one. Mixed-org batches are rejected.

See [rewrite-plan.md](rewrite-plan.md) §6.
