# Security and tenancy

## Tenancy

`org_id` is the boundary and is in the identity / order key of every
ClickHouse fact and every query path — not applied as a post-filter.
Provider connections, secrets, rubrics, retention, search documents,
queues, and LLM budgets are all per-org.

Core stamps `org_id` from the authenticated connection. Plugins and
telemetry cannot choose it. Payload fields, span attributes, trace
resources, and object keys may corroborate. They may never select.

Cross-tenant reads and writes are prevented at the repository / query
layer and asserted in tests. Cross-org identifiers return 404.

## Authentication

Fail-closed. Empty credentials are `missing_credential`, not "skip."
`require_auth=false` is deleted.

Service keys are hashed at rest, org-bound, scoped, rotatable with
overlap. Browser sessions use secure / HTTP-only / SameSite cookies and
CSRF. Roles map to an endpoint authorization matrix.

Recoverable secrets are envelope-encrypted, narrowly decrypted, redacted
from logs, audited on use, and never returned after creation.

## Webhook receive

Read raw bytes first. Parsing changes the byte sequence and breaks
signatures. Core retains raw multi-valued headers, rejects duplicates for
headers the plugin declares singleton, and passes the rest to
`plugin.authenticate`. Persist only an allowlist of diagnostic headers;
never archive authorization or cookie headers.

Dedupe is a persisted Postgres table, not a short Redis TTL.

## Redaction and PII

Redaction is synchronous, at one choke point, before durable write of
normalized data. This improves on systems that mask in an async worker and
therefore land unmasked payloads in blob storage first.

Raw blobs are unredacted by definition: encrypted at rest, short
retention, separately access-controlled, excluded from normal read paths.
That is the honest tradeoff for replayability.

Conversational content on spans lives under `obsalt.pii.*`. Default export
strips it. The denylist is a test assertion.

A pluggable redactor lets an operator supply their own policy.

## Retention and deletion

Defaults are finite: 30 / 90 / 400 days. Deletion cannot be undone.
Durable tombstones are checked by receive, replay, backfill, indexing,
analysis, and export.

Deletion is verified across every managed store: Postgres control rows and
search documents delete synchronously; ClickHouse rows are masked promptly,
then physically removed; object storage purges current and noncurrent
versions; queues, DLQ references, rollups, caches, Parquet manifests, and
backup expiry are included. External copies cannot be revoked and are
disclosed.

Every privacy request and completion state is audited. A deletion that
never sets `completed_at` is not a deletion.

## Egress

One policy for backfill, judges, embedders, OTLP destinations, recording
retrieval, and outbound webhooks. Validates schemes and ports, blocks
internal address ranges by default, re-resolves DNS after redirects, bounds
response size and time.

Outbound webhooks: HTTPS-only, Standard Webhooks signatures, PII-minimal
payloads, org-scoped outbox, no second logical event on finalize retry.

## Resource boundaries

Ingest has compressed / expanded byte, item-count, nesting,
attribute-length, deadline, and concurrency limits. Storage, analysis,
search, and delivery have per-org quotas and fair queues.
High-cardinality attribute promotion requires an explicit budget.

Plugins receive a join deadline. Exception wrapping is not a sandbox.
Tenant-installable plugins are out of v2 scope.

## Reliability signals

Inbox / outbox age, orphan-blob count, decode failure rate by plugin, DLQ
depth, late-root frequency, active-revision promotion failure, unmapped
provider-code rate, unmapped-attribute rate, per-org queue pressure,
deletion backlog, tier-2 spend burn-down.

The dead-letter queue references the raw envelope and carries only
allowlisted diagnostics, decoder version, and error history — with an
alert on insert, not just a log line.
