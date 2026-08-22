# Style guide

Opinionated conventions for this repository. The goal is one voice: the same
import path, the same name for the same concept, the same test layout.

Ruff is the formatter and linter. Line length 100. Target Python 3.11.
`make format` applies it; `make lint` checks it.

## Language and types

- Python 3.11+, `from __future__ import annotations` in every module.
- Pydantic v2 models for domain objects and plugin types. `StrEnum` for
  closed vocabularies.
- Absolute imports: `from obsalt.domain.events import NormalizedEvent`.
- Public packages ship `py.typed`.
- Strict mypy on `obsalt`, `obsalt_testkit`, `obsalt_example`,
  `obsalt_vapi`, `obsalt_retell`, checked as Python 3.12 so vendor stubs
  that use `type` statements parse. `requires-python` remains `>=3.11`.
  New core code must type-check. The CI typecheck job is informational
  until the existing backlog is cleared.

## Names that are stable

Do not rename these for taste. They are the product vocabulary:

`CallRevision`, `NormalizedEvent`, `StageMeasurement`, `AggregateMeasurement`,
`SignalCoverage`, `FidelityDeclaration`, `RawEnvelope`, `TimelineFidelity`,
`MeasurementPlacement`, `PipelineArchitecture`, `Provenance`.

Event types stay `*Observed` / `CallFinalized` / `FactRetracted`.

HTTP paths stay `/v1/ingest/{provider}/{ingest_key}`, `/v1/traces`,
`/v1/calls`, `/v1/outbound-webhooks`, `/v1/privacy/deletion-requests`.

Plugin entry-point keys stay `vapi`, `retell`, `elevenlabs`, `cartesia`,
`openai_realtime`, `gemini_live`, `pipecat`, `livekit`, `example`.

Environment variables stay `OBSALT_*`.

## Naming rules

| Kind | Convention | Example |
| --- | --- | --- |
| Modules | Full words, no `_pg` / `_ch` abbreviations | `obsalt.store.trace`, `obsalt.store.forward` |
| Store classes | `{Backend}{Concern}` | `PostgresInbox`, `ClickHouseSink` |
| Tenant identity | `org_id` everywhere | `VoiceCall.start(..., org_id=...)` |
| Decoder identity | `"{plugin}/{n}"` | `vapi/3`, `retell/2`, `example/1` |
| Loggers | Module path | `logging.getLogger("obsalt.analysis.contributions")` |
| Tests | Directory is the marker | `tests/unit/test_assembler.py` |

### otel vs otlp

- **Code package:** `obsalt.otel` — OpenTelemetry SDK types and conventions.
- **Wire and settings:** `otlp` — `receive_otlp_batch`, `OBSALT_OTLP_GRPC_PORT`.
- **HTTP path:** `POST /v1/traces` — the OTLP export URL users already know.
  Do not rename it to `/v1/otlp` or `/v1/otel`.

### Connection strings

Postgres uses a libpq **DSN**. ClickHouse, Redis, and S3 use **URLs**. That
suffix difference is intentional.

### Plugin package names

Three related identifiers, one mapping:

| PyPI | Entry point / `name` | Extra |
| --- | --- | --- |
| `obsalt-openai-realtime` | `openai_realtime` | `openai-realtime` |
| `obsalt-gemini-live` | `gemini_live` | `gemini-live` |
| `obsalt-vapi` | `vapi` | `vapi` |

Hyphenated distribution, underscored Python / entry-point identifier.

## Imports

Plugins and application code import the public surface:

```python
from obsalt.plugin import PLUGIN_API_VERSION, Capability, WebhookSource
from obsalt import VoiceCall, __version__
```

Never `from obsalt._version import PLUGIN_API_VERSION` outside core.
`obsalt._version` is an implementation module. Core internals may import it.

Do not import `_`-prefixed helpers across package boundaries. If another
module needs it, give it a public name (`as_clickhouse_datetime`, not
`_ch_dt`).

## Plugins

Every plugin class defines:

```python
class RetellPlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "retell"
    display_name = "Retell"
    decoder_version = "retell/2"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-retell-signature"})
    manifest = PluginManifest(...)
    fidelity = FidelityDeclaration(...)
```

`decoder_version` is required. Core falls back to `"{name}/1"` only so a
broken plugin does not crash the worker; that fallback is not a license to
omit the attribute.

Hosted webhook plugins ship:

```
fixtures/schema/          vendored vendor schema + PIN.json
fixtures/schema_overlays/ reviewed additive patches, if any
fixtures/raw/             real-shaped payloads
fixtures/expected/        NormalizedEvent[] goldens
fixtures/ignore_fields
fixtures/bypass_reasons
data/                     enum maps (ended reasons, …)
```

OTLP / SDK plugins ship `fixtures/otlp/*.json`. Dual plugins (ElevenLabs)
ship both trees.

Every plugin `pyproject.toml` declares `license`,
`[tool.setuptools.package-data]` for `py.typed` and fixtures, and the
`obsalt.plugins` entry point.

## Tests

```
tests/unit/           pure functions, no HTTP
tests/integration/    multi-component, memory doubles OK
tests/blackbox/       HTTP-only assertions
tests/contract/       OTLP, SQL, SSRF, schema fixtures
tests/conformance/    plugin testkit subclasses
tests/property/       hypothesis
tests/security/       auth, tenancy, SSRF
tests/helpers.py      fixture paths and signed-header helpers
tests/conftest.py     fixtures + auto-markers from directory
```

Directory is the marker. `pytest -m unit` is a real fast path. Do not put
new `test_*.py` files in `tests/` root.

- Use `tests.helpers` for fixture paths. Do not redefine
  `FIXTURES = Path(__file__)...`.
- Use `create_test_app()` or `tests.helpers.api_client`. The one test that
  asserts `create_app()` fails without state is the exception.
- Memory stores are test doubles. They are not a production backend. Do not
  document them as one.

## HTTP and CLI

- Collection endpoints are paginated with cursors and require a bounded time
  range.
- Cross-org identifiers return 404, not 403.
- CLI subcommands are kebab-case (`schema-drift`, `record-golden`).

## Comments and docs

- Comments explain non-obvious invariants, not what the next line does.
- Module docstrings on public packages and on root modules (`api`, `config`,
  `cli`, `util`).
- Do not cite deleted rewrite-plan section numbers (`§6.2`) in new code.
  Point at `docs/` pages.

## What not to add

- A second storage backend or `require_auth=false`.
- Dead aliases (`spend_for`, `ClickHouseRevisionSink`) "for compatibility"
  with ourselves.
- Span names that embed indexes, provider names, or tool names.
- Conversational content outside `obsalt.pii.*`.
