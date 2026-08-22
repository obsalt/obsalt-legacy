# Storage

obsalt commits to three stores. There is no SQLite mode, no Postgres-only
production mode, and no pluggable-backend abstraction.

`docker compose up` is the supported path. `obsalt demo` launches the same
stack with a loud not-for-production banner. That is one command, not a second
storage implementation.

## Why three stores

Two product answers conflicted, and the resolution is load-bearing:

- One instruction said "Postgres only for production, no pluggable-backend
  abstraction, Parquet export for analytics."
- Another said "assume millions and use the right database that we don't want
  to change, think from system design."

Langfuse's Postgres architecture hit IOPS exhaustion at tens of thousands of
events per minute and moved analytics to ClickHouse. That is relevant evidence,
not proof that every million-call workload requires the same design. Our
projected narrow fact-table volume and percentile / group-by workload make
ClickHouse the selected engine.

**Keep the principle — commit to one architecture, build no adapter layer —
and apply the scale criterion to choose it.** ClickHouse is in the compose
file. There is no Postgres-only mode.

Requiring ClickHouse for `pip install obsalt && obsalt serve` is hostile.
Building a second storage backend is worse. The compromise is compose + demo,
not a shadow implementation.

## What lives where

| Store | Holds | Why |
| --- | --- | --- |
| **ClickHouse** | Immutable call revisions, turns, stage measurements, aggregate measurements, tool invocations, analysis results, immutable-fact rollups | Append-mostly, billions of rows, percentile and group-by-agent-over-time queries. `quantileTDigestState` makes long-window sample percentiles a merge of pre-aggregated states, not a scan of every call. |
| **Postgres** | Raw-envelope inbox, delivery dedupe, transactional outbox, processing runs, active-call revision pointer, rollup-generation pointers, orgs, users, hashed API/ingest keys, encrypted provider credentials, agents, rubrics, plugin config, retention / deletion / audit, **search documents + pgvector** | Transactional acceptance, constraints, revision promotion, current-call listing, authorization, search filtering, and synchronous privacy-control state. |
| **Object storage** (S3 / MinIO / GCS) | Org-namespaced raw payload blobs, redacted transcripts, tool payloads, recordings, OTLP forwarding payloads | Lifecycle-managed and encrypted at rest. Raw blobs are unredacted by definition and have a shorter access and retention boundary. |
| **Redis** | Lease accelerator and optional dedupe-hit cache | If Redis is down, workers still claim from the Postgres outbox. Postgres is authoritative. |

Queries always specify `(org_id, call_id, revision)`. Never `SELECT latest`.
Call-list cursors are `{call_id}:{revision}`. Fleet endpoints return one
`as_of_generation`.

## Why not the obvious alternatives

**Postgres only.** Listing and authorization fit Postgres. A year of
`stage_measurements` does not. At the target (1M calls/month, ~40 turns/call,
OTLP sources contributing ~10 stage rows/turn) that table is 100–400 million
rows per month. Percentile-over-time by agent is the query that decides the
engine.

**ClickHouse only.** Receive needs a transaction that writes the envelope
index, delivery-key dedupe, and outbox together, then compare-and-swaps an
active-revision pointer. ClickHouse is the wrong tool for that, and the wrong
tool for hashed keys, encrypted secrets, tombstones, and hybrid search.

**One abstract "backend" with SQLite for demo.** That is the multi-database
adapter Langfuse explicitly rejected. Demo uses the real stack in disposable
containers.

**ReplacingMergeTree as "latest wins."** Candidate revisions are complete.
Postgres atomically chooses the active one. No query depends on an
asynchronously synchronized ClickHouse "latest" projection.

## Sizing at the target

At 1M calls/month:

| Table | Rows/month | Notes |
| --- | --- | --- |
| `call_revisions` | 1M plus corrections | complete immutable snapshots; identity `(org_id, call_id, revision)` |
| `turns` | 40M | partition by month, order by `(org_id, call_id, turn_index)` |
| `stage_measurements` | 100–400M | the volume driver; narrow rows, heavily compressible |
| `tool_invocations` | ~5M | |
| `analysis_results` | 5–10M | additive, versioned |

Billions of rows within a year. That is why `stage_measurements` is a narrow
fact table with references rather than nested JSON.

## Schema principles

- **Append-only facts and complete revisions.** No shallow ClickHouse call
  rows. Call-list queries use the Postgres active-call index and hydrate exact
  immutable ClickHouse revisions.
- **Provenance travels with the value.** `stage_measurements` carries
  `provenance`, `source_path`, `derivation` as columns.
- **Derived storage matches mutability.** Incremental materialized views
  consume only immutable facts. Revision-sensitive rollups use refreshable
  views or partition rebuild-and-swap so replay and late corrections retract
  obsolete contributions.
- **Provider-published percentiles never enter sample rollups.** Mixing them
  is what produced the 290ms-vs-740ms error in v0.1. They are
  `AggregateMeasurement`s, stored separately.
- **Search is one Postgres projection.** `search_documents` holds org, call
  id, active revision, redacted transcript text, `tsvector`, embedding, and
  filter facets. Lexical and vector candidates share tenant and structured
  filters; call bodies hydrate from ClickHouse.

## Search

Real embeddings, not a hash trick.

- Default embedder: local ONNX model. No torch dependency, no API key, works
  air-gapped. Optional hosted embedders.
- pgvector with HNSW, to be benchmarked rather than assumed.
- Hybrid retrieval: reciprocal-rank fusion of vector and lexical candidates.
  Pure vector search is bad at order numbers; pure keyword is bad at
  "customers asking about refunds."
- Embedded content is redacted content. Re-embedding is a versioned
  reprocessing job.

## Parquet export

Finalized calls, turns, and measurements export to Parquet on a schedule for
teams that want their own warehouse. This is the escape valve that means
committing to ClickHouse internally does not lock anyone's data in.

Export manifests record call revision and deletion status. obsalt propagates
deletion to managed exports. It cannot revoke copies moved to an external
warehouse. The API and this page say so.

## Retention defaults

Finite by default. "Indefinite" is how self-hosted tools accumulate liability.

| Data | Default |
| --- | --- |
| Raw payloads | 30 days |
| Transcripts and recordings | 90 days |
| Aggregates | 400 days |

Raw blobs are unredacted, encrypted at rest, separately access-controlled, and
excluded from normal read paths. That is the honest tradeoff for replayability.

`obsalt retain` sweeps expired raw, transcript, and aggregate data, and purges
orphan raw objects that have no inbox row.

## Redis is not a store

Redis accelerates leased work delivery. Dedupe retention is a persisted
Postgres table, not a short Redis TTL. Automatic retry horizons may be
unknown, and manual resends can occur much later.
