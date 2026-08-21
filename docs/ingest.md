# Ingest webhooks

Use this guide when a **hosted platform** runs the call and you receive webhooks — or when you want obsalt to store evidence and run analysis.

The server:

1. Accepts a provider payload.
2. Normalizes it to a `CanonicalCall`.
3. On a terminal event: analyzes the call, stores evidence, and reconstructs the same span tree the SDK would have emitted.

If you own STT/LLM/TTS in-process and only need a waterfall, use [Instrument an agent](instrumentation.md) instead.

## Run the server

```bash
export OBSALT_OTLP_ENDPOINT=http://localhost:4318   # omit to skip trace export
export OBSALT_API_KEYS=acme:secret
obsalt --host 0.0.0.0 --port 8080
```

Interactive OpenAPI: `http://localhost:8080/docs`. Health: `GET /health`.

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

## Point the provider at obsalt

| Provider | obsalt path | Typical trigger |
| --- | --- | --- |
| Vapi | `POST /v1/ingest/vapi` | Server URL; `end-of-call-report` finalizes |
| Retell | `POST /v1/ingest/retell` | `call_ended` |
| Bland | `POST /v1/ingest/bland` | Post-call webhook; live `category=latency` events merge |
| OpenAI Realtime | `POST /v1/ingest/openai-realtime` | Batch of session events, each with `t_ms` |
| Native | `POST /v1/ingest/native` | `CallRecorder.snapshot()` from your code |

Example:

```bash
curl -X POST http://localhost:8080/v1/ingest/vapi \
  -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d @end-of-call-report.json
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

obsalt does not invent fallbacks. One STT hop in the payload is one `stt.provider.{name}` child.

After a terminal event, hangup taxonomy, hallucination flags, and rubrics attach `evaluation.assertion_check` spans to the same root. Search runs on the evidence store, not on span attributes.

## Native snapshots

Use `CallRecorder` when you own the loop but do not want OpenTelemetry in the agent process — or when you already emit live spans and also want evidence/evals.

```python
from obsalt import CallRecorder
from obsalt.domain.enums import Speaker
import httpx

rec = CallRecorder(provider="openai_realtime", call_id=session_id, agent_id="concierge")
with rec.turn(Speaker.USER, "book Friday") as turn:
    turn.stt_ms = 120
with rec.tool("create_booking", {"night": "Friday"}) as tool:
    tool.set_result({"confirmation": "HTL-1"})
with rec.turn(Speaker.AGENT, "Booked HTL-1") as turn:
    turn.llm_ms = 300
    turn.tts_ms = 90
payload = rec.snapshot(hangup_reason="completed")

httpx.post(
    "http://localhost:8080/v1/ingest/native",
    json=payload,
    headers={"X-API-Key": "secret"},
)
```

The server reconstructs traces from this packet the same way it does for Vapi.

## Look up a call

```bash
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls
curl -H "X-API-Key: secret" http://localhost:8080/v1/calls/$CALL_ID
curl -H "X-API-Key: secret" "http://localhost:8080/v1/search?q=refund"
curl -H "X-API-Key: secret" http://localhost:8080/v1/hangups
curl -H "X-API-Key: secret" http://localhost:8080/v1/latency
```

Full reference: [HTTP API](api.md).
