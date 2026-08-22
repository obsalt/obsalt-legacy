# Connect a hosted platform

Use this when **Vapi, Retell, ElevenLabs, or Cartesia** owns STT / LLM / TTS.
obsalt receives a signed webhook after (or during) the call. You do **not**
import `VoiceCall` for these calls.

```
Provider dashboard
    │  "server URL" / "webhook URL"
    ▼
POST /v1/ingest/{provider}/{ingest_key}     ← you create this URL
    │  ack immediately
    ▼
worker decodes → console shows the call
```

If you cannot answer "what is the start timestamp of the LLM stage on turn
3?", you will not get a stage waterfall. You will still get a transcript,
hangup, tools, evals, and honest latency chips. That is the hosted-platform
product.

## 1. Install the plugin

Core ships no providers.

```bash
pip install obsalt-vapi          # or obsalt-retell / obsalt-elevenlabs / obsalt-cartesia
# from a clone, already covered by requirements-dev.txt
obsalt doctor                    # should list the plugin
```

## 2. Create a connection

Each tenant gets its own ingest URL and its own encrypted credentials.
There is no global `OBSALT_VAPI_SECRET`.

```bash
export KEY="${OBSALT_BOOTSTRAP_API_KEY:-dev-key}"

curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"provider":"vapi","secrets":{"legacy_secret":"replace-me"},"settings":{"auth_mode":"legacy_secret"}}'
```

The response includes `ingest_key` **once**. Store it. The URL you paste
into the provider is:

```
https://<your-host>:8080/v1/ingest/vapi/<ingest_key>
```

Swap `vapi` for `retell`, `elevenlabs`, or `cartesia`.

Empty secrets fail closed. They do not mean "skip verification."

## 3. Point the provider — then place a real call

Only **observational** events belong here. obsalt is not your application's
webhook. If the platform asks your server for the next tool result or the
next assistant, that request must still hit **your** app.

Then place a live call on that agent. Wait a few seconds. Refresh
`/v1/ui`. Open the row. Read the provenance panel.

Replay later with `POST /v1/replay` if a decoder bug ate a payload that is
still inside the raw-retention window (default 30 days).

---

## Vapi

**Paste this into Vapi** as a Server URL for observational messages, not as
the handler for assistant or tool requests.

| Secret / setting | What to send |
| --- | --- |
| `secrets.legacy_secret` + `settings.auth_mode=legacy_secret` | Shared `X-Vapi-Secret` |
| `secrets.bearer_token` + `auth_mode=bearer` | `Authorization: Bearer …` |
| `secrets.hmac_secret` + `auth_mode=hmac` | HMAC; set `hmac_algorithm`, `hmac_header`, optional `timestamp_header` |
| `secrets.oauth_token` + `auth_mode=oauth2` | OAuth2 bearer |

**Send:** `end-of-call-report`, final transcripts, `status-update`,
`user-interrupted`.

**Do not send:** `assistant-request`, `tool-calls`, `function-call`,
`transfer-destination-request`, `knowledge-base-request`. Connection
validation rejects those. They have to reach your application.

**You will see:** transcript, hangup (`endedReason` mapped), tools (no
measured duration), cost, recording ref when present, interruption counts,
word-level confidence, per-turn stage durations as **chips**.

**You will not see:** a stage waterfall. `transcriberLatency` /
`modelLatency` / `voiceLatency` / `turnLatency` / `endpointingLatency` are
milliseconds **without stage timestamps**. The console will say that.

## Retell

**Auth.** `X-Retell-Signature: v={unix_ms},d={hex}`.
`HMAC-SHA256(raw_body + timestamp)` keyed by the **Retell API key**.
±5 minutes.

```bash
curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"retell","secrets":{"api_key":"<retell-api-key>"}}'
```

Point Retell's webhook at `/v1/ingest/retell/<ingest_key>`.

**You will see:** transcript, word timings (Retell sends **seconds**; we
convert), hangup, tools without a duration, recording, cost, knowledge and
user grounding, call-level p50/p95 as **aggregates**.

**You will not see:** those p50/p95 values drawn as span widths, or mixed
into sample percentiles. Tool bars with a real duration. A stage waterfall.

## ElevenLabs

**Auth.** `ElevenLabs-Signature: t={unix},v0={hex}`.
`HMAC-SHA256("{t}.{body}")`. 30-minute window, both sides.

```bash
curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"elevenlabs","secrets":{"webhook_secret":"..."}}'
```

Subscribe to `post_call_transcription` (and `post_call_audio` if you want
the recording). Those two share a conversation id and must not dedupe as
one delivery — they do not.

**You will see:** transcript at whole-second message anchors, hangup, user
grounding. If they send the OTLP-shaped webhook, a waterfall **only** on
spans that have real clocks.

**You will not see:** millisecond stage bars from the post-call JSON.

## Cartesia Line

**Auth.** Header `x-webhook-secret`, a plain shared secret. Weakest of the
committed schemes; said here so you treat it that way.

```bash
curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"cartesia","secrets":{"webhook_secret":"..."}}'
```

**You will see:** turns with real start/end when Cartesia sends them,
unplaced STT/TTS TTFB chips, transcript, user grounding.

**You will not see:** a cascade waterfall.

## After the ack

1. Raw body lands in object storage:
   `org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}`.
2. Postgres holds the inbox row, delivery-key dedupe, and outbox record.
3. The worker decodes, redacts, assembles a complete revision, writes
   ClickHouse, then CAS-promotes the Postgres pointer.
4. Cheap analysis (latency, hangup, tools, coverage) runs on every call.
5. LLM evals run only if you asked, or if a sample/budget allows it.

Duplicates resume incomplete work. They do not blindly return "already
processed."

```bash
# Re-decode a retained envelope after a plugin fix
curl -sS -X POST http://localhost:8080/v1/replay \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","source_call_id":"call_123"}'
```

Replay cannot invent data past the raw or provider horizon.

Webhooks are lossy. `POST /v1/backfill` pulls from the provider if that
plugin implements `RestBackfill` (Vapi does).

Next: [The console](console.md).
