# The console

This is the product. The HTTP API is the same data without the HTML.

If you are deciding whether obsalt is the right tool, read
[Product](product.md) first — then come back here for the fidelity table.

Open `/v1/ui` after a live call has been decoded. If the list is empty,
ingest has not promoted a revision yet — check `obsalt worker` and
`GET /ready`.

## What you are looking at

The header is the whole surface. There is no query builder and no custom
dashboard. Fleet-wide infrastructure correlation is a link out to your
OTLP backend.

| Page | What it answers |
| --- | --- |
| **Calls** | What happened, recently. Filter by agent, outcome, source, latency, flag, eval. |
| **Call detail** | Where time went **and** what was said. Timeline left, transcript right. |
| **Latency** | Stage distributions and percentiles, by agent, over time. |
| **Hangups** | Why calls ended, clustered, with drill-through to the ugly ones. |
| **Quality** | Rubric results and hallucination flags. Missing output is never a pass. |
| **Search** | "Customers asking about refunds." Semantic + filters. |
| **Settings** | Connections, rubrics, retention, plugins, keys, users. |

### Call detail — the join view

This is the page you open when a call went wrong.

- **Timeline.** Turn bars when we have turn clocks. A stage waterfall
  **only** from `INTERVAL` measurements (real start and end). Unplaced
  durations are chips. Provider p50/p95 are labelled aggregates, never
  mixed into sample percentiles.
- **Transcript.** Speaker + text from the assembled revision.
- **Tools.** Name, status, duration if the source measured one.
- **Flags / evals.** Deterministic flags on every call. LLM evals when
  you request them or a sample/budget allows. "Evaluate this call" is on
  the page.
- **Provenance / coverage.** The honest panel. For every signal:
  present, absent, unsupported, redacted, or decode failed — with a
  reason and a source path. This is how you tell "Vapi never sent VAD
  clocks" from "our decoder dropped a field."

Fidelity on the call is derived from what landed, not from a sticker on
the plugin:

| Fidelity | Meaning |
| --- | --- |
| `stage_level` | At least one real stage interval. Waterfall possible. |
| `turn_level` | Real turn boundaries. Stage values are chips. |
| `message_level` | Coarse message anchors (often whole seconds). |
| `call_level` | Aggregates only. |
| `none` | We have a call and almost no clocks. |

### Grafana vs this console

| Question | Where |
| --- | --- |
| Where did time go **and** what was said? | This console, call detail |
| Fleet waterfalls / P95 of **real** spans | Your Tempo / Grafana |
| Hangup clusters, evals, search, provenance | This console and `/v1` |

obsalt-derived metrics (`voice.call.duration`, `voice.stage.duration`, …)
export alongside forwarded OTLP. Provider aggregate latency is a labelled
gauge, not a span width.

## What each source will show you

This is the table people skip, then file a bug. Read your row.

| Source | How it arrives | Transcript | Hangup | Tools | Stage waterfall | Latency you *will* see |
| --- | --- | --- | --- | --- | --- | --- |
| **Vapi** | Webhook | Yes | Yes | Yes, no duration | **No** | Per-turn STT/LLM/TTS/e2e/endpointing as chips (ms, unplaced). Interruptions, confidence, cost, recording when sent. |
| **Retell** | Webhook | Yes, words in **seconds** | Yes | Yes, no duration | **No** | Call-level p50/p95 as aggregates. Word-ish turn intervals. Transport RTT when sent. |
| **ElevenLabs** | Webhook | Yes, whole-second anchors | Yes | Limited | Only if the OTLP-shaped webhook has real span clocks | Message-level timing from post-call JSON. |
| **Cartesia Line** | Webhook | Yes | Limited | Limited | **No** | Real turn intervals + unplaced STT/TTS TTFB chips. |
| **Pipecat** | OTLP | When you put text on `obsalt.pii.*` or the mapper finds it | If you emit outcome | Yes, if you emit `execute_tool` | **Yes**, where spans have real intervals | Native STT/LLM/TTS/TTFA from clocks you own. |
| **LiveKit** | OTLP | Same as above | Same | Yes | **Yes** | Real span intervals; audio-token attributes accepted in both key shapes. |
| **OpenAI Realtime** | OTLP | If you attach PII attrs | If you emit it | If you emit it | **Partial** — `user_input` / `generation` / `playout` only | No STT/LLM/TTS split. Empty cascade rows are a bug, not a feature. |
| **Gemini Live** | OTLP | Same | Same | Same | **Partial** — same S2S shape | Same as Realtime. |

Grounding (prompt, knowledge, tool results, caller text) is what
hallucination detection reads. Hosted plugins populate it when the
payload has it. Custom agents populate it when you emit it. If grounding
is empty, evals that need it fail closed — they do not silently pass.

## Provenance legend

| Status | Meaning |
| --- | --- |
| `present` | We have the value. `source_path` says where. |
| `absent` | This source *can* send it; this call did not. |
| `unsupported` | This source structurally cannot send it. |
| `redacted` | It arrived and the redaction choke point stripped it. |
| `decode_failed` | The payload had something we could not parse. Replay after a plugin fix. |

`provider_reported` vs `obsalt_derived` travels with every number. Derived
values carry the derivation (for example a turn gap). We do not hide that.

## Settings you will actually use

- **Connections** — the ingest keys you already created. Secrets never
  come back out.
- **Rubrics** — plain-English evals. Editing creates a new version;
  historical results keep the version they were judged under.
- **Retention** — 30 days raw, 90 days transcripts, 400 days aggregates
  unless you change it. Raw is unredacted. Replay dies when raw expires.
- **Keys / users** — scopes `ingest`, `read`, `analyze`, `admin`. Roles
  `owner`, `admin`, `analyst`, `reviewer`.

## If the call is "wrong"

1. Provenance first. Unsupported vs decode_failed is the whole game.
2. `GET /ready` — inbox age, outbox depth, DLQ, orphan blobs.
3. `GET /v1/plugins` — is the plugin even loaded?
4. `obsalt parse payload.json --provider vapi` — decode without ingesting.
5. `POST /v1/replay` if the raw blob is still in the horizon.
