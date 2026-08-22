# Architecture

Receive writes the raw body to object storage and commits a Postgres inbox/outbox row before the provider-specific success response. Decode is a pure function from that raw envelope to `NormalizedEvent[]`. Redaction is a single choke point on the normalized stream. The assembler folds facts into a complete immutable `CallRevision`. Postgres compare-and-swaps the active-revision pointer after ClickHouse has the candidate.

A timeline is only drawn from measurements with real timestamps (T1). Every value carries provenance (T2). Adapter bugs become replays (T3). Fixtures validate against vendored provider schemas (T4).

See [rewrite-plan.md](rewrite-plan.md) §4–§6 for the stage contracts and promotion protocol.
