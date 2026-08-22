# Operate

These procedures are the production-release drills. They do not replace the
1M-call load target; they make restore, deletion, and rotation exercisable.

Supported path: `docker compose up -d && obsalt serve` plus `obsalt worker`.
There is no SQLite or Postgres-only production mode.

## Health

`GET /health` is liveness. `GET /ready` reports inbox age, outbox depth,
DLQ depth, orphan-blob count, and deletion backlog. `GET /metrics` is
Prometheus.

Page on: inbox/outbox age, orphan-blob count, decode failure rate by plugin,
DLQ depth, late-root frequency, promotion failures, unmapped provider codes,
unmapped-attribute rate, per-org queue pressure, deletion backlog
(`completed_at IS NULL`), and tier-2 spend burn-down.

OTLP capacity pressure is a retryable HTTP 503 / gRPC `UNAVAILABLE` when
outbox depth exceeds `OBSALT_OUTBOX_BACKPRESSURE_LIMIT`.

## Restore from raw

Raw blobs are unredacted and short-lived (default 30 days). Replay
re-decodes retained wire bytes. It cannot invent data past the raw or
provider horizon.

1. Confirm the envelope is still in object storage:
   `org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}`.
2. `POST /v1/replay` with `provider` and `source_call_id` (or `call_id`).
   Decode still runs in a worker.
3. The worker promotes a **new** call revision. The Postgres active-revision
   pointer CAS onto the new revision; ClickHouse keeps the previous
   immutable snapshot.
4. Fleet rollups publish a new serving generation. Call-detail reads the
   pointer first and fetches that exact ClickHouse revision.

`obsalt retain` reports the replay horizon. Expired raw data cannot be
re-decoded.

## Verified deletion

Deletion cannot be undone. Tombstones are checked by receive, replay,
backfill, indexing, and export.

1. `POST /v1/privacy/deletion-requests` with `call_id`, `caller` /
   `caller_token`, or `start`/`end`.
2. Receive writes a tombstone and a `deletion_requests` row
   (`status=accepted`).
3. The job deletes the Postgres pointer and search document, masks then
   deletes ClickHouse facts, purges org-namespaced evidence objects, and
   rebuilds rollups.
4. Completion writes `deletion_requests.completed_at` and
   `status=completed`. A deletion that never sets `completed_at` is not a
   deletion.

Caller lookup uses a per-org keyed HMAC token retained only for privacy
operations. Evidence is org-namespaced; objects are never deduplicated
across tenants.

External warehouse copies cannot be revoked. The API says so.

## Secret rotation with overlap

API keys and outbound webhook secrets rotate without downtime.

- `POST /v1/keys/rotate` inserts a new hashed key and sets `expires_at` on
  the previous key (`OBSALT_KEY_ROTATION_OVERLAP_SECONDS`, default 24h).
  Both hashes accept requests until the old key expires.
- `POST /v1/outbound-webhooks/{id}/rotate` moves the current `whsec_`
  material to `previous_secret_*` with an expiry. Delivery signs with both
  secrets (`webhook-signature` is space-delimited `v1,...` values) until
  the overlap ends.

Revoke after the overlap if the old secret was exposed.

## Retention

Finite defaults: 30 days raw, 90 days transcripts, 400 days aggregates.

`obsalt retain` runs `sweep` (raw blobs + transcript rows + aggregate
contributions). Orphan raw objects without an inbox row are purged.

## Backup expiry

Managed backups expire. Tombstones survive restore.

1. `record_backup` stamps `taken_at` and `expires_at`
   (`OBSALT_BACKUP_RETENTION_DAYS`, default 30).
2. `obsalt retain` and every deletion job call `expire_backups`. Expired
   backups cannot restore a call.
3. `restore_allowed` refuses a tombstoned `source_call_id` or `caller_token`
   even when a retained backup still exists. Deletion cannot be undone by
   restoring from a managed backup, expired or not.

This drill is the production-readiness check for delete-by-caller through
the backup lifecycle.

## Parquet export

```bash
obsalt export --org acme --dest ./exports/acme
# or POST /v1/export
```

Manifests record call revision and deletion status. obsalt propagates
deletion to managed exports. It cannot revoke copies moved to an external
warehouse.

## Egress

Backfill, judges, embedders, OTLP destinations, recording retrieval, and
outbound webhooks share one egress policy. It validates schemes and ports,
blocks internal address ranges by default, re-resolves DNS after redirects,
and applies per-destination credentials without exposing them to unrelated
plugins.

Outbound webhooks are HTTPS-only and follow
[Standard Webhooks](https://www.standardwebhooks.com/). Payloads are
PII-minimal. Finalization retries never create a second logical outbound
event.

## Keys and roles

Service API and ingest keys are high-entropy, hashed at rest, org-bound,
scoped (`ingest`, `read`, `analyze`, `admin`), expiring or explicitly
non-expiring, revocable, and rotatable with overlap.

Browser users authenticate through secure server-side sessions. Sessions use
secure / HTTP-only / SameSite cookies and CSRF protection. Roles (`owner`,
`admin`, `analyst`, `reviewer`) map to an endpoint authorization matrix.

Every resource lookup includes the authenticated `org_id`. Cross-org
identifiers return 404.

Recoverable provider, judge, embedder, and outbound-webhook secrets are
envelope-encrypted, narrowly decrypted, redacted from logs, audited on use,
and never returned after creation.
