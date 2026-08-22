# CLI

The `obsalt` command. Product walkthroughs live in
[getting started](../getting-started.md); this page is the flag list.

```
obsalt [-V] <command>
```

`obsalt` with no command prints help plus a typical local path
(`compose` → `init` → `doctor` → `serve` / `worker`).

| Command | Purpose | Exit ≠ 0 |
| --- | --- | --- |
| `serve [--host] [--port] [--in-memory]` | HTTP API + console against the compose stack | `2` if the durable stack is down |
| `worker [--poll 1.0] [--once]` | Drain the outbox. Decode never runs on the webhook path. | `2` if the stack is down |
| `demo` | Ephemeral compose + serve. Not for production. | `2` if compose is missing or never becomes ready |
| `init [--dir .] [--write-env]` | Write `.env.example`. `--write-env` also writes `.env` when missing. | — |
| `doctor [--skip-network] [--json]` | Plugins + probe Postgres / ClickHouse / object store / Redis | `2` required store down; `1` no plugins |
| `plugins [--json]` | List installed source plugins | `1` if none |
| `parse PATH --provider NAME` | Decode a payload without ingesting | `2` if the plugin is not installed |
| `record-golden PATH --provider NAME [--out]` | Write `fixtures/expected/` from a raw payload | `2` if the plugin is not installed |
| `schema-drift [--all] [--remote FILE]` | Compare vendored schema pins | `1` if diverged |
| `retain` | Sweep expired raw, transcript, and aggregate data | `2` if the stack is down |
| `export --org ORG --dest DIR` | Parquet / JSONL manifest of active revisions | `2` if the stack is down |
| `version` / `-V` | Print the packaging version | — |

`serve --in-memory` exists for tests. It is forbidden in production and
prints a warning. `obsalt serve` without the compose stack exits 2 and
tells you there is no SQLite or Postgres-only mode.

`worker --once` processes one batch and exits. `worker --poll 1.0` is the
long-running loop.

`doctor` does **not** start a memory backend when stores are down. Redis
is optional: a Redis failure is reported and is not fatal. `/ready` is
the in-process health surface once `serve` is up.

`schema-drift --all` is offline and reproducible. Supply `--remote` only
when you have a refetched vendor schema to diff. Updating a pin requires a
reviewed schema, fixture, and expected-output diff.

Makefile wrappers: `make help`, `make up`, `make serve`, `make worker`,
`make doctor`, `make test-unit`.
