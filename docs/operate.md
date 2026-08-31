# Operate

Supported path: `docker compose up -d`. That is the stores,
`obsalt serve` (API + console on :8080), and `obsalt worker`. There is
no SQLite or Postgres-only production mode.

Replace every `change-me` and `dev-key` before the process is reachable
from a network you do not trust. `/ready` reports `insecure_defaults`.

| Task | Where |
| --- | --- |
| Liveness / readiness / Prometheus | `/health`, `/ready`, `/metrics` — the console health strip repeats `/ready` when something is off |
| Replay retained raw | Call detail → Replay this call, or Settings → Privacy, or `POST /v1/replay` (~30 days) |
| Verified deletion | Call detail (type the call id) or Settings → Privacy. Done means `completed_at` is set |
| Rotate a service key | Settings → Keys, or `POST /v1/keys/rotate` (old + new accept for 24h by default) |
| Export active revisions | Settings → Privacy download, or `obsalt export` |

## Health

`GET /health` is liveness. `GET /ready` reports inbox age, outbox
depth, DLQ depth, orphan-blob count, and deletion backlog.

OTLP capacity pressure is a retryable HTTP 503 when outbox depth
exceeds `OBSALT_OUTBOX_BACKPRESSURE_LIMIT`.

Before you debug a screen:

```bash
obsalt doctor
curl -sS -H "X-API-Key: ${OBSALT_BOOTSTRAP_API_KEY:-dev-key}" http://localhost:8080/ready
obsalt plugins
```

`doctor` exit `2` means a required store is down. Exit `1` means no
plugins.

## The console is empty

| Check | What “good” looks like |
| --- | --- |
| `obsalt worker` is running | Decode never happens on the webhook ack. Production is `serve` **and** `worker`. |
| `GET /ready` | `outbox_depth` draining toward 0. `dlq_depth` 0. |
| Plugin installed | `obsalt plugins` lists `vapi` / `retell` / … |
| A live call was placed | Fixtures are for tests. Locally: **Load sample calls** or `obsalt seed`. |
| Time filters | UI defaults to last 7 days UTC. `/v1` lists require ISO `start` and `end`. |

Raw blobs expire (default 30 days). Expired raw cannot be re-decoded.

## `obsalt serve` exits 2

There is no SQLite mode. Start compose, wait until healthy, then
`obsalt doctor`. Postgres is `localhost:5432`, ClickHouse `8123`,
Redis `6379`, MinIO `9010`.

## Webhook 401 / 404

| Status | Meaning |
| --- | --- |
| **404** `plugin not installed` | Install the extra. Restart `serve`. |
| **401** / `bad_signature` | Wrong secret or auth mode. Empty secrets fail closed. Vapi’s default is `Authorization: Bearer`. |
| **401** `invalid API key` on `/v1/*` | `X-API-Key` missing or not the bootstrap / rotated key. |
| Connection validation rejects the event | You pointed an **application** webhook at obsalt. Observational events only. |

Decode without ingesting: `obsalt parse path/to/payload.json --provider vapi`.

## Local groundedness encoder

Optional. The default compose file and image stay CPU-only — torch is
not installed and sample rate `0` is a no-op.

```bash
uv pip install 'obsalt[groundedness]'
```

Install the extra in a **custom image** (do not add `--all-extras` to
the default Dockerfile), then Settings → Evals → Local groundedness
(per org, no restart). Env `OBSALT_GROUNDEDNESS_ENABLED` /
`OBSALT_GROUNDEDNESS_SAMPLE_RATE` is bootstrap until that org saves a
policy row. Sample rate `0` means no calls run. First enabled run
downloads HuggingFace weights unless `OBSALT_GROUNDEDNESS_MODEL` is a
filesystem path, for example:

```bash
OBSALT_GROUNDEDNESS_MODEL=/models/lettucedect-v2-mmbert-base
```

## OTLP 401 / 415 / 503

| Status | Meaning |
| --- | --- |
| **401** | `X-API-Key` missing, wrong, or a span asserted a different `obsalt.org` than the key. |
| **415** | `Content-Type` must be `application/x-protobuf` or `application/json`. |
| **400** | Malformed protobuf. Do not retry that body. |
| **503** | Outbox deeper than the backpressure limit. Retry the **whole** batch. |

## The waterfall is missing

Read your row in [the console](console.md) table before filing a bug.

- **Vapi / Retell / Cartesia:** no stage waterfall. Chips and aggregates
  only.
- **ElevenLabs post-call JSON:** whole-second anchors.
- **Pipecat / LiveKit:** waterfall only where **your** spans have real
  start and end.
- **Realtime / Gemini:** `user_input` / `generation` / `playout` only.

| Provenance | What to do |
| --- | --- |
| `unsupported` | This source cannot send it. Stop expecting it. |
| `absent` | This source *can*; this call did not. |
| `decode_failed` | Plugin bug. `parse`, then replay after a fix. |
| `redacted` | Arrived; choke point stripped it. |

## Restore from raw

1. Confirm the envelope is still in object storage.
2. `POST /v1/replay` with `provider` and `source_call_id` (or `call_id`).
3. The worker promotes a **new** call revision.
4. Fleet rollups publish a new serving generation.

## Verified deletion

Deletion cannot be undone. Tombstones are checked by receive, replay,
backfill, indexing, and export.

1. `POST /v1/privacy/deletion-requests` with `call_id`, `caller`, or
   `start`/`end`.
2. Status `accepted` is not done.
3. Completion sets `completed_at`. External warehouse copies cannot be
   revoked.

## Rotation, retention, export

- `POST /v1/keys/rotate` — old + new accept for
  `OBSALT_KEY_ROTATION_OVERLAP_SECONDS` (default 24h).
- Defaults: 30 days raw, 90 days transcripts, 400 days aggregates.
  `obsalt retain` sweeps expired data.
- `obsalt export --org acme --dest ./exports/acme` writes a manifest
  of active revisions. obsalt cannot revoke copies you move elsewhere.

Backfill, judges, embedders, OTLP destinations, and outbound webhooks
share one egress policy: scheme/port checks, no private ranges by
default, DNS re-resolved after redirects.

## Next

Every `OBSALT_*` knob: [Configuration](reference/configuration.md).
Tenancy: [Security](reference/security.md). Scripts: [HTTP API](api.md).
