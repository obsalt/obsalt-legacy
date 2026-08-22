# Developing obsalt

obsalt is a Python monorepo. Core lives in `packages/obsalt`. Providers
live in their own packages and must keep working against the public
plugin contract.

If you are connecting a voice agent, you want [Getting started](getting-started.md),
not this page.

## Local setup

Python 3.11+. Docker for the durable stack.

```bash
python -m pip install -r requirements-dev.txt
make test-unit          # no Docker
docker compose up -d
obsalt doctor
```

`make ci` is lint + typecheck + the full suite — the same gates as GitHub
Actions.

| Command | What it does |
| --- | --- |
| `make install` | Editable install of core, testkit, and first-party plugins |
| `make lint` / `make format` | Ruff |
| `make typecheck` | Strict mypy on core, testkit, example, vapi, retell. CI warns until the backlog is cleared. |
| `make test` / `make test-unit` | pytest |
| `obsalt parse FILE --provider vapi` | Decode without ingesting |
| `obsalt record-golden FILE --provider vapi` | Write `fixtures/expected/` (review the diff) |
| `obsalt schema-drift --all` | Offline vendored-pin check |

## Layout

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
  session.py    VoiceCall
packages/obsalt-testkit/     inheritable conformance tests
packages/obsalt-<provider>/  first-party plugins
tests/
  unit/ integration/ blackbox/ contract/ conformance/ property/ security/
```

## Style, briefly

Ruff is the formatter and linter. Line length 100. Target Python 3.11.
`from __future__ import annotations` in every module. Absolute imports.
Pydantic 2 models for domain objects. `StrEnum` for closed vocabularies.

Do not rename these for taste — they are the product vocabulary:

`CallRevision`, `NormalizedEvent`, `StageMeasurement`,
`AggregateMeasurement`, `SignalCoverage`, `FidelityDeclaration`,
`RawEnvelope`, `TimelineFidelity`, `MeasurementPlacement`,
`PipelineArchitecture`, `Provenance`.

HTTP paths stay `/v1/ingest/{provider}/{ingest_key}`, `/v1/traces`,
`/v1/calls`. Plugin entry points stay `vapi`, `retell`, `elevenlabs`,
`cartesia`, `openai_realtime`, `gemini_live`, `pipecat`, `livekit`,
`example`. Environment variables stay `OBSALT_*`.

Plugins import `PLUGIN_API_VERSION` from `obsalt.plugin`.
Tests use `create_test_app()`. `create_app()` without a state is not a
memory backend. Tenant identity is `org_id` everywhere, including
`VoiceCall.start`.

## Pull request bar

- One concern per PR.
- Tests for the behavior you changed. Plugin changes need fixture +
  golden + schema validation.
- No invented provider fields. If a captured payload is ahead of the
  vendor schema, use a reviewed, expiring schema overlay.
- Docs if you change a product surface, a public contract, or how
  someone connects an agent.
- `make ci` green.

## Adding a provider plugin

1. Copy `packages/obsalt-example` or a hosted plugin that matches the
   capability (webhook vs OTLP mapper).
2. Register on `obsalt.plugins`.
3. Set `name`, `display_name`, `decoder_version` (`"{name}/{n}"`),
   `capabilities`, `manifest`, `fidelity`.
4. Vendor the provider schema, captured payloads, and golden outputs.
5. Subclass the relevant `obsalt-testkit` conformance classes.
6. Add the package to `requirements-dev.txt` and the extras in
   `packages/obsalt/pyproject.toml`.
7. Document how a user connects it and what they will see — in
   [connect-hosted](connect-hosted.md) or [connect-custom](connect-custom.md),
   and in the table on [the console](console.md).

Read [Write a plugin](plugins.md) before writing code.

## Tests

Tests live under `tests/` by kind. The directory is the marker.

| Directory | Marker | Meaning |
| --- | --- | --- |
| `tests/unit/` | `unit` | Pure functions, no HTTP |
| `tests/integration/` | `integration` | Multi-component, memory doubles OK |
| `tests/blackbox/` | `blackbox` | HTTP status codes and JSON only |
| `tests/contract/` | `contract` | OTLP, SQL, SSRF, schema fixtures |
| `tests/conformance/` | `conformance` | Plugin testkit subclasses |
| `tests/property/` | `property` | Assembler fold properties |
| `tests/security/` | `security` | Auth, tenancy, SSRF |

Use `tests.helpers` for fixture paths and signed headers. Use
`create_test_app()`. Memory stores are doubles, not a backend.

Webhook plugins must validate `fixtures/raw/` against a vendored vendor
schema. Overlays are allowed only as reviewed, additive, expiring patches
that still report the original vendor mismatch. `obsalt schema-drift`
compares the pin; updating a pin is a reviewed schema + fixture + golden
diff.

## Do not

- Add a second storage backend.
- Decode on the webhook request path.
- Draw a waterfall from unplaced durations.
- Let a payload field or OTLP resource attribute choose `org_id`.
- Treat missing Tier-2 output as a passing eval.
- Introduce `require_auth=false`.
- Re-introduce a private "POST us a JSON snapshot" SDK shape. Custom
  agents emit OTLP.

## Known gaps (useful, not a roadmap ceremony)

- `StreamSource` is declared. There is no first-party Deepgram tap.
- Bland is not a first-party plugin.
- OTLP gRPC is opt-in (`obsalt[grpc]`).
- There is no OIDC login. Bootstrap key + hashed service keys +
  session cookies.
- Langfuse-shaped ingest is intentionally not implemented.

Architecture: [architecture](architecture.md).
