# Operations runbook (Phase 7)

These procedures are the production-release drills from `docs/rewrite-plan.md` §12.3 and Phase 7. They do not replace the 1M-call load target; they make restore, deletion, and rotation exercisable.

## Restore from raw

Raw blobs are unredacted and short-lived (default 30 days). Replay re-decodes retained wire bytes; it cannot invent data past the raw or provider horizon.

1. Confirm the envelope is still in object storage: `org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}`.
2. `POST /v1/replay` with `provider` and `source_call_id` (or `call_id`). This requeues the envelope. Decode still runs in a worker.
3. The worker promotes a **new** call revision. The Postgres active-revision pointer CAS onto the new revision; ClickHouse keeps the previous immutable snapshot.
4. Fleet rollups publish a new serving generation (`rollup_generations.fleet`). Call-detail reads the pointer first and fetches that exact ClickHouse revision.

`obsalt retain` reports the replay horizon. Expired raw data cannot be re-decoded.

## Verified deletion

Deletion cannot be undone. Tombstones are checked by receive, replay, backfill, indexing, and export.

1. `POST /v1/privacy/deletion-requests` with `call_id`, `caller` / `caller_token`, or `start`/`end`.
2. Receive writes a tombstone and a `deletion_requests` row (`status=accepted`).
3. The job deletes the Postgres pointer and search document, masks then deletes ClickHouse facts, purges org-namespaced evidence objects, and rebuilds rollups.
4. Completion writes `deletion_requests.completed_at` and `status=completed`. A deletion that never sets `completed_at` is not a deletion.

External warehouse copies cannot be revoked; the API says so.

## Secret rotation with overlap

API keys and outbound webhook secrets rotate without downtime.

- `POST /v1/keys/rotate` inserts a new hashed key and sets `expires_at` on the previous key (`OBSALT_KEY_ROTATION_OVERLAP_SECONDS`, default 24h). Both hashes accept requests until the old key expires.
- `POST /v1/outbound-webhooks/{id}/rotate` moves the current `whsec_` material to `previous_secret_*` with an expiry. Delivery signs with both secrets (`webhook-signature` is space-delimited `v1,...` values) until the overlap ends.

Revoke after the overlap if the old secret was exposed.

## Retention

Finite defaults: 30 days raw, 90 days transcripts, 400 days aggregates.

`obsalt retain` runs `sweep` (raw blobs + transcript rows + aggregate contributions). Orphan raw objects without an inbox row are purged.

## Alerting signals worth paging

Inbox/outbox age, orphan-blob count, decode failure rate by plugin, DLQ inserts, promotion failures, unmapped provider codes, deletion backlog (`completed_at IS NULL`), and tier-2 spend burn-down.

OTLP capacity pressure is a retryable HTTP 503 / gRPC `UNAVAILABLE` when outbox depth exceeds `OBSALT_OUTBOX_BACKPRESSURE_LIMIT`.
