# Scenarios

These are the stories the rest of the docs compress. Each one names what you run, what you send, and what you should see in Tempo vs `GET /v1/calls`.

## 1. Ada calls Vapi support and is quoted a fake order number

**Setup.** You run `obsalt serve` with `OBSALT_API_KEYS=acme:secret` and Vapi’s Server URL = `https://obsalt.example/v1/ingest/vapi`. [Vapi setup](providers/vapi.md).

**Call.** Ada: “I want a refund.” The assistant calls `lookup_order` (email in the arguments). The tool returns `not_found`. The model still says it processed **ORD-99999** for **$48.50**. Ada: “That number is wrong. This is useless.” She hangs up.

**What obsalt does.** Vapi `end-of-call-report` is terminal. The adapter reads `artifact.messages` (turns + tool call/result), `endedReason=customer-ended-call`, assistant system prompt (“Never invent order numbers”). Finalize:

| Signal | Result |
| --- | --- |
| Hangup | `user_hangup` / party `user`, high `loss_score` (user hangup + “useless” + hallucination + failed tool) |
| Tools | `lookup_order` status `error`; `payload_shape.email = string`; `ada@example.com` **not** stored |
| Hallucinations | `fabricated_id` (ORD-99999), `price_claim` ($48.50) — neither token is in prompt or tool result |
| Eval “Grounded claims” | Fail |

**Where you look.**

- Tempo: `call.lifecycle` → `turn.*` → `llm.tool_call.lookup_order` in ERROR, plus `evaluation.assertion_check` fails.
- `GET /v1/hangups` cluster `user_hangup|user|…` with `lost_customer_call_id` pointing at this call.
- `GET /v1/search` query `refund` hits the transcript.

The fixture that produces this: `tests/fixtures/vapi_end_of_call.json`. `obsalt parse` it without a server.

## 2. Your Pipecat agent falls back from Deepgram to Azure

**Setup.** No webhook. The agent process calls `setup_tracing(otlp_endpoint=...)` and `VoiceCallTracer`. [Custom agents](providers/custom-agent.md).

**Call.** User speaks. Deepgram times out. Azure returns text, confidence 0.91. LLM and TTS succeed.

**What you write.**

```python
with call.turn(0, "user") as turn:
    with turn.stt("deepgram") as stt:
        with stt.provider_attempt("deepgram") as attempt:
            attempt.fail("timeout")
        with stt.provider_attempt("azure", fallback=True) as attempt:
            attempt.set(latency_ms=400, confidence=0.91)
```

**What Tempo shows.** `stt.transcription` with children `stt.provider.deepgram` (ERROR) and `stt.provider.fallback.azure`. obsalt will **not** invent a fallback on a Vapi ingest — only your SDK path can express two hops.

**Evidence.** Still empty until you `CallRecorder.snapshot()` and POST native. If you only care about the waterfall, skip that.

## 3. Retell billing bot looks up an invoice that exists

**Setup.** Retell agent webhook → `/v1/ingest/retell`, HMAC configured.

**Call.** User asks for INV-1001. Tool `lookup_invoice` succeeds with `$20.00`. Agent reads it back. Agent hangup.

**What obsalt does.** `latency.asr/llm/tts.values[]` become per-turn samples (not just p50). `transcript_with_tool_calls` becomes turns + tools. `disconnection_reason=agent_hangup`. Price **$20.00** is in the tool result, so it is **not** a hallucination. Eval “Tools succeed” passes.

**Where you look.** `GET /v1/latency` STT p50 ≈ 110 ms (from `[100, 140]`). `GET /v1/tools` shows `lookup_invoice` success_rate 1.0, shape `{invoice_id: string}`.

Fixture: `tests/fixtures/retell_call_ended.json`.

## 4. Bland outbound confirms an appointment, then transfers

**Setup.** Bland post-call webhook → `/v1/ingest/bland`. During the call, live `category=latency` lines may already have arrived.

**Call.** Assistant confirms tomorrow 10am. User: “I need to speak to a human.” Assistant transfers.

**What obsalt does.** `disposition_tag=TRANSFERRED` + `transferred_to` → hangup `transfer`. Transcript timestamps 08.000 → 09.200 yield e2e/TTFA **1200 ms** on that agent turn. `agent-action` “Transferred call” is a tool-shaped row. Live `TTS: 218ms` (if you sent it) is merged onto the same `provider_call_id`.

**Where you look.** Hangup clusters by transfer, not user_hangup. Latency rollup includes both derived gaps and live TTS samples.

Fixture: `tests/fixtures/bland_post_call.json` plus a live event with the same `call_id`.

## 5. OpenAI Realtime concierge books a room

**Setup.** Your sidecar buffers Realtime events, stamps `t_ms`, POSTs one batch to `/v1/ingest/openai-realtime`.

**Call.** User: “Book Friday.” Model calls `create_booking`. Tool returns `HTL-4421` at `$189` (that rate is in `session.instructions`). Agent speaks the confirmation. User barges in with “Great, thanks.”

**What obsalt does.**

| Derived sample | From events |
| --- | --- |
| STT 170 ms | `speech_stopped` 1750 → transcription.completed 1920 |
| TTFA | last `speech_stopped` → first `response.audio.delta` |
| Tool success | `function_call_arguments.done` + `function_call_output` |
| Interrupt | `speech_started` while an agent turn exists → `interrupted=true` |

HTL-4421 and $189 are grounded (tool + instructions) → no hallucination flags.

Fixture: `tests/fixtures/openai_realtime_session.json`.

## 6. Two services, one call (W3C traceparent)

The web gateway starts `VoiceCallTracer.start(..., agent_id="web")`, injects `traceparent` on the queue message. The worker does `VoiceCallTracer.start(..., headers=headers, agent_id="worker")` and runs `call.evaluate("grounded-claims")`.

Tempo shows **one** trace. `workspace.id` and `call.id` match. The worker still does not write evidence; the ingest server does, if you also snapshot.

## Choosing a scenario as a template

| If you… | Copy |
| --- | --- |
| Pay Vapi / Retell / Bland | Scenario 1, 3, or 4 + the matching provider page |
| Own the loop and care about fallbacks | Scenario 2 |
| Use gpt-realtime / WebRTC | Scenario 5 |
| Split ingest and eval across services | Scenario 6 |
