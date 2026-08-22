# Storage

Committed architecture: Postgres + ClickHouse + object storage. Not optional.
Not abstracted behind a pluggable-backend layer.

| Store | Holds |
| --- | --- |
| ClickHouse | Immutable call revisions, turns, measurements, tools, analysis, rollups |
| Postgres | Inbox, dedupe, outbox, tenants, active-revision pointers, search, keys |
| Object storage | Raw blobs, evidence, recordings |

Raw blobs are unredacted by definition: encrypted, short TTL (30 days),
excluded from normal read paths. Replayability is bounded by that horizon.

`docker compose up` is the supported path. `obsalt demo` is ephemeral and
loud about not being production. There is no SQLite/Postgres-only mode.

See [rewrite-plan.md](rewrite-plan.md) §10.
