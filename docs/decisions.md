# Decisions

Architecture Decision Records for obsalt v2. Each record states what was
chosen, what was rejected, and why. The tenets in [tenets.md](tenets.md) are
the claims these decisions rest on.

## Why v2 existed

Three independent investigations of the unreleased v0.1 prototype converged on
one root cause: adapters were written against invented payloads, webhook
security was non-functional for Retell and Bland, and retroactive span
synthesis cannot be fixed because it is a category error. This section is
historical. It is kept so the same mistakes are not reintroduced.

### Adapters were written against invented payloads

Provider adapters were validated against hand-written fixtures that were
reverse-engineered from the adapters themselves. All 62 tests passed. On real
payloads:

| Provider | Real state |
| --- | --- |
| **Vapi** | The schema-conformant latency path was dead. It read `turnLatencies[].stt/llm/tts/e2e`; the published keys are `transcriberLatency` / `modelLatency` / `voiceLatency` / `turnLatency`. It read `artifact.messages[].metadata.llmLatency`, although published `BotMessage` does not define that metadata. Fixtures contained the invented fields, so tests passed while real provider stage measurements were discarded. |
| **Retell** | Worse, because it was silent. `words[].start/end` are **seconds**, read as milliseconds. Correct provider values (580ms, 900ms) were averaged with 1000×-low derived values (0.2ms, 0.6ms), yielding a P50 of 290ms where the truth is ~740ms. Direct tool duration is unavailable. |
| **Bland** | The most faithful adapter in v0.1. The log-line regex initially assumed fabricated is documented. Its one real defect: the `category == "tool"` branch was unreachable because Bland sends `category: "call"`. |
| **OpenAI Realtime** | Barge-in detection was a 100% false positive — every agent turn followed by any user speech was flagged as an interruption. One measured interval was written into three different fields and then rendered as three sequential spans. |

### Webhook security was non-functional

Retell and Bland implemented the wrong algorithms. The Retell test verified
our formula against itself (a tautology). Real Retell sends
`X-Retell-Signature: v={ms},d={hex}` where the digest is
`HMAC-SHA256(raw_body + timestamp)` keyed by the **API key**. Real Bland sends
`X-Webhook-Signature` as an HMAC digest, not a plaintext secret.

Leave any provider secret blank and `verify_*` returned `True`
unconditionally. Fail-open is deleted. Authentication is fail-closed.

### Retroactive span synthesis cannot be fixed

Not a bug — a category error. Child spans were anchored at a containing turn
boundary and laid out by duration arithmetic. User STT and agent LLM/TTS
stages were independently anchored to their respective turn starts, not to
measured stage timestamps. Even with the field names fixed, Vapi stage values
have no stage timestamps or turn association, and Retell publishes call-level
latency distributions plus approximate word intervals. There is nothing to
place those stage values at. A waterfall drawn from summary statistics is not
observability.

### Coverage reported the wrong absence

`assess_coverage` reported VAD / endpointing timing and STT confidence as
structural gaps — "hosted platforms do not send this." Vapi sends
`endpointingLatency` per turn, `numAssistantInterrupted`,
`numUserInterrupted`, and word-level confidence, in the payload we already
received and threw away. Retell publishes `llm_websocket_network_rtt`. Bland
can publish `corrected_transcript[]` with real second-precision timings when
delayed post-call enrichment is enabled.

Our own honesty mechanism could not distinguish "the provider did not send
it" from "we failed to parse it," so it confidently reported the wrong one.
That finding determines the architecture: provenance and coverage are
first-class, and decode is a replayable pure function.

### The rest was prototype scaffolding

In-memory store (restart loses everything, so no adapter bug is ever
recoverable). `merge_calls` mutating stored objects in place. All analysis
running synchronously inside the webhook request. One global secret per
provider across all tenants. `require_auth=false` collapsing every unknown
key into the first org. `_call_summary` rebuilding a full span tree per call
per list request. Hangup clustering re-embedding every call in the org on
every request.

### Half the premise was inverted

The dated provider survey found no surveyed hosted voice platform that pushes
standard OTLP to an arbitrary collector. ElevenLabs comes closest: it delivers
OTLP-*shaped* JSON over a signed webhook that users must still receive.

So the webhook receiver is not the redundant part. For the committed hosted
providers it is the ingest path that exists. What was redundant is the span
synthesis on top of it.

## Evidence standard

Provider documentation is necessary but not sufficient. Published schemas
often omit units, leave metadata untyped, or lag deployed payloads. Every
provider claim in plugin code must identify:

1. the pinned schema or documentation revision
2. a captured, redacted payload where legally available
3. semantic assertions for units and cross-field meaning
4. the date the claim was last verified

Schema validation catches structural drift. It does not prove unit
correctness, stage placement, tool pairing, or interruption semantics.

## ADR-0001 — Committed three-store architecture

**Status:** accepted

**Decision:** Postgres + ClickHouse + object storage. No pluggable backend.
No SQLite / Postgres-only production mode.

**Rejected:** Postgres-only with Parquet export as the analytics path;
a second in-process storage implementation for `pip install && serve`.

**Why:** See [storage](storage.md). Overturning this means accepting a future
migration. It is the single biggest deviation from the original "Postgres
only" instruction and the thing to overturn first if you disagree.

## ADR-0002 — Never draw unmeasured intervals

**Status:** accepted

**Decision:** Separate `StageMeasurement` from timeline placement. Waterfalls
render only `INTERVAL`. Unplaced durations are chips. Provider percentiles
never enter sample rollups.

**Rejected:** Reconstructing a waterfall from summary statistics (v0.1 span
synthesis).

**Why:** T1. Hosted platforms ship durations without timestamps. Inventing
positions caused wrong conclusions and cannot be fixed at the field-name
layer.

## ADR-0003 — Raw first, decode later

**Status:** accepted

**Decision:** Persist the raw envelope to object storage and commit a
Postgres inbox/outbox row before the provider-specific success response.
Decode is a pure function in a worker.

**Rejected:** Decode on the request path; in-memory-only ingest.

**Why:** T3. Adapter bugs are the steady state. Replay is bounded by the
displayed raw retention horizon.

## ADR-0004 — Providers are plugins, not core

**Status:** accepted

**Decision:** Core ships no providers. First-party plugins register on
`obsalt.plugins` — the same group as third-party ones. Convenience extras
(`obsalt[vapi,retell]`) and `obsalt-providers-all` are install-time only.

**Rejected:** Vendoring adapters into core; a privileged first-party load
path.

**Why:** T8. A privileged path lets the public API rot.

## ADR-0005 — Hand-written Python decoders, not a DSL

**Status:** accepted

**Decision:** Decoders are Python, with a small `FieldMap` helper for the
boring majority.

**Rejected:** A YAML / JSON mapping DSL as the primary adapter mechanism.

**Why:** dbt shipped `pytest-dbt-adapter` and reversed out of it to
inheritable Python classes. Airbyte's low-code CDK retains a Python escape
hatch that gets heavy use. Here the decision is forced anyway: Vapi's
signature algorithm and header names are operator-configured at runtime.
Event discrimination, turn reconstruction, unit normalization, and tool
pairing are real logic.

## ADR-0006 — Schema-validated fixtures as a CI gate

**Status:** accepted

**Decision:** Every webhook plugin vendors the provider schema, captured
payloads, and golden `NormalizedEvent[]`. CI validates raw fixtures against
the vendor schema. Additive overlays are reviewed, expire, and still report
divergence from the untouched vendor schema. A scheduled job refetches
published schemas. Enum coverage fails below a configured threshold.

**Rejected:** Fixtures reverse-engineered from the adapter (v0.1).

**Why:** T4. That is the actual fix for invented payloads.

## ADR-0007 — Low-cardinality OTLP span names

**Status:** accepted

**Decision:** Span names are `turn`, `stt.provider_attempt`, `execute_tool`.
Cardinality moves to attributes (`turn.index`, `stt.provider`,
`gen_ai.tool.name`).

**Rejected:** The Hamming guide's `turn.{index}`, `stt.provider.{name}`,
`llm.tool_call.{name}` as span *names*.

**Why:** Backends group by name and derive span metrics from it. A 200-turn
call minting 200 unique span names degrades Tempo / Datadog and makes "P95
across turns" unaskable. Hamming can afford this inside their own backend. A
tool whose output lands in *your* backend cannot.

## ADR-0008 — Marked PII namespace, not a denylist

**Status:** accepted

**Decision:** Conversational content lives under `obsalt.pii.*`. Default
exporter config strips that prefix. The denylist is retained as a
belt-and-braces assertion in tests, not the mechanism.

**Rejected:** v0.1's denylist of ~13 literal attribute keys.

**Why:** A denylist fails open on anything unanticipated. LiveKit's approach
fails safe: a collector can strip by prefix. Content never appears in a span
name, because names are not redactable.

## ADR-0009 — Trusted operator-installed plugins

**Status:** accepted

**Decision:** Plugins are pinned, inventoried, vulnerability-scanned,
operator-installed code. Loading isolates version mismatches and ordinary
exceptions. That is not a security sandbox. An arbitrary wheel can still
read process memory, block, or exit.

**Rejected:** Tenant-installable plugins in v2.

**Why:** Tenant code requires a future out-of-process runtime with IPC
validation, resource limits, network policy, and per-job credentials. The
product does not claim a sandbox it does not have.

## ADR-0010 — Langfuse-compatible ingest is deferred

**Status:** deferred, not rejected

**Decision:** Do not expose a Langfuse-shaped ingest endpoint in v2.

**Why:** Vapi natively pushes traces to Langfuse via
`assistant.observabilityPlan`. Exposing a compatible endpoint would let a
Vapi user point at obsalt with zero webhook configuration. It is genuinely
tempting. It is also a compatibility surface for a proprietary,
undocumented, moving API. Breadth of first-class provider support wins.
Revisit if Vapi's webhook path proves insufficient.

## ADR-0011 — Analysis is two tiers plus an index

**Status:** accepted

**Decision:** Tier 1 on every call. Index every eligible call. Tier 2
sampled or triggered, default baseline 0%, hard monthly spend cap.

**Rejected:** LLM-judge every call; sample the search index.

**Why:** T9. At 1M calls/month and six rubrics, judging every call is ~6M
LLM calls/month. Sampling search would make it silently incomplete.

## ADR-0012 — Hybrid search in Postgres, not a second vector store

**Status:** accepted

**Decision:** One Postgres `search_documents` projection with `tsvector` and
pgvector HNSW. Reciprocal-rank fusion. Call bodies hydrate from ClickHouse.

**Rejected:** A separate vector database; MD5 hashing as "semantic search"
(v0.1).

**Why:** Lexical and vector candidates must share tenant and structured
filters. A second store would split authorization and deletion.

## ADR-0013 — Outbound webhooks follow Standard Webhooks

**Status:** accepted

**Decision:** `call.finalized`, `eval.failed`, `flag.raised`, `slo.breached`
use the [Standard Webhooks](https://www.standardwebhooks.com/) symmetric
profile. Destination validation is HTTPS-only and blocks private /
link-local / loopback / metadata targets.

**Why:** Signing is only one part of delivery. Events use an org-scoped
transactional outbox, stable id, schema version, call revision, and a DLQ.

## ADR-0014 — Finite retention, deletion cannot be undone

**Status:** accepted

**Decision:** Defaults of 30 / 90 / 400 days. Durable tombstones are checked
by receive, replay, backfill, indexing, analysis, and export. Deletion is
verified across every managed store. External warehouse copies cannot be
revoked and are disclosed.

**Why:** "Indefinite by default" is how self-hosted tools accumulate
liability. A deletion job that was never exercised is a deletion job that
does not work.

## What was carried forward from v0.1

The voice attribute vocabulary. The coverage / provenance *idea* (not the
implementation). The hangup taxonomy. The evidence-vs-span split.

## Open questions

These were genuinely open when v2 was specified. Defaults below are what
the tree implements; overturn them with a new ADR.

1. **Bland** was dropped from the committed provider list. It was the most
   faithful adapter in v0.1 and can publish `corrected_transcript[]` with
   second-precision timings. Keep as a community plugin, or restore it after
   the committed set?
2. **Cartesia Line's priority.** It has real turn intervals but only
   unplaced stage TTFBs, so it does not prove a stage waterfall. It is in
   the committed hosted set as a `turn_level` source.
3. **Default embedding model.** Local ONNX is the default (air-gapped,
   free, weaker). Users can upgrade to a hosted embedder.
4. **Tier-2 default sample rate.** 0% baseline, trigger-based analysis on,
   so the first bill is predictable. Prompt the operator to opt into a
   sample rate.
5. **UI stack.** Server-rendered templates with progressive enhancement
   shipped for the functional milestone. A small SPA remains a later option
   for the timeline and review queue.
6. **Upstream OTel voice conventions.** OTel's voice-convention work (PRs
   #390, #393, #394) has open questions that are literally this product's
   architecture. Commenting is cheap and would shape the spec toward our
   design rather than away from it. `obsalt.*` remains authoritative
   internally; proposed `gen_ai.*` attributes sit behind a version flag.

## Risks still in force

| Risk | Mitigation |
| --- | --- |
| Provider schemas omit units or drift | Vendored schemas + captured payloads + semantic assertions + scheduled drift and enum checks |
| Receive spans object storage, Postgres, and Redis | Deterministic blob keys + transactional inbox/dedupe/outbox + Redis as a lease accelerator + crash-point tests |
| Replay or late data double-counts facts | Stable fact ids, complete call revisions, promotion protocol, revision-keyed consumers |
| ClickHouse operational burden | Full-stack demo, tuned defaults, immutable facts, Postgres active-call index, rollup serving generations |
| Tier-2 LLM cost surprises a user | Hard per-org cap, 0% default baseline sampling, content-hash caching |
| OTel voice conventions land differently | Version-flagged attributes; `obsalt.*` internally; mapper registry absorbs renames |
| Plugin API ossifies wrong, or plugins are over-trusted | `API_VERSION`, first-party public path, capability testkit, trusted operator-installed model stated explicitly |
| Privacy controls lag ingestion | Encryption, raw TTL, header allowlist, tombstones, delete-by-call, and audit are ingest-path gates |
