# Testing

This page is the actual fix for invented fixtures and tautological
signature tests. Everything else is a consequence.

## Layout and markers

Tests live under `tests/` by kind. Directory is the marker; `conftest.py`
applies it automatically.

| Directory | Marker | Meaning |
| --- | --- | --- |
| `tests/unit/` | `unit` | Pure functions, no HTTP |
| `tests/integration/` | `integration` | Multi-component, memory doubles OK |
| `tests/blackbox/` | `blackbox` | HTTP status codes and JSON only |
| `tests/contract/` | `contract` | OTLP, SQL, SSRF, schema fixtures |
| `tests/conformance/` | `conformance` | Plugin testkit subclasses |
| `tests/property/` | `property` | Assembler fold properties |
| `tests/security/` | `security` | Auth, tenancy, SSRF |

```bash
make test-unit          # pytest -m unit
pytest -m "not property"
make ci
```

Use `tests.helpers` for fixture paths and signed headers. Use
`create_test_app()` or `api_client()`. Memory stores are doubles, not a
backend.

## Schema-validated fixtures

Every webhook plugin ships `fixtures/{schema,raw,expected,...}`. CI does
three things v0.1 never did:

1. **Validate every `raw/` fixture against the vendored provider schema.**
   When a captured deployed payload is ahead of vendor documentation, a
   narrowly additive overlay may admit it only with the original
   validation failure, redacted capture evidence, owner, review date, and
   expiry condition recorded. CI validates the base-plus-overlay and
   continues reporting divergence from the untouched vendor schema.
2. **Drift check.** A scheduled job may refetch the provider's published
   schema. Normal CI stays reproducible and offline
   (`obsalt schema-drift --all`). Updating the pin requires a reviewed
   schema, fixture, and expected-output diff.
3. **Enum coverage.** Diff the provider's published reason-code enum
   against our mapping table. Unmapped values fail the plugin's coverage
   threshold.

## Schema overlays

Overlays are the escape hatch that keeps captured reality representable
without making fiction cheap. They are reviewed, additive, and expiring.
They do not silence the vendor-schema mismatch; they preserve it.

## Conformance kit

`obsalt-testkit` ships inheritable base classes. Plugin authors subclass.
Skipping a required test needs an explicit marker plus a written reason.

**Required for every decoder / mapper:** idempotent decode; stable fact
ids; unknown event types tolerated without crashing; call identity
extraction; declaration matching possible decode output; grounding
populated where the provider supplies it; no PII in span names; explicit
unit tests (including a dedicated seconds-vs-milliseconds class); semantic
invariants for stage placement, tool pairing, and interruption signals.

**Required by capability:** authentication tests use captured header
shapes, negative cases, missing-credential fail-closed behavior,
duplicate / malformed headers, and replay windows. Webhooks test
delivery-key fallback and provider-specific success responses. Backfill
tests pagination, hydration, corrections, tombstones, and retention
truncation. Judges, embedders, redactors, stream sources, and SDK
instrumentation each have their own contract suite.

The critical rule: **the fidelity declaration and per-call coverage are
tested against decode output.**

```bash
obsalt record-golden packages/obsalt-vapi/src/obsalt_vapi/fixtures/raw/end_of_call.json \
  --provider vapi
```

Review the golden diff before committing.

## Other gates

- Property tests on the assembler: any permutation and duplicate delivery
  folds to the same candidate revision.
- Crash-point tests cover every receive step.
- Contract tests cover OTLP retry / partial-success semantics and real
  Pipecat / LiveKit output.
- Replay tests prove atomic revision promotion and correction-aware
  rollup serving generations.
- Cross-tenant read/write assertions, deletion-tombstone tests, SSRF
  tests, secret-redaction tests, and ClickHouse migration /
  physical-deletion tests.

## CI

Pull requests and pushes to `main` run one workflow: lint, typecheck, and
tests on Python 3.11 and 3.12. Feature-branch pushes without a pull
request do not run CI — open a PR. In-progress runs on the same PR cancel
when you push again. The required `CI` job gates on lint and tests.
Strict mypy still runs for visibility; it is informational until the
existing type backlog is cleared.

The scheduled schema-drift workflow is for refetch comparison. The
offline pin check also runs on every CI test job.
