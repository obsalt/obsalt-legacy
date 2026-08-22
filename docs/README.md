# obsalt documentation

obsalt v2 is specified by [rewrite-plan.md](rewrite-plan.md). That document is
the architecture source of truth.

- [Product](product.md) — six capabilities, UI surfaces, non-goals
- [Plugins](plugins.md) — public contract, trusted-install model, packaging
- [Ingest](ingest.md) — webhooks, OTLP, tenancy, fail-closed auth
- [Storage](storage.md) — Postgres + ClickHouse + object storage
- [API](api.md) — `/v1` routes
- [Providers](providers/index.md)

v0.1 docs that described in-memory adapters and span synthesis are withdrawn.
They described a system this rewrite replaces.
