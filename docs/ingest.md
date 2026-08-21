# Ingest webhooks

Use this guide for **Path A**: a hosted platform runs the call and you receive webhooks.

Path B (Pipecat / your loop) does not use these vendor URLs. Use [Custom agents](custom-agents.md) and [Native snapshots](providers/native.md).

The server:

1. Accepts a provider payload (vendor JSON, or a native snapshot).
2. Normalizes it to a `CanonicalCall`.
3. On a terminal event: analyzes the call, stores evidence, and — unless `spans_exported` — reconstructs the span tree.

If you own STT/LLM/TTS in-process, use [Custom agents](custom-agents.md) instead.

Provider-by-provider dashboard steps, headers, and field maps:

- [Vapi](providers/vapi.md)
- [Retell](providers/retell.md)
- [Bland](providers/bland.md)
- [OpenAI Realtime](providers/openai-realtime.md)
- [Native snapshots](providers/native.md)

Pipeline detail: [Data flow](data-flow.md).

## Run the server

```bash
obsalt init
obsalt doctor
obsalt serve --host 0.0.0.0 --port 8080
```

Equivalent env-only form:

```bash
export OBSALT_OTLP_ENDPOINT=http://localhost:4318   # omit to skip trace export
export OBSALT_API_KEYS=acme:secret
obsalt serve --host 0.0.0.0 --port 8080
```

Interactive OpenAPI: `http://localhost:8080/docs`. Health: `GET /health`. Config reference: [Configuration](configuration.md).

Without `OBSALT_OTLP_ENDPOINT`, evidence is still stored and analyzed. Reconstructed spans have nowhere to go.

## Auth

Send the secret from `OBSALT_API_KEYS` (`org:secret`, comma-separated):

- `X-API-Key: secret`
- or `Authorization: Bearer secret`

obsalt maps the secret to `org_id`. Set `OBSALT_REQUIRE_AUTH=true` in production so missing keys are rejected.

Provider HMAC is separate. Empty secret = signature check skipped (local only).

| Variable | Header |
| --- | --- |
| `OBSALT_VAPI_SECRET` | `x-vapi-secret` |
| `OBSALT_RETELL_SECRET` | `x-retell-signature` (HMAC-SHA256 of the body) |
| `OBSALT_BLAND_SECRET` | `x-webhook-secret` or `Authorization: Bearer …` |

Most vendor dashboards **cannot** send `X-API-Key`. Use HMAC + a private listener, or a proxy that injects the API key. Details on each provider page.

## Point the provider at obsalt

| Provider | obsalt path | Typical trigger |
| --- | --- | --- |
| Vapi | `POST /v1/ingest/vapi` | Server URL; `end-of-call-report` finalizes |
| Retell | `POST /v1/ingest/retell` | `call_ended` |
| Bland | `POST /v1/ingest/bland` | Post-call webhook; live `category=latency` events merge |
| OpenAI Realtime | `POST /v1/ingest/openai-realtime` | Batch of session events, each with `t_ms` |
| Native | `POST /v1/ingest/native` | `VoiceCall` / `CallRecorder.snapshot()` from your code |

```bash
curl -X POST http://localhost:8080/v1/ingest/vapi \
  -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d @end-of-call-report.json
```

Local dry-run (no server):

```bash
obsalt parse end-of-call-report.json
```

Response:

```json
{
  "call_id": "3f1a0c2e-…",
  "provider_call_id": "vapi-call-id",
  "status": "finalized",
  "created": true,
  "finalized": true
}
```

`status` is `accepted` or `merged` for live events, `finalized` for terminal ones. `call_id` is obsalt’s id (`uuid5` of org + provider + provider call id). Use it with `GET /v1/calls/{call_id}`.

## What each provider can reconstruct

| Source | Reconstructed | Often unknown |
| --- | --- | --- |
| Vapi `end-of-call-report` | Turns, tools, per-turn llm/tts/e2e from metadata / `performanceMetrics`, hangup | STT vendor unless `assistant.transcriber` is present |
| Retell `call_ended` | `latency.asr/llm/tts.values[]` as per-turn samples, `transcript_with_tool_calls`, `disconnection_reason` | Inside-provider spans |
| Bland post-call + `category=latency` | Transcript timestamp gaps as e2e/TTFA, `TTS: 218ms` lines, disposition | Fine-grained STT vendor |
| OpenAI Realtime events with `t_ms` | STT = speech_stopped → transcription.completed; TTFA = speech_stopped → first audio delta; tools from function_call events | Provider-internal TTS |
| Native snapshot | Whatever you put on `CallRecorder` | Whatever you omitted |

obsalt does not invent fallbacks. One STT hop in the payload is one `stt.provider.{name}` child.

After a terminal event, hangup taxonomy, hallucination flags, and rubrics attach `evaluation.assertion_check` spans to the same root. Search runs on the evidence store, not on span attributes.

## Native snapshots

See [Native snapshots](providers/native.md). Short form with `VoiceCall`:

```python
from obsalt import VoiceCall, ObsaltClient, setup_tracing

setup_tracing(otlp_endpoint="http://localhost:4318")
with VoiceCall.start(call_id=session_id, workspace_id="acme", agent_id="concierge", client=ObsaltClient(api_key="secret")) as call:
    with call.turn(0, "user", text="book Friday") as turn:
        with turn.stt("deepgram") as stt:
            stt.set(latency_ms=120)
```

## Look up a call

```bash
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls/$CALL_ID
curl -H "X-API-Key: secret" "http://localhost:8080/v1/search?q=refund"
curl -H "X-API-Key: secret" http://localhost:8080/v1/hangups
curl -H "X-API-Key: secret" http://localhost:8080/v1/latency
```

Full reference: [HTTP API](api.md).
