# Conventions

**Who this is for:** anyone changing code — humans and coding agents.

**Question this page answers:** which names, rules, and test habits are
not optional?

These are the rules we write code against. Style that Ruff can enforce
lives in `pyproject.toml`. Everything here is the part a formatter
cannot see.

New contributors: [Developing](developing.md). AI agents:
[AGENTS.md](../AGENTS.md).

---

## Language and layout

- Python **3.11+**. CI runs 3.11 and 3.12. Typecheck is 3.12.
- `from __future__ import annotations` as the first statement in every
  module.
- Absolute imports. First-party packages are listed under
  `[tool.ruff.lint.isort].known-first-party`.
- Line length **100**. Ruff format + `E`/`F`/`I`/`B`/`UP`. Do not add a
  rule family until the tree is clean under it.
- Pydantic v2 models for domain objects. `StrEnum` for closed
  vocabularies. Frozen models only when identity must not change
  (`FidelityDeclaration`).
- One idea per module. If you need a comment to explain *what*, the
  name is wrong. Comments explain *why* — especially units, placement,
  and fail-closed auth.

```python
# Good: the unit is the whole story
# Retell words[].start/end are seconds; convert before StageObserved.

# Bad
# increment i
```

---

## Product vocabulary — do not rename for taste

These names *are* the domain. Tests, docs, and plugins speak them.

| Keep | Do not replace with |
| --- | --- |
| `CallRevision` | `Call`, `CallSnapshot`, `latest` |
| `NormalizedEvent` | `Event`, `Payload` |
| `StageMeasurement` / `AggregateMeasurement` | mixing them |
| `SignalCoverage` | `OptionalField` |
| `FidelityDeclaration` | `PluginCaps` |
| `RawEnvelope` | `Request`, `Webhook` |
| `TimelineFidelity` | `Quality`, `Level` |
| `MeasurementPlacement` | `Kind` |
| `PipelineArchitecture` | `Mode` |
| `Provenance` | `Source` (overloaded) |
| `org_id` | `tenant`, `workspace_id`, `account` |
| `ingest_key` | `webhook_token` in new APIs |

HTTP paths stay `/v1/ingest/{provider}/{ingest_key}`, `/v1/traces`,
`/v1/calls`. Plugin entry points stay `vapi`, `retell`, `elevenlabs`,
`cartesia`, `openai_realtime`, `gemini_live`, `pipecat`, `livekit`,
`example`. Environment variables stay `OBSALT_*`.

`VoiceCall.start` takes `org_id`. Plugins import `PLUGIN_API_VERSION`
from `obsalt.plugin`, not `obsalt._version`.

---

## The rules that show up in code

If a change violates one of these, it is the wrong change.

1. **Never draw what you did not measure.** `INTERVAL` requires real
   `started_at` and `ended_at`. Unplaced durations are chips. Provider
   p50/p95 are `AggregateMeasurement` and never enter sample
   percentiles.
2. **Provenance is a field.** `provider_reported` + `source_path`, or
   `obsalt_derived` + `derivation`. Absence is `SignalCoverage` with a
   reason — `unsupported` vs `absent` vs `decode_failed`.
3. **Raw first, then ack.** Object store + Postgres inbox/outbox commit
   before the provider-facing success response. Decode is a pure
   function of `RawEnvelope`. Decode does **not** run on the webhook
   request path (the TestClient drain-after-ack is a test convenience).
4. **Vendor schema, not our own reflection.** Fixtures validate against
   a vendored provider schema. No invented fields. Overlays are
   reviewed, additive, and expiring.
5. **One choke point.** Redaction happens once, on the normalized
   stream, before durable write of queryable data. Export policy is one
   place. Plugins cannot skip either.
6. **Revisions are immutable.** Reprocess → new `CallRevision` → CAS
   promote. Never mutate a stored call in place. Never `SELECT latest`.
7. **One storage shape.** Postgres + ClickHouse + object storage.
   Memory types are test doubles. No second backend. No SQLite.
8. **Providers are packages.** Core ships none. First-party plugins
   use the public `obsalt.plugins` contract.
9. **Expensive analysis is sampled and budget-capped.** Missing Tier-2
   output is not a pass (`sampled_out`, `budget_blocked`, `failed`).
10. **`org_id` comes only from authenticated credentials.** Payload
    fields and OTLP resource attributes may corroborate. They may never
    select. No `require_auth=false`.

---

## Errors and HTTP

- Fail closed. Empty credentials are `missing_credential`, not skip.
- Cross-org identifiers return **404**, not 403.
- Collection lists require a bounded `start` and `end`.
- Cursors are `{call_id}:{revision}`. Fleet responses carry one
  `as_of_generation`.
- Use `HTTPException` with a short `detail`. Do not leak other orgs,
  raw secrets, or stack traces to the client.
- OTLP 503 = retry the batch. OTLP partial success = do not retry.

---

## Plugins

- Implement the capability protocols in `obsalt.plugin.contract`.
  Structural typing is enough; you do not have to inherit the Protocol.
- `authenticate` must use constant-time compares and the primitives in
  `obsalt.crypto.primitives`. Do not roll HMAC.
- `classify` must reject synchronous application events
  (`assistant-request`, tool-calls, …).
- `decode` emits small facts. Core stamps `org_id`, `fact_id`,
  `decoder_version`, `processing_run_id`.
- Never emit `INTERVAL` without clocks. Never mark a signal
  `unsupported` if your own fixtures contain an unconsumed field for it.
- `decoder_version` is `"{name}/{n}"`.
- Tests: vendored schema + raw fixtures + golden `NormalizedEvent[]` +
  `obsalt-testkit` conformance classes. Skip a required test only with
  an explicit marker **and** a written reason.

---

## Tests

The directory **is** the marker (`tests/conftest.py`). Sizes follow
*How Google Tests Software*: many Small, fewer Medium, few Large.

| Directory | Size | Means |
| --- | --- | --- |
| `tests/unit/` | Small | One process. No HTTP. No I/O except committed fixtures. |
| `tests/integration/` | Medium | Several components. Memory doubles are fine. |
| `tests/blackbox/` | Large | Status codes and JSON only. No poking `app.state`. |
| `tests/contract/` | Medium | OTLP, SQL, SSRF, schema fixtures. |
| `tests/conformance/` | Medium | Plugin testkit subclasses. |
| `tests/property/` | Small | Assembler fold: permutation + duplicates. |
| `tests/security/` | Medium | Auth, tenancy, SSRF. |

- Use `tests.helpers` for fixture paths, signed headers, and oracles.
- Use `create_test_app()`. `create_app()` without a state must not
  become a memory backend.
- One behavior per test, named after the behavior:
  `test_retain_fails_when_durable_stack_is_down`.
- Independent oracles: expected values come from fixtures, vendor
  schemas, or the product promise — not from running the same function
  twice. Do not assert tautologies (HMAC yourself, then “verify” with
  the same function against the same bytes) unless you are testing the
  primitive in isolation.
- Do not duplicate the same assertion across layers. A Large test
  covers the HTTP contract; the Small test covers the algorithm.
- If a correct test fails, fix the product. Do not weaken the test.

---

## CLI and settings

- Production commands (`serve`, `worker`, `retain`, `export`) talk to
  the compose stack. On connection failure they exit **2** and tell the
  operator there is no SQLite mode. They must not fall back to memory.
- `--in-memory` is a test double and must print a warning.
- New `OBSALT_*` settings go on `Settings` **and** in `ENV_EXAMPLE`
  (the unit test will fail if you forget).
- `obsalt doctor` probes stores. Redis is optional.

---

## Docs

These pages follow the same contract as the product: never invent a
waterfall. One page, one job. Tables decide; prose explains the table.
Every how-to ends with what to do next. Informal language is fine.
Sloppy units are not.

If you change a product surface, a public contract, or how someone
connects an agent, update the matching page:

| Change | Page |
| --- | --- |
| What a PM should expect | [product.md](product.md) |
| What a source will show | [console.md](console.md) + connect guide |
| HTTP route or payload | [api.md](api.md) |
| `OBSALT_*` | [configuration.md](reference/configuration.md) + `ENV_EXAMPLE` |
| CLI flag | [cli.md](reference/cli.md) |
| How to run / recover | [getting-started.md](getting-started.md) / [troubleshooting.md](troubleshooting.md) |

Do not put version theater in titles (“v2 guide”). The product is
unreleased; packaging versions exist so extras resolve.

---

## Pull requests

- One concern.
- Tests for the behavior you changed.
- `make lint` and `make test-unit` locally. `make ci` before you
  consider it done.
- No drive-by refactors. No new storage backend. No invented provider
  fields.
