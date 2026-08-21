# Glossary

Short definitions for the names that look like they might be servers, formats, or products.

## OTLP

**OpenTelemetry Protocol.** A wire format (HTTP/protobuf) that collectors speak, usually at `http://localhost:4318`.

- obsalt does **not** run an OTLP server.
- Grafana Tempo, Jaeger, Honeycomb, and `grafana/otel-lgtm` accept OTLP.
- obsalt **exports** spans and metrics *to* that endpoint (`/v1/traces`, `/v1/metrics`).

Think: USB, not a hard drive.

## VoiceCallTracer

A **Python class** (`from obsalt import VoiceCallTracer`). Library code you call inside the agent process. It opens a `call.lifecycle` span and child spans for turns / STT / LLM / TTS.

It is not a daemon, not a port, not Grafana. It only emits telemetry. Prefer [`VoiceCall`](custom-agents.md) unless you want spans with no evidence.

## VoiceCall

A **Python class** that does both jobs for custom agents: live OTLP spans *and* an evidence snapshot. If you pass `client=ObsaltClient(...)`, the snapshot is POSTed when the `with` block exits.

This is the production API for Pipecat / LiveKit / your loop.

## CallRecorder

A **Python class** that builds a native snapshot **without** emitting spans. Use it when you do not want OpenTelemetry in the agent process. `VoiceCall` uses a `CallRecorder` internally.

## Native snapshot / CallRecorder snapshot

A **JSON object** POSTed to `POST /v1/ingest/native`. obsalt’s own envelope: turns, tools, timings, transcript, hangup.

It is the same *role* as a Vapi `end-of-call-report` — a complete picture of one call — but it is obsalt’s schema, not Vapi’s.

Typed in code as `NativeSnapshot`. Full example: [Native snapshots](providers/native.md).

## Why hosted providers do not send a snapshot

Vapi, Retell, and Bland already have post-call JSON. Their adapters map **that** JSON onto `CanonicalCall`. Asking them to emit obsalt’s snapshot would mean they adopt our schema. They will not.

```
Path A:  Vapi JSON  --VapiAdapter--> CanonicalCall
Path B:  NativeSnapshot JSON --NativeAdapter--> CanonicalCall
                                ↑
                         VoiceCall / CallRecorder
```

Same destination. Different envelope. You never mix them on one call.

## obsalt serve

The **HTTP server** (`obsalt serve`, default `:8080`). FastAPI.

- Ingest: `/v1/ingest/{vapi,retell,bland,openai-realtime,native}`
- Evidence: `/v1/calls`, `/search`, `/hangups`, `/latency`, `/evals`
- Join view: `/v1/ui`, `/v1/calls/{id}/ui`, `/v1/calls/{id}/view`
- OpenAPI: `/docs`
- Health: `/health`

It can also **reconstruct** traces from a stored call and export them over OTLP. That is how Path A gets a waterfall without `VoiceCallTracer` in the vendor’s process.

## CanonicalCall

The internal normalized record. Adapters fill it. The pipeline analyzes it. The HTTP API returns it. The span emitter walks it. You rarely build one by hand.

## call.id vs call.provider_id

| Attribute / field | Meaning |
| --- | --- |
| `call.id` / `CanonicalCall.id` | obsalt uuid5(`org`, `provider`, your call id). Join Tempo → `GET /v1/calls/{id}` |
| `call.provider_id` / `provider_call_id` | **Your** id: Pipecat room, SIP Call-ID, Vapi call id, Realtime session |

`VoiceCall.start(call_id="room-42", workspace_id="acme")` stamps both. `workspace_id` must be the org on the API key.

## Evidence vs traces

| | Traces (OTLP) | Evidence (store) |
| --- | --- | --- |
| Purpose | Where time went | What was said |
| Contents | Span names, timings, join keys | Transcript, tools, hangup, evals |
| Backend | Tempo / Jaeger / Honeycomb | `obsalt serve` (in-memory in v0.1) |
| PII | Forbidden | Stored, redacted where possible |
| Human view | Grafana (fleet) | `/v1/ui` joins both for one call |

## spans_exported

Flag on a native snapshot. `VoiceCall` sets it `true` because live spans already went to Tempo. The server then skips building a second `call.lifecycle` (it still stores evidence and may attach eval spans to the live trace via `traceparent`).

`CallRecorder` alone leaves it `false`, so the server reconstructs the tree — that is the point of the no-OTel path.
