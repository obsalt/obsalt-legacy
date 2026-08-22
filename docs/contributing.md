# Contributing

obsalt is a Python monorepo. Core lives in `packages/obsalt`. Providers live
in their own packages and must keep working against the public plugin
contract.

## Local setup

Python 3.11+. Docker for the durable stack.

```bash
python -m pip install -r requirements-dev.txt
make test-unit          # no Docker
docker compose up -d    # Postgres, ClickHouse, Redis, MinIO
obsalt doctor
```

`make ci` runs lint, typecheck, and the full suite — the same gates as GitHub
Actions.

Useful commands:

| Command | What it does |
| --- | --- |
| `make install` | Editable install of core, testkit, and first-party plugins |
| `make lint` | `ruff check` + `ruff format --check` |
| `make format` | Apply Ruff |
| `make typecheck` | Strict mypy on core, testkit, example, vapi, retell. Informational in CI until the existing backlog is cleared. |
| `make test` | Full pytest |
| `make test-unit` | `pytest -m unit` |
| `obsalt parse FILE --provider vapi` | Decode a payload without ingesting it |
| `obsalt record-golden FILE --provider vapi` | Write `fixtures/expected/` (review the diff) |
| `obsalt schema-drift --all` | Offline vendored-pin check |

## How the repo is laid out

```
packages/obsalt/src/obsalt/
  domain/       events, models, enums, coverage
  ingest/       webhook + OTLP receive
  assemble/     fold facts → CallRevision, promote
  redact/       the single redaction choke point
  store/        Postgres, ClickHouse, object storage
  otel/         conventions, mappers, forwarder, receiver
  analysis/     tier 1, tier 2, hangup, rollups
  search/       lexical + vector
  plugin/       public contract and host
  api.py        HTTP API + UI
  cli.py        obsalt command
packages/obsalt-testkit/     inheritable conformance tests
packages/obsalt-<provider>/  first-party plugins
tests/
  unit/ integration/ blackbox/ contract/ conformance/ property/ security/
```

Core ships no providers. If you are adding a source, add a package.

## Pull request bar

- One concern per PR. Do not mix a decoder fix with a CI rewrite.
- Tests for the behavior you changed. Plugin changes need fixture + golden
  + schema validation.
- No invented provider fields. If a captured payload is ahead of the vendor
  schema, use the reviewed overlay process in
  [testing](reference/testing.md#schema-overlays).
- Docs if you change a product surface, a public contract, or a decision.
- `make ci` green.

Use the pull request template. Say why, what, and how to verify.

## Naming and style

Follow [style.md](style.md). The short version:

- Domain names in [domain](reference/domain.md) are stable. Do not rename
  `CallRevision`, `NormalizedEvent`, or `StageMeasurement` for taste.
- Plugins import `PLUGIN_API_VERSION` from `obsalt.plugin`, never
  `obsalt._version`.
- Tests use `create_test_app()`. `create_app()` without a state is not a
  memory backend.
- Tenant identity is `org_id` everywhere, including `VoiceCall.start`.

## Adding a provider plugin

1. Copy `packages/obsalt-example` or a hosted plugin that matches the
   capability (webhook vs OTLP mapper).
2. Register on entry-point group `obsalt.plugins`.
3. Set `name`, `display_name`, `decoder_version` (`"{name}/{n}"`),
   `capabilities`, `manifest`, `fidelity`.
4. Vendor the provider schema, captured payloads, and golden outputs.
5. Subclass the relevant `obsalt-testkit` conformance classes.
6. Add the package to `requirements-dev.txt` and the extras in
   `packages/obsalt/pyproject.toml`.
7. Document fidelity, units, and auth in
   [providers](reference/providers.md).

Read [write a plugin](guides/write-a-plugin.md) before writing code.

## Roadmap (build sequence)

v2 was specified as a clean rewrite with no compatibility promises. v0.1
was unreleased and in-memory. Phases 0–5 are the usable product. Phase 6
adds source breadth. Phase 7 is the production-release gate.

| Phase | What it proved | Status |
| --- | --- | --- |
| 0 Foundations | Example plugin discovered, loaded, passes conformance | done |
| 1 Durable spine | Signed webhook persisted, queued, acked; restart-safe; fail-closed auth | done |
| 2 Decode and assemble | Vapi and Retell from pinned schemas; no invented intervals | done |
| 3 OTLP in and out | Pipecat / LiveKit mappers; identity-preserving forward; SSRF tests | done |
| 4 Analysis | Tier 1, index, Tier 2 framework, hangup coverage, hybrid search | done |
| 5 API and UI | `/v1` + provenance panel; the six capabilities work end to end | done |
| 6 Provider breadth | ElevenLabs, Cartesia, OpenAI Realtime, Gemini Live | done |
| 7 Target-scale operations | Restore, delete-by-caller through backups, rotation, load | drills exist; 1M-call load is a cluster exercise |

A phase is done when its exit criterion is demonstrable, not when code was
written. Phase 7's remaining work is operational: restore-from-raw within
the configured horizon, delete-by-caller through every managed store and
backup lifecycle, secret rotation without downtime, and the
[storage sizing](storage.md#sizing-at-the-target) load/query target,
exercised and documented.

## What not to do

- Do not add a second storage backend.
- Do not decode on the webhook request path.
- Do not draw a waterfall from unplaced durations.
- Do not let a payload field or OTLP resource attribute choose `org_id`.
- Do not treat missing Tier-2 output as a passing eval.
- Do not introduce `require_auth=false`.
