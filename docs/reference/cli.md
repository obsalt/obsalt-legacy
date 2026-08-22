# CLI

```
obsalt [-V] <command>
```

| Command | Purpose |
| --- | --- |
| `serve` | HTTP API against the compose stack |
| `worker` | Drain the outbox. Decode never runs on the webhook path. |
| `demo` | Ephemeral full stack. Not for production. |
| `init` | Write `.env.example` |
| `doctor` | Print config, plugins, insecure-default warnings |
| `parse PATH --provider NAME` | Decode a payload without ingesting |
| `record-golden PATH --provider NAME` | Write `fixtures/expected/` from a raw payload |
| `schema-drift [--all] [--remote FILE]` | Compare vendored schema pins |
| `retain` | Sweep expired raw, transcript, and aggregate data |
| `export --org ORG --dest DIR` | Parquet / JSONL manifest of active revisions |
| `version` | Print `2.0.0` |

`serve --in-memory` exists for tests. It is forbidden in production and
prints a warning. `obsalt serve` without the compose stack exits 2 and
tells you there is no SQLite or Postgres-only mode.

`worker --once` processes one batch and exits. `worker --poll 1.0` is the
long-running loop.

`schema-drift --all` is offline and reproducible. Supply `--remote` only
when you have a refetched vendor schema to diff. Updating a pin requires a
reviewed schema, fixture, and expected-output diff.
