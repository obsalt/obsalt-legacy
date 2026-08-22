# Operate

Supported path: `docker compose up -d && obsalt serve` plus `obsalt worker`.
There is no SQLite or Postgres-only production mode.

Replace every `change-me` and `dev-key` before the process is reachable
from a network you do not trust. `/ready` reports `insecure_defaults`.

## Health

`GET /health` is liveness. `GET /ready` reports inbox age, outbox depth,
DLQ depth, orphan-blob count, and deletion backlog. `GET /metrics` is
Prometheus.

Page on: inbox/outbox age, orphan blobs, decode failure rate by plugin,
DLQ depth, late-root frequency, promotion failures, unmapped provider
codes, unmapped-attribute rate, per-org queue pressure, deletion backlog
(`completed_at IS NULL`), and tier-2 spend burn-down.

OTLP capacity pressure is a retryable HTTP 503 when outbox depth exceeds
`OBSALT_OUTBOX_BACKPRESSURE_LIMIT`.

## Restore from raw

Raw blobs are unredacted and short-lived (default 30 days). Replay
re-decodes retained wire bytes. It cannot invent data past the raw or
provider horizon.

1. Confirm the envelope is still in object storage:
   `org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}`.
2. `POST /v1/replay` with `provider` and `source_call_id` (or `call_id`).
   Decode still runs in a worker.
3. The worker promotes a **new** call revision. ClickHouse keeps the
   previous snapshot. Postgres CAS-points at the new one.
4. Fleet rollups publish a new serving generation.

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
   deletes ClickHouse facts, purges org-namespaced evidence, rebuilds
   rollups.
4. Completion sets `completed_at` and `status=completed`. A deletion
   that never sets `completed_at` is not a deletion.

Caller lookup uses a per-org keyed HMAC token retained only for privacy
operations. Evidence is org-namespaced.

External warehouse copies cannot be revoked. The API says so.

## Secret rotation with overlap

- `POST /v1/keys/rotate` inserts a new hashed key and expires the
  previous one (`OBSALT_KEY_ROTATION_OVERLAP_SECONDS`, default 24h).
  Both accept requests until the old key expires.
- `POST /v1/outbound-webhooks/{id}/rotate` keeps the previous `whsec_`
  material through the overlap. Delivery signs with both
  (`webhook-signature` is space-delimited `v1,…` values).

Revoke after the overlap if the old secret was exposed.

## Retention

Defaults: 30 days raw, 90 days transcripts, 400 days aggregates.

`obsalt retain` sweeps expired raw, transcript, and aggregate data, and
purges orphan raw objects that have no inbox row.

## Backup expiry

Managed backups expire. Tombstones survive restore.

1. `record_backup` stamps `taken_at` and `expires_at`
   (`OBSALT_BACKUP_RETENTION_DAYS`, default 30).
2. `obsalt retain` and every deletion job call `expire_backups`.
3. `restore_allowed` refuses a tombstoned `source_call_id` or
   `caller_token` even when a retained backup still exists.

## Parquet export

```bash
obsalt export --org acme --dest ./exports/acme
# or POST /v1/export
```

Manifests record call revision and deletion status. obsalt propagates
deletion to managed exports. It cannot revoke copies moved to an
external warehouse.

## Egress

Backfill, judges, embedders, OTLP destinations, recording retrieval, and
outbound webhooks share one egress policy: scheme/port checks, no
private ranges by default, DNS re-resolved after redirects, per-
destination credentials.

Outbound webhooks are HTTPS-only and follow
[Standard Webhooks](https://www.standardwebhooks.com/). Payloads are
PII-minimal. Finalization retries never create a second logical event.

## Keys and roles

Service keys are high-entropy, hashed at rest, org-bound, scoped
(`ingest`, `read`, `analyze`, `admin`), rotatable with overlap.

Browser users get secure / HTTP-only / SameSite cookies and CSRF.
Roles (`owner`, `admin`, `analyst`, `reviewer`) map to an endpoint
matrix. Cross-org identifiers return 404.

Recoverable secrets are envelope-encrypted, narrowly decrypted,
redacted from logs, audited on use, and never returned after creation.

Full setting list: [configuration](reference/configuration.md).
