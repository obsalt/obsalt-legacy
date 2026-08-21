# What you can see, and from where

obsalt does **not** replace Grafana. Fleet waterfalls and SLOs stay in Tempo / Prometheus. What was missing — and what `/v1/ui` fills — is **one join surface** so a human can see the span tree, transcript, recording, tools, and evals for a single call without copying `call.id` between tabs.

Transcripts, prompts, and tool payloads still do **not** go on OpenTelemetry spans. The join view reads evidence from the store and rebuilds the same conversation tree the tracer exports.

```
You / on-call / product
        │
        ├─ obsalt /v1/ui                 one call: waterfall + transcript + coverage
        ├─ Grafana Explore → Tempo       fleet waterfalls, “where did 1.8s go?”
        ├─ Grafana Explore → Prometheus  P95 STT / LLM / TTS, error counts
        ├─ curl / scripts / your app  → obsalt HTTP API
        │                                 transcripts, hangups, evals, search, /view JSON
        └─ (optional) Loki               turn envelopes you emit yourself
```

```mermaid
flowchart LR
  subgraph face ["What a human opens"]
    UI["/v1/ui  one call"]
    G["Grafana :3000  fleet"]
    API["GET /v1/calls"]
  end
  subgraph backends ["Backends you run"]
    Tempo["Tempo / Jaeger / Honeycomb"]
    Prom["Prometheus / Mimir"]
    Store["obsalt evidence store"]
  end
  UI --> Store
  G --> Tempo
  G --> Prom
  API --> Store
  Tempo -.->|"join key call.id"| UI
```

## Surfaces

| Question | Where you look | Produced by | Path |
| --- | --- | --- | --- |
| This call, end to end | `/v1/calls/{call.id}/ui` | Join view: reconstructed span tree + evidence | Both |
| Coverage gaps (no recording, no STT fallback, live vs reconstructed) | Same page, coverage chips | `coverage` on `/view` and list summaries | Both |
| Where did time go on this turn? (fleet search) | Tempo waterfall (`call.lifecycle` → `turn.N` → STT/LLM/TTS) | **A:** server reconstructs after the webhook. **B:** `VoiceCall` exports live over OTLP | Both |
| Did Deepgram time out and Azure save the turn? | Join view or Tempo, `stt.provider.*` children | Path B records attempts on the snapshot. Path A will **not** invent them | B (A: structural gap) |
| What did the agent actually say? | Join view transcript pane, or `GET /v1/calls/{call.id}` | `obsalt serve` evidence store | A always; B if you POST a snapshot |
| Invented order number / price? | `hallucinations[]` on that call / evals pane | Server analysis at finalize | A always; B with snapshot |
| Are refunds hanging up angry? | `GET /v1/hangups` | Server | A / B+snapshot |
| P95 time-to-first-audio | Prometheus `voice_response_latency_seconds{stage="ttfa"}` or `GET /v1/latency` | Server at finalize (not the live tracer) | A / B+snapshot |
| Recording | Audio player on `/ui` when `recording_url` is set | Copied from the vendor payload or your snapshot | A if the vendor sent it; B if you set it |

Join key: **`call.id`** on every span = the obsalt uuid returned by ingest = `GET /v1/calls/{that id}` = `/v1/calls/{that id}/ui`.

Your room / SIP / Vapi id is **`call.provider_id`** on spans and `provider_call_id` on the JSON. Lookup: `GET /v1/calls?provider_call_id=room-42&provider=native`.

## What is *not* a screen in obsalt

| Thing | What it actually is |
| --- | --- |
| VoiceCallTracer | A **Python class** in your process. It creates spans. It is not a server and has no port. |
| VoiceCall | A **Python class**. Spans + an evidence snapshot. Still not a server. |
| OTLP | A **wire protocol** (HTTP, usually port **4318**). Tempo/Jaeger/Honeycomb speak it. obsalt does not host it. |
| `obsalt serve` | An **HTTP server** (`:8080` by default). Ingest + evidence + the per-call join view. Not Grafana. |
| CallRecorder snapshot | A **JSON document**. The obsalt-shaped equivalent of a Vapi `end-of-call-report`. |
| `/v1/ui` | A **per-call correlation page**. It is not a product analytics suite and does not replace Tempo. |

## Path A — what appears after a Vapi call

1. Vapi POSTs `end-of-call-report` to `obsalt serve`.
2. You can immediately open `/v1/ui` or `GET /v1/calls` — transcript, tools, hangup, hallucinations, evals, plus a reconstructed waterfall.
3. Coverage will mark `stt_fallback` and `live_spans` as **structural** gaps: the vendor JSON has no Deepgram→Azure hop, and there is no in-call OTLP.
4. If `OBSALT_OTLP_ENDPOINT` is set, the server **also** rebuilds a `call.lifecycle` tree and POSTs it to Tempo. The waterfall shows the call’s real timestamps, not “just now”.
5. Prometheus histograms update from the same finalize step.

You never see a live waterfall *during* the Vapi call. The vendor does not stream OTel to you.

## Path B — what appears during a Pipecat call

1. `setup_tracing(otlp_endpoint="http://localhost:4318")` in the **agent** process. Spans leave that process over OTLP while the user is talking.
2. Grafana Tempo: live `call.lifecycle`. No transcript text on spans (by design).
3. `/v1/calls` stays empty until the snapshot is POSTed (`VoiceCall(..., client=...)` does this on exit, or `ObsaltObserver.close()`).
4. After ingest: `/v1/ui` shows the same tree next to the transcript. STT `provider_attempt` hops are stored on the snapshot, so the join view can show Deepgram timeout → Azure fallback even if you open the page after the fact.
5. Eval spans are attached to the **same** live trace (via `traceparent`). The server does **not** emit a second `call.lifecycle`.

## Grafana, Tempo, Prometheus

Local all-in-one:

```bash
docker run --rm -p 3000:3000 -p 4317:4317 -p 4318:4318 grafana/otel-lgtm
```

| Port | Role |
| --- | --- |
| 3000 | Grafana UI (admin / admin on a fresh LGTM) |
| 4318 | OTLP HTTP — `OBSALT_OTLP_ENDPOINT` and `setup_tracing(otlp_endpoint=...)` |
| 4317 | OTLP gRPC (optional) |
| 8080 | obsalt — ingest, evidence, `/v1/ui` |

Filter Tempo on `call.id`, `call.provider_id`, `workspace.id`, `agent.id`, `turn.index`. Dashboards: [Metrics and Grafana](grafana.md).

From a Tempo span, open evidence with `/v1/calls/{call.id}/ui`. The span will not contain the transcript.

## Evidence HTTP API (no fleet UI)

```bash
curl -H "X-API-Key: secret" http://localhost:8080/v1/ui
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls/$CALL_ID/view
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls/$CALL_ID
curl -H "X-API-Key: secret" "http://localhost:8080/v1/search?q=refund"
curl -H "X-API-Key: secret" http://localhost:8080/v1/hangups
curl -H "X-API-Key: secret" http://localhost:8080/v1/latency
```

Interactive OpenAPI: `http://localhost:8080/docs`. Full reference: [HTTP API](api.md).

v0.1 store is **in-memory**. A restart loses calls. Traces in Tempo are independent — they survive an obsalt restart if Tempo persisted them. The join view can only show evidence that is still in the store.
