# Troubleshooting

**Who this is for:** the console is empty, the webhook bounced, or
someone filed “waterfall missing.”

**Question this page answers:** what is actually broken?

If you are still installing, start at
[Getting started](getting-started.md). Symptoms first. Run these three
before you read further:

```bash
obsalt doctor
curl -sS -H "X-API-Key: ${OBSALT_BOOTSTRAP_API_KEY:-dev-key}" http://localhost:8080/ready
obsalt plugins
```

`doctor` probes Postgres, ClickHouse, object storage, and Redis.
`--skip-network` prints plugins and config warnings only. `--json` is
for scripts. Exit `2` means a required store is down. Exit `1` means no
plugins are loaded.

---

## The console is empty

| Check | What “good” looks like |
| --- | --- |
| `obsalt worker` is running | Decode never happens on the webhook ack. Production is `serve` **and** `worker`. |
| `GET /ready` | `outbox_depth` draining toward 0. `dlq_depth` 0. `insecure_defaults` true only on localhost. |
| Plugin installed | `obsalt plugins` lists `vapi` / `retell` / … Settings → Plugins in the UI. |
| A **live** call was placed | Fixtures in this repo are for tests. The console fills from production traffic. |
| Time filters | Collection pages (calls, latency, hangups, quality, search) and the matching `/v1` lists require `start` and `end`. |

Replay a retained envelope after a decoder fix:

```bash
curl -sS -X POST http://localhost:8080/v1/replay \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"provider":"vapi","source_call_id":"call_123"}'
```

Raw blobs expire (default 30 days). Expired raw cannot be re-decoded.

---

## `obsalt serve` exits 2

```
Could not connect to Postgres / ClickHouse / object storage.
Supported path: docker compose up -d && obsalt serve
```

There is no SQLite mode. Start compose, wait until healthy, then
`obsalt doctor`.

```bash
docker compose up -d
docker compose ps
obsalt doctor
```

Postgres is `localhost:5432`, ClickHouse `8123`, Redis `6379`, MinIO
`9010` (console `9001`). If those ports are taken, change the compose
file **and** the matching `OBSALT_*` URLs.

---

## Webhook returns 401 / 404 / 4xx

| Status | Meaning |
| --- | --- |
| **404** `plugin not installed` | `pip install obsalt-vapi` (or the provider you pointed at). Restart `serve`. |
| **401** / plugin `bad_signature` | Wrong secret, wrong auth mode, or the provider is signing a different byte sequence than you think. Empty secrets **fail closed** — they do not skip verification. |
| **401** `invalid API key` on `/v1/*` | `X-API-Key` is missing or is not the bootstrap / rotated key. |
| Connection validation rejects the event | You pointed the **application** webhook (assistant-request, tool-calls) at obsalt. Observational events only. See [Connect a hosted platform](connect-hosted.md). |

Decode without ingesting:

```bash
obsalt parse path/to/payload.json --provider vapi
```

---

## OTLP exporter gets 401 / 415 / 503

| Status | Meaning |
| --- | --- |
| **401** | `X-API-Key` missing, wrong, or a span tried to assert a different `obsalt.org` than the key. Tenancy is the key. |
| **415** | `Content-Type` must be `application/x-protobuf` or `application/json`. |
| **400** | Malformed protobuf. Do not retry that body. |
| **503** | Outbox deeper than `OBSALT_OUTBOX_BACKPRESSURE_LIMIT`. Retry the **whole** batch. |
| Partial-success protobuf | Permanent identity conflict. **Do not retry** those records. |

`setup_tracing(otlp_endpoint="http://localhost:8080", api_key="dev-key")`
appends `/v1/traces` for you.

---

## “The waterfall is missing”

Read your row in [the console](console.md) table before filing a bug.

- **Vapi / Retell / Cartesia:** no stage waterfall. Chips and aggregates
  only. That is T1: we do not draw what we did not measure.
- **ElevenLabs post-call JSON:** whole-second anchors, not millisecond
  bars.
- **Pipecat / LiveKit:** waterfall only where **your** spans have real
  start and end. Empty cascade stages are a bug in the emitter.
- **Realtime / Gemini:** `user_input` / `generation` / `playout` only.
  We will not invent STT / LLM / TTS.

Provenance statuses:

| Status | What to do |
| --- | --- |
| `unsupported` | This source cannot send it. Stop expecting it. |
| `absent` | This source *can*; this call did not. Check the provider config. |
| `decode_failed` | Plugin bug or unexpected payload. `parse`, then `replay` after a fix. |
| `redacted` | Arrived; choke point stripped it. |

---

## Evals are empty or `budget_blocked`

- Deterministic flags (prices, ids, phantom tools) run on every call.
  LLM evals do not.
- `OBSALT_LLM_MONTHLY_BUDGET_USD` defaults to `0`. Paid judges will
  not run until you raise it.
- `OBSALT_JUDGE_BASE_URL` / `OBSALT_JUDGE_API_KEY` must be set for the
  OpenAI-compatible judge. Otherwise you get the heuristic stand-in.
- Empty grounding → hallucination checks that need it fail closed.
- `sampled_out` means the unbiased sample rate skipped this call.
  Manual “Evaluate this call” still works (and still spends budget).

---

## Search finds nothing

- Time range is required on `POST /v1/search`.
- Indexing happens after a revision is promoted. Worker down → no docs.
- Content is **redacted** before embed. Queries that need a raw phone
  number will not hit.
- Default embedder is local ONNX. If `OBSALT_EMBEDDER_ONNX_PATH` points
  at a missing file, lexical search still runs.

---

## Sign-in / CSRF / “session required”

- UI login is `POST /v1/ui/login` with the API key. Cookie is HTTP-only.
- Mutating UI posts need the CSRF field from the page. Do not strip it.
- `secure` cookies turn on when defaults are no longer insecure. On
  plain HTTP with rotated secrets, use TLS or you will not get a cookie.

---

## Doctor says plugins: none

Core ships **no** providers.

```bash
pip install -r requirements-dev.txt   # clone
# or
pip install obsalt-vapi obsalt-retell
obsalt plugins
```

Entry points are discovered at process start. Restart `serve` / `worker`
after installing a plugin.

---

## Deletion “did nothing”

`POST /v1/privacy/deletion-requests` returns `accepted` immediately.
It is not done until `completed_at` is set. `GET /ready` →
`deletion_backlog`. Tombstones survive restore. External copies cannot
be revoked.

---

## Still stuck

1. Provenance panel on the call.
2. `GET /ready` and `GET /metrics`.
3. `obsalt parse` the raw payload.
4. [Operate](ops.md) for restore, retention, and rotation.
5. [Architecture](architecture.md) if you are about to change code.
