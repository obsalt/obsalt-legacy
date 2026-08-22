# Write a plugin

Core ships no providers. A plugin is a separately installable package
registered on the `obsalt.plugins` entry-point group. First-party providers
use that **same** group. If you need a privileged path, the public API is
already rotting.

Plugins are **trusted, operator-installed code**. They are pinned,
inventoried, and receive only the credentials needed for their capability.
Loading isolates version mismatches and ordinary exceptions; that is not a
security sandbox. An arbitrary wheel can still read process memory.
Tenant-installable plugins need a future out-of-process runtime.

## Skeleton

```python
from datetime import date

from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin import (
    PLUGIN_API_VERSION,
    Capability,
    ConnectionConfig,
    PluginManifest,
    RawEnvelope,
    VerifyResult,
    WebhookResponse,
)
from obsalt.domain.enums import (
    MeasurementPlacement,
    ObservationalEventKind,
    PipelineArchitecture,
    Signal,
    VerifyOutcome,
)
from obsalt.domain.events import NormalizedEvent

class AcmePlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "acme"
    display_name = "Acme Voice"
    decoder_version = "acme/1"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-acme-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="acme.v1",
        possible_architectures=frozenset({PipelineArchitecture.CASCADE}),
        possible_placements=frozenset({MeasurementPlacement.UNPLACED}),
        provides=frozenset({Signal.TRANSCRIPT, Signal.HANGUP}),
        structurally_absent={Signal.STAGE_INTERVAL: "Acme publishes durations without clocks"},
        schema_source="https://example.com/acme.openapi.json",
        schema_revision="2026-08-01",
        verified_at=date(2026, 8, 22),
    )

    def authenticate(self, raw: bytes, headers, cfg: ConnectionConfig) -> VerifyResult:
        ...

    def classify(self, raw: bytes) -> ObservationalEventKind:
        ...

    def delivery_key(self, raw: bytes, headers) -> str | None:
        ...

    def decode(self, envelope: RawEnvelope) -> list[NormalizedEvent]:
        ...
```

```toml
[project.entry-points."obsalt.plugins"]
acme = "acme_obsalt.plugin:AcmePlugin"
```

## Capabilities

| Capability | What you implement |
| --- | --- |
| `webhook_source` | authenticate, classify, delivery_key, tombstone_hints, acknowledgement, decode |
| `otlp_mapper` | `claims(span) -> int` (0 = not mine), `decode(spans)` |
| `rest_backfill` | `scan`, `hydrate` |
| `stream_source` | `frames` — declared in v2, unimplemented first-party |
| `sdk_instrumentation` | wrap a vendor client |
| `authentication` | fail-closed verify with explicit outcomes |
| `judge` / `embedder` / `redactor` | analysis plugins |

`VerifyResult` outcomes are `ok`, `malformed`, `missing_credential`,
`bad_signature`, `stale`, `replayed`. No authentication capability may
silently disable itself.

Core provides tested primitives (`hmac_hex`, `hmac_base64`,
`parse_kv_header`, `ed25519_verify`, `enforce_window`, `constant_time_eq`,
JWT validation) so plugins compose rather than reimplement.

## Decode rules

Decoders emit small facts, not whole-call snapshots. Core stamps
`(org_id, call_key, fact_id, envelope_id, decoder_version, processing_run_id, …)`.
A plugin may emit optional `source_revision`; core validates its declared
ordered type before using it for precedence.

- `fact_id` is deterministic for a source fact.
- A content hash identifies content but does not establish which content is
  newer.
- For the same `fact_id`: identical content dedupes; a greater ordered
  source revision wins; differing content without a comparable revision is
  a visible conflict, not an overwrite.
- Snapshot decoders emit `SnapshotBoundaryObserved` and may retract omitted
  facts only for domains the provider documents as authoritative. Delta
  events never imply retraction by omission.

Populate grounding where the provider supplies it. In v0.1
`grounding.tool_results` and `grounding.user_text` were never set, which
silently crippled hallucination detection. That is now a conformance
requirement.

Never emit an `INTERVAL` placement without real start and end. The fidelity
declaration is tested against decode output. A plugin that claims
`INTERVAL` but produces a value without timestamps fails. A plugin cannot
mark a signal structurally unsupported while fixtures contain an unconsumed
source field for it.

`FieldMap` covers the boring majority. Discrimination, unit normalization,
and tool pairing stay as code.

## Fixtures

```
fixtures/
  schema/            vendored provider OpenAPI/JSON Schema + PIN.json
  schema_overlays/   reviewed additive patches
  raw/               real-shaped payloads, one per event type
  expected/          NormalizedEvent[] each raw payload must produce
  ignore_fields
  bypass_reasons
```

```bash
obsalt record-golden path/to/raw.json --provider acme
```

That writes `fixtures/expected/`. Review the diff. Golden-file testing that
is tedious to update rots; this is what keeps the corpus alive.

Subclass `obsalt-testkit` conformance classes. Skipping a required test
needs an explicit marker plus a written reason.

## Example plugin

`packages/obsalt-example` is the Phase 0 exit criterion: a trivial plugin in
a separate package that is discovered, loaded, and passes the conformance
kit. Copy it.

`StreamSource` is implemented there as an example so Deepgram is additive
and does not require a core change.
