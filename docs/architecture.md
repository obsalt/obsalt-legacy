# Architecture

Receive writes the raw body to object storage and commits a Postgres inbox/outbox
row **before** the provider-specific success response. Decode is a pure function
from that raw envelope to `NormalizedEvent[]`. Redaction is a single choke point
on the normalized stream. The assembler folds facts into a complete immutable
`CallRevision`. Postgres compare-and-swaps the active-revision pointer after
ClickHouse has the candidate. Decode never runs on the webhook request path.

```
raw bytes → ingest_key → fail-closed auth → classify
         → object store → Postgres inbox/outbox → ack
         → worker: decode → redact → assemble → ClickHouse write
         → Postgres CAS promote → index / analyze / outbound
```

## Tenets that show up in code

| Id | Rule |
| --- | --- |
| T1 | A timeline is drawn only from `INTERVAL` measurements with real timestamps. Unplaced durations are chips, never span positions. |
| T2 | Provenance and source path travel with every value. The call-detail provenance panel is the product surface for this. |
| T3 | Raw envelopes are durable. Adapter bugs become replays inside the raw retention horizon. |
| T4 | Plugin fixtures validate against vendored provider schemas plus golden `NormalizedEvent[]`. |
| T5 | Redaction is one choke point on the normalized stream, before durable write of normalized data. Raw blobs stay unredacted and short-lived. |
| T7 | Production is Postgres + ClickHouse + object storage. There is no SQLite / Postgres-only mode. Memory types are test doubles. |
| T8 | Providers are separately installable plugins. Core ships none. |
| T10 | `org_id` comes only from authenticated credentials. Plugins and OTLP resource attributes cannot choose it. |

## Stores

| Store | Holds |
| --- | --- |
| **ClickHouse** | Immutable call revisions and narrow fact tables. Queries always specify `(org_id, call_id, revision)`. Never `SELECT latest`. |
| **Postgres** | Inbox/outbox, delivery dedupe, active-revision pointer, hashed keys, encrypted secrets, tombstones, search documents + pgvector HNSW, audit. |
| **Object storage** | Org-namespaced raw blobs. Encrypted at rest, 30-day default retention, excluded from normal read paths. |
| **Redis** | Lease accelerator only. If Redis is down, workers still claim from the Postgres outbox. |

Fleet endpoints return one `as_of_generation`. Call-list cursors are `{call_id}:{revision}`.

## What this tree does not do

Phase 7 (restore-from-raw drills, delete-by-caller through backups, load tests at
1M calls/month) is the production-release gate and is not claimed here. Parquet
export, OTLP gRPC, and OIDC are designed for and not the functional milestone.

See [rewrite-plan.md](rewrite-plan.md) §4–§6 for the stage contracts and promotion protocol.
