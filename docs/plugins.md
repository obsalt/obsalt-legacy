# Write a plugin

Core ships no providers. A plugin is a separately installable package on
the `obsalt.plugins` entry-point group. First-party providers use that
**same** group. If you need a privileged path, the public API is already
rotting.

Plugins are **trusted, operator-installed code**. They are pinned and
inventoried. Loading isolates version mismatches and ordinary exceptions.
That is not a security sandbox. An arbitrary wheel can still read process
memory.

Copy `packages/obsalt-example` and rename things. It is a webhook source
that is discovered, loaded, and expected to pass the conformance kit.

## Skeleton

```python
from datetime import date

from obsalt.domain.enums import (
    Capability,
    MeasurementPlacement,
    ObservationalEventKind,
    PipelineArchitecture,
    Signal,
    VerifyOutcome,
)
from obsalt.domain.events import NormalizedEvent
from obsalt.domain.models import FidelityDeclaration
from obsalt.plugin import (
    PLUGIN_API_VERSION,
    ConnectionConfig,
    PluginManifest,
    RawEnvelope,
    VerifyResult,
    WebhookResponse,
)

class AcmePlugin:
    API_VERSION = PLUGIN_API_VERSION
    name = "acme"
    display_name = "Acme Voice"
    decoder_version = "acme/1"
    capabilities = frozenset({Capability.WEBHOOK_SOURCE, Capability.AUTHENTICATION})
    singleton_headers = frozenset({b"x-acme-signature"})
    manifest = PluginManifest(secret_fields=frozenset({"webhook_secret"}))
    fidelity = FidelityDeclaration(
        source_format="acme.call",
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

Import `PLUGIN_API_VERSION` from `obsalt.plugin`, not `obsalt._version`.

## Capabilities

| Capability | What you implement |
| --- | --- |
| `webhook_source` | authenticate, classify, delivery_key, tombstone_hints, acknowledgement, decode |
| `otlp_mapper` | `claims(span) -> int` (0 = not mine), `decode(spans)` |
| `rest_backfill` | `scan`, `hydrate` |
| `stream_source` | `frames` — declared so a WebSocket tap can land later; no first-party impl |
| `sdk_instrumentation` | wrap a vendor client |
| `authentication` | fail-closed verify with explicit outcomes |
| `judge` / `embedder` / `redactor` | analysis plugins |

`VerifyResult` outcomes: `ok`, `malformed`, `missing_credential`,
`bad_signature`, `stale`, `replayed`. Auth must not silently disable
itself.

Core primitives you should compose, not reimplement: `hmac_hex`,
`hmac_base64`, `parse_kv_header`, `ed25519_verify`, `enforce_window`,
`constant_time_eq`, JWT helpers.

## Decode rules

Emit small facts, not a whole-call blob. Core stamps org, call key,
`fact_id`, envelope id, `decoder_version`, `processing_run_id`.

- `fact_id` is deterministic for a source fact.
- A content hash identifies content. It does not say which content is
  newer.
- Same `fact_id`: identical content dedupes; a greater ordered
  `source_revision` wins; a disagreement without a comparable revision is
  a visible conflict.
- Snapshot decoders emit `SnapshotBoundaryObserved` and may retract
  omitted facts only for domains the provider documents as
  authoritative. Delta events never retract by omission.
- Populate grounding when the provider sends prompt, knowledge, tool
  results, or caller text. Hallucination detection reads those fields.
- Never emit `INTERVAL` without real start and end.
- Do not mark a signal structurally unsupported if your fixtures contain
  an unconsumed source field for it.

`FieldMap` covers the boring path lookups. Discrimination, units, and
tool pairing stay as code.

## Fixtures

```
fixtures/
  schema/            vendored OpenAPI / JSON Schema + PIN.json
  schema_overlays/   reviewed additive patches
  raw/               real-shaped payloads, one per event type
  expected/          NormalizedEvent[] each raw payload must produce
```

```bash
obsalt record-golden path/to/raw.json --provider acme
obsalt parse path/to/raw.json --provider acme
obsalt schema-drift --all
```

Review the golden diff. Subclass `obsalt-testkit` conformance classes.
Skipping a required test needs an explicit marker and a written reason.

Units that have already hurt people, so test them:

- Retell `words[].start/end` are **seconds**.
- Vapi turn-latency fields are milliseconds and **unplaced**. Published
  keys are `transcriberLatency` / `modelLatency` / `voiceLatency` /
  `turnLatency` / `endpointingLatency`.
- ElevenLabs post-call message anchors are whole seconds.

## First-party packages

| PyPI | Entry point | Kind |
| --- | --- | --- |
| `obsalt-vapi` | `vapi` | hosted webhook |
| `obsalt-retell` | `retell` | hosted webhook |
| `obsalt-elevenlabs` | `elevenlabs` | hosted webhook |
| `obsalt-cartesia` | `cartesia` | hosted webhook |
| `obsalt-pipecat` | `pipecat` | OTLP mapper |
| `obsalt-livekit` | `livekit` | OTLP mapper |
| `obsalt-openai-realtime` | `openai_realtime` | SDK + S2S mapper |
| `obsalt-gemini-live` | `gemini_live` | SDK + S2S mapper |

Auth schemes and the console-side fidelity table live in
[connect-hosted](connect-hosted.md) and [the console](console.md).
