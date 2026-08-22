# Hosted webhook

Use this guide when a hosted voice platform owns the pipeline. obsalt
receives a signed observational webhook, persists the raw body, and decodes
it in a worker.

```
Provider  --POST /v1/ingest/{provider}/{ingest_key}-->  obsalt receive
                                                         |  raw blob + inbox/outbox
                                                         v
                                                       worker decode → redact → assemble
```

## Create a connection

```bash
curl -sS -X POST http://localhost:8080/v1/connections \
  -H "X-API-Key: $OBSALT_BOOTSTRAP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"provider":"vapi","secrets":{"shared_secret":"..."}}'
```

Save `ingest_key`. It is shown once. The URL you give the provider is:

```
https://obsalt.example.com/v1/ingest/vapi/<ingest_key>
```

Each tenant has its own key and its own encrypted credentials. There is no
global `OBSALT_VAPI_SECRET`.

## Vapi

**Auth.** Distinct static Bearer, OAuth2, and HMAC paths. HMAC configuration
includes algorithm, signature header, optional timestamp header, and payload
canonicalization. Legacy `X-Vapi-Secret` shared secret is supported.

**Events.** Configure the server URL for observational events:
`end-of-call-report`, transcripts, status. Do **not** point
`assistant-request`, `tool-calls`, `transfer-destination-request`, or
`knowledge-base-request` at obsalt. Connection validation rejects those.

**Units.** `transcriberLatency` / `modelLatency` / `voiceLatency` /
`turnLatency` / `endpointingLatency` are milliseconds and **unplaced**.
`secondsFromStart` + `duration` are real turn anchors. Word-level confidence
and `numAssistantInterrupted` / `numUserInterrupted` are consumed when
present.

**Hangup.** `endedReason` is mapped from the pinned schema enum, with prefix
rules for `call.in-progress.error-vapifault-*`, `call.start.error-*`,
`call.ringing.*`, `call.ending.*`. `*-voice-failed` resolves to TTS before
the provider-name token resolves it to STT/LLM. CI reports
`(mapped_count / current_enum_count)` and targets >95%.

## Retell

**Auth.** Header `X-Retell-Signature`: `v={unix_ms},d={hex}`.
`HMAC-SHA256(raw_body + timestamp)` keyed by the **API key**. ±5 minute
window.

**Units.** `words[].start/end` are **seconds**. obsalt converts them. Do not
"fix" this by treating them as milliseconds — that is the 290ms-vs-740ms
bug. Call-level `p50` / `p90` / `p95` / `p99` are `AggregateMeasurement`s
and never enter sample percentiles.

**Tools.** Retell does not provide direct invocation/result timestamps.
Exact duration is unsupported. `transcript_with_tool_calls` can associate a
call with an utterance for coarse placement. The UI labels that and shows
`duration: not reported by Retell` rather than an empty bar.

## ElevenLabs

**Auth.** Header `ElevenLabs-Signature`: `t={unix},v0={hex}`.
`HMAC-SHA256("{t}.{body}")`. Upstream documents a one-sided 30-minute
tolerance; obsalt enforces both sides.

**Formats.** Post-call JSON is `message_level` (whole-second message
anchors, no documented end timestamps). The OTLP-shaped webhook is mapped
per span; a waterfall renders only where the interval contract passes.

Some ElevenLabs guides specify or recommend HTTP 200; the OTLP-shaped
endpoint accepts any 2xx. The plugin pins the applicable acknowledgement.

## Cartesia Line

**Auth.** Header `x-webhook-secret`, plain shared secret. Weakest committed
scheme; documented as such.

**Fidelity.** Real turn intervals plus unplaced STT/TTS TTFBs. No stage
waterfall.

## What happens after the ack

1. The raw body is in object storage under
   `org/{org_id}/raw/{provider}/{delivery_key}/{content_sha}`.
2. Postgres holds the inbox row, delivery-key dedupe, and outbox record.
3. A worker decodes, redacts, assembles a complete candidate revision,
   writes ClickHouse, and CAS-promotes the Postgres pointer.
4. Tier-1 analysis and search indexing run on the active revision.
5. Tier-2 analysis runs only if triggered or sampled.

Duplicate deliveries resume incomplete acceptance. They do not blindly
return "already processed."

## Replay

```bash
curl -sS -X POST http://localhost:8080/v1/replay \
  -H "X-API-Key: $OBSALT_BOOTSTRAP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"provider":"vapi","source_call_id":"call_123"}'
```

Replay re-decodes retained raw bytes and promotes a **new** revision. It
cannot invent data past the raw or provider horizon. `obsalt retain`
reports that horizon.

## Backfill

Webhooks are lossy. `POST /v1/backfill` triggers a provider pull for
plugins that implement `RestBackfill`. Identity is
`(connection, upstream_entity_id, content_hash)`. Different snapshots
without an ordered revision become a conflict, not a silent overwrite.
