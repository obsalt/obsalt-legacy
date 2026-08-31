# Develop

If you are connecting a voice agent, you want [Start](start.md), not
this page.

obsalt is a Python monorepo. Core lives in `packages/obsalt`. Providers
live in their own packages and must keep working against the public
plugin contract.

Coding rules a formatter cannot see: [Conventions](conventions.md).
One-screen version: [AGENTS.md](../AGENTS.md).

## Local setup

Python 3.11+. Docker for the durable stack.

```bash
uv sync --all-packages
make test-unit          # no Docker
docker compose up -d
obsalt doctor
```

`make ci` is lint + typecheck + the full suite — the same gates as
GitHub Actions.

| Command | What it does |
| --- | --- |
| `make help` | List targets |
| `make install` | Editable install of core, testkit, and first-party plugins |
| `make up` | `docker compose up -d` |
| `make lint` / `make format` | Ruff |
| `make typecheck` | Strict mypy. CI warns until the backlog is cleared. |
| `make test` / `make test-unit` | pytest |
| `obsalt parse FILE --provider vapi` | Decode without ingesting |
| `obsalt seed` | Replay vendored fixtures into a running serve over HTTP |
| `obsalt record-golden FILE --provider vapi` | Write `fixtures/expected/` |
| `obsalt schema-drift --all` | Offline vendored-pin check |

## Populate the console

An empty call list after compose is success. To fill it from the same
vendored payloads CI validates:

```bash
docker compose up -d
uv run obsalt seed
```

The empty Calls page **Load sample calls** (dev / test only) queues the
same corpus. `obsalt seed` is a black-box HTTP client. It refuses
`OBSALT_ENVIRONMENT=production`. `--dry-run` schema-validates with no
HTTP.

## Layout

```
packages/obsalt/src/obsalt/
  domain/       events, models, enums, coverage
  ingest/       webhook + OTLP receive
  assemble/     fold facts → CallRevision, promote
  redact/       the single redaction choke point
  store/        ports, Postgres, ClickHouse, S3/MinIO
  otel/         conventions, mappers, forwarder, receiver
  analysis/     tier 1, tier 2, evals, hangup, rollups
  worker/       decode + drain loops
  ops/          sweep, doctor, seed, parquet export
  privacy/      caller tokens; deletion lives in ops
  crypto/       primitives; security/ holds authz, keys, sessions
  webhooks/     outbound delivery
  search/       lexical + vector
  plugin/       public contract and host
  ui/           console presenters and templates
  api.py        HTTP API + UI
  cli.py        obsalt command
  session.py    VoiceCall
packages/obsalt-testkit/     inheritable conformance tests
packages/obsalt-<provider>/  first-party plugins
tests/
  unit/ integration/ blackbox/ contract/ conformance/ property/ security/
```

The test directory **is** the pytest marker.

## Style, briefly

Ruff. Line length 100. Python 3.11+. `from __future__ import annotations`.
Absolute imports. Pydantic 2. `StrEnum`.

Do not rename these for taste: `CallRevision`, `NormalizedEvent`,
`StageMeasurement`, `AggregateMeasurement`, `SignalCoverage`,
`FidelityDeclaration`, `RawEnvelope`, `TimelineFidelity`,
`MeasurementPlacement`, `PipelineArchitecture`, `Provenance`, `org_id`,
`ingest_key`.

HTTP paths stay `/v1/ingest/{provider}/{ingest_key}`, `/v1/traces`,
`/v1/calls`. Tests use `create_test_app()`. `create_app()` without a
state is not a memory backend.

## Pull request bar

- One concern per PR.
- Tests for the behavior you changed. Plugin changes need fixture +
  golden + schema validation.
- No invented provider fields.
- Docs if you change a product surface — update the page a human would
  actually open. See the table in [Conventions](conventions.md).
- `make lint` and `make test-unit` green.

## Next

Why the pipeline looks like this: [Architecture](architecture.md).
Public plugin contract: [Write a plugin](plugins.md).
