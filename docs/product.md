# What obsalt does

obsalt is a **self-hosted call record and quality system for live AI
voice agents**. You run it next to the stack you already have. Live
calls flow in. You open a console when a call went wrong, or when you
want to know which agent is losing customers.

New to voice agents? Read [Voice agents](concepts.md) first.

Ready to install? [Getting started](getting-started.md).

---

## What it is

Three pieces ship together:

| Piece | Role |
| --- | --- |
| **Service** | HTTP API + worker. Receives calls, stores them, scores them. |
| **Console** | Seven screens at `/v1/ui`. The call-detail join view is the product. |
| **`VoiceCall`** | A thin OpenTelemetry tracer. Import it only if **you** own the agent process. |

Core ships **no** providers. `obsalt-vapi`, `obsalt-pipecat`, and the
other source packages are ordinary Python extras.

It is **not**:

| Not this | Why |
| --- | --- |
| A hosted SaaS you log into | You keep the transcripts, recordings, and evals. |
| An agent builder or prompt manager | You already have Vapi, Retell, Pipecat, LiveKit, or similar. |
| A replacement for Grafana / Tempo | Traces still go to your OpenTelemetry backend. obsalt owns the **call**. |
| A testing / simulation platform | Hamming, Coval, and similar tools generate callers. obsalt observes **production**. |
| A dashboard builder | Seven screens. No query builder. No custom charts. |

---

## The six capabilities

Everything in obsalt exists to answer these questions.

| Capability | Question |
| --- | --- |
| **Latency** | Where did time go on this call — and is this agent slower than last week? |
| **Hangups** | Why did we lose this caller? |
| **Hallucination** | Did the agent invent a price, an order id, or a completed tool? |
| **Tools** | Which function calls fail, retry, or stall the turn? |
| **Evals** | Did this call meet *our* bar, in plain English? |
| **Search** | Show me calls where customers asked about refunds. |

### 1. Latency breakdown

**What you get**

- Per-call timing that matches what the source actually measured.
- Fleet percentiles (P50 / P95) of **real samples**, by agent, over time.
- A stage waterfall **only** when we have real start and end timestamps.

**What you will not get**

- A waterfall invented from Vapi or Retell summary statistics. Those
  platforms send durations without stage clocks. The console shows them
  as **chips** and says so.

Voice engineers use this when the bot feels slow. Product managers use
it after a model swap.

### 2. Hangup analyzer

**What you get**

- Every call classified into a stable hangup list (`user_hangup`,
  `silence_timeout`, `error_llm`, `voicemail`, and the rest).
- Clusters with drill-through to the ugly calls — last speaker, last
  user text, last agent text, a loss score.
- The original provider code, so you can audit the mapping.

### 3. Hallucination detection

**What you get**

- Deterministic flags on **every** call: ungrounded prices, fabricated
  ids, commitments, phantom tool success.
- Optional LLM entailment (Tier 2) against prompt, knowledge, tool
  results, and what the caller said — only if you set a budget and a
  judge.
- Fail-closed evals. Missing judge output is **never** a pass.

**What you need:** grounding. Hosted plugins populate it when the
payload has it. Custom agents populate it when you emit it. Empty
grounding means flags that need it do not silently succeed.

### 4. Function-call telemetry

**What you get**

- Every tool invocation: name, status, retries, payload shape, duration
  when the source measured one.
- Fleet tool success / error / timeout rollups.

Vapi and Retell often report that a tool ran **without a duration**.
The console shows the invocation and says duration was not reported.

### 5. Custom evals

**What you get**

- Rubrics you write in English. Editing a rubric creates a **new
  version**; historical scores stay attached to the version they were
  judged under.
- On-demand “evaluate this call” in the console, plus an optional
  fleet sample behind a hard monthly USD cap.
- A review queue: humans can agree or disagree with the judge.

**What you need:** `OBSALT_JUDGE_*` pointed at an OpenAI-compatible
endpoint, and `OBSALT_LLM_MONTHLY_BUDGET_USD` > 0 if you want paid
spend. Default budget is **$0**.

### 6. Semantic search

**What you get**

- Hybrid search (lexical + vector) over **redacted** content.
- Filters: agent, source, hangup reason, time range (required).

Default embedder is a local ONNX model. You do not have to send
transcripts to a third-party embedding API.

---

## Who uses it

| You are… | You will… |
| --- | --- |
| A team on **Vapi / Retell / ElevenLabs / Cartesia** | Point their webhook at obsalt. Own a call console those dashboards do not give you. |
| A team building on **Pipecat / LiveKit / OpenAI Realtime / Gemini Live** | Emit OTLP. Keep Tempo. Get a voice-aware join view on top. |
| A **product manager** | Read hangup clusters, eval fail rates, and search — without SQL. |
| A **voice / platform engineer** | Debug one call with transcript, timing, and provenance on one page. |
| A **quality / compliance lead** | Rubrics, hallucination flags, and deletion that completes. |

Multi-workspace is a tenancy constraint: credentials and data are
per-`org_id` from day one.

---

## A first week

1. **Install.** Clone, `docker compose up`, sign in with `dev-key`.
   Empty console. Confirm `/ready`. See
   [Getting started](getting-started.md).
2. **One live agent.** Connect [hosted](connect-hosted.md) or
   [your own](connect-custom.md). Place three real calls. Open them.
3. **Read provenance.** If Vapi latency is a chip, that is correct.
   Brief the team with [the console](console.md) table so nobody files
   “waterfall missing.”
4. **One rubric.** Write the quality question you already ask in QA.
   Leave the LLM budget at $0 until you are ready; deterministic flags
   still run.
5. **A second agent.** Compare hangup clusters.
6. **Before production.** Rotate the bootstrap key, set real
   `OBSALT_MASTER_KEY` / `OBSALT_SESSION_SECRET`, read
   [Operate](ops.md) and [Security](reference/security.md).

Do not mix a hosted webhook and OTLP on the **same** call. You get two
partial records and no join.

---

## How this compares

| Need | Provider dashboard | Grafana / Tempo | Hamming / Coval | **obsalt** |
| --- | --- | --- | --- | --- |
| Transcript + timing on one page | Partial | No (traces are not a transcript) | Lab only | **Yes** |
| Honest “we don’t have stage clocks” | Rarely | N/A | N/A | **Yes** |
| Hangup clusters you own | Vendor-specific | DIY | No | **Yes** |
| Hallucination vs *your* grounding | Limited | No | Sometimes, in test | **Yes**, production |
| Plain-English rubrics | Rarely | No | Yes, synthetic | **Yes**, live |
| “Calls about refunds” | Keyword-ish | No | Corpus you generated | **Yes** |
| Pre-launch load / personas | No | No | **Yes** | No |
| Infra correlation (CPU, k8s) | No | **Yes** | No | Link out |

Use a simulation platform to break the agent before launch. Use Grafana
for fleet waterfalls of **real** spans you emitted. Use obsalt for the
production call record.

---

## What each source can deliver

Full notes: [The console](console.md).

| Source | Path | Stage waterfall | Latency you will see |
| --- | --- | --- | --- |
| Vapi | Webhook | **No** | Per-turn STT/LLM/TTS/e2e chips (ms, no clocks) |
| Retell | Webhook | **No** | Call-level p50/p95 as aggregates; words in **seconds** |
| ElevenLabs | Webhook | Only if their OTLP-shaped hook has clocks | Whole-second message anchors |
| Cartesia Line | Webhook | **No** | Turn intervals + unplaced TTFB chips |
| Pipecat / LiveKit | OTLP | **Yes**, where *you* emit intervals | Native stages from your clocks |
| OpenAI Realtime / Gemini Live | OTLP | **Partial** — `user_input` / `generation` / `playout` | No fake STT/LLM/TTS split |

---

## Privacy, tenancy, and cost

- **Tenant boundary is `org_id`.** It comes from the API key or the
  ingest URL, never from a field the provider sent.
- **Raw webhooks are stored unredacted** for about 30 days so a decoder
  bug is a replay, not a hole. Queryable transcripts are redacted
  first.
- **Deletion completes.** A request that never sets `completed_at` is
  not a deletion. External warehouse copies cannot be revoked; the API
  says so.
- **LLM spend is capped.** Default monthly budget is $0. Deterministic
  analysis always runs.
- **No “auth off.”** Local bootstrap is `dev-key` bound to org `local`.

---

## Current limits

- No first-party Bland or Deepgram plugin (Deepgram is designed for as
  a stream source).
- No OIDC / SSO login. Bootstrap key + hashed service keys + session
  cookies.
- OTLP gRPC is opt-in (`obsalt[grpc]`). HTTP `/v1/traces` is the path.
- Parquet export exists; warehouse-native sync does not.
- The UI is functional. The join view is the product.

---

## Next

| I am… | Next |
| --- | --- |
| Learning the domain | [Voice agents](concepts.md) |
| Checking what a source will show | [The console](console.md) |
| Installing | [Getting started](getting-started.md) |
| Connecting Vapi / Retell / … | [Connect a hosted platform](connect-hosted.md) |
| Connecting Pipecat / LiveKit / … | [Connect your own agent](connect-custom.md) |
| Operating | [Operate](ops.md) |
| Changing the code | [Developing](developing.md) |
