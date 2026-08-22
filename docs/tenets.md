# Design tenets

These are the load-bearing claims. Each one is a direct response to evidence
from the v0.1 prototype. If one of them is wrong, the architecture changes
shape. Attack these first; everything else is downstream.

The evidence that forced them is recorded in [decisions](decisions.md#why-v2-existed).

## T1. Never draw what you did not measure

A timeline is only rendered where real timestamps exist. Durations without
timestamps are stored and displayed as *measurements* — distributions,
percentiles, per-turn chips — never as span positions. Fidelity is derived
from the measurements present on each call, not merely declared once per
source.

A stage waterfall renders only `INTERVAL` measurements. At `TURN_LEVEL` the
UI shows turn bars with unplaced stage chips. `ANCHORED_DURATION` is visibly
distinguished from an interval. At `CALL_LEVEL` it shows distributions and
says why.

Provider-published percentiles are `AggregateMeasurement`s. They never enter
sample-derived percentile rollups.

## T2. Provenance is a first-class field, not metadata

Every value carries where it came from: `provider_reported` (with the source
path), or `obsalt_derived` (with the derivation). Absence is a separate
`SignalCoverage` fact with a reason. This is the only mechanism that
distinguishes "the provider did not send it" from "we failed to parse it."
It is the product's trust surface.

## T3. Raw first, durable, replayable

Every inbound payload is persisted verbatim before it is acknowledged, and
decode is a pure function from raw to normalized. Adapter bugs become
replays, not permanent data loss. Given the v0.1 adapter record, we assume
adapter bugs are the *steady state*, not an exception.

Replayability is bounded by configured raw retention and upstream provider
retention. The UI states that horizon explicitly.

## T4. Decoders are validated against published schemas, not against themselves

A provider fixture that does not validate against the vendor's published
OpenAPI / JSON Schema fails CI unless it uses the reviewed, additive,
expiring schema-overlay process. Captured payloads, golden normalized
outputs, unit assertions, and semantic invariants cover what schemas cannot
express — units, stage placement, tool pairing, interruption semantics.

## T5. One choke point per cross-cutting concern

All sources funnel through one normalization and assembly path. Queryable
normalized content crosses one core-owned redaction boundary before
persistence. Forwarded telemetry crosses one core-owned export-policy
boundary before egress. Plugins cannot bypass either boundary.

Redaction is synchronous, on the normalized stream, before durable write of
normalized data. Raw blobs stay unredacted and short-lived.

## T6. Decode, assemble, and analyze are separate stages with separate versions

Adapters emit small normalized events. Core owns assembly. Analysis runs
async on the assembled aggregate. Reprocessing builds a complete revision
side by side and atomically promotes it. This is what kills in-place
`merge_calls` and makes reprocessing tractable.

## T7. Pick the storage engine once, for the target scale, and do not abstract it

A pluggable-backend layer is the thing to avoid, not the thing to build.
Production is Postgres + ClickHouse + object storage. Memory types are test
doubles. See [storage](storage.md).

## T8. Providers are separately installable packages against a versioned contract

Core ships no providers. First-party providers use the same public plugin
API as third-party ones, so the API cannot rot.

## T9. Expensive analysis is sampled, cached, and budget-bounded

Deterministic analysis and search indexing scale linearly with eligible call
volume. At millions of calls, LLM-judging every call against every rubric is
financially impossible. Cheap deterministic analysis runs on 100%. Expensive
LLM analysis runs on a sampled or triggered subset, behind a hard per-org
monthly cap.

## T10. Tenant identity comes only from authenticated credentials

Payload fields, span attributes, trace resources, and object keys may
corroborate an authenticated organization. They may never select one.
`require_auth=false` does not exist. It silently collapsed every unknown key
into the first org.

## How the tenets show up in code

| Tenet | Code / product surface |
| --- | --- |
| T1 | `MeasurementPlacement.INTERVAL` is the only waterfall input. Timeline view-model tests refuse invented stage intervals. |
| T2 | Provenance and `source_path` travel with every value. Call-detail provenance panel. |
| T3 | Receive writes object storage + Postgres inbox/outbox before the provider ack. |
| T4 | `obsalt-testkit` schema-validated fixtures. `obsalt schema-drift`. |
| T5 | `obsalt.redact` on the normalized stream. `obsalt.otel.export_policy` on egress. |
| T6 | `decoder_version`, `assembler_version`, `analyzer_version` on outputs. Promotion protocol. |
| T7 | `production_state()` talks to compose. `create_test_app()` is explicit. |
| T8 | Entry-point group `obsalt.plugins`. Core registers none. |
| T9 | Tier-1 / index / Tier-2 lanes. `AnalysisState.SAMPLED_OUT` and `BUDGET_BLOCKED`. |
| T10 | `org_id` stamped from the authenticated connection. OTLP resource attributes cannot choose it. |
