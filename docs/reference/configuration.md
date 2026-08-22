# Configuration

**Who this is for:** you are writing a `.env` or wondering which
`OBSALT_*` knob does what.

**Question this page answers:** what is the default, and what happens
if I leave the shipped secrets in place?

Every setting is an `OBSALT_*` environment variable. A `.env` file in
the working directory is loaded. Extra keys are ignored.

Copy `.env.example` via `obsalt init`. `obsalt init --write-env` also
writes `.env` when it is missing and will not overwrite an existing
file. Replace every `change-me` and `dev-key` before a network-exposed
deploy. `Settings.insecure_defaults()` is true when the master key,
session secret, or bootstrap API key is still the shipped value.
`/ready` reports that. `obsalt doctor` prints the same warning.

Every `Settings` field must appear in `ENV_EXAMPLE`
(`packages/obsalt/src/obsalt/config.py`). The unit test fails if a new
key is added without the example line.

## Process

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_HOST` | `0.0.0.0` | Bind address |
| `OBSALT_PORT` | `8080` | HTTP port |
| `OBSALT_ENVIRONMENT` | `dev` | `test` / `testing` may construct memory doubles via `create_test_app` only |
| `OBSALT_SERVICE_NAME` | `obsalt` | OTel service name |

## Stores

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_POSTGRES_DSN` | `postgresql://obsalt:obsalt@localhost:5432/obsalt` | libpq DSN |
| `OBSALT_CLICKHOUSE_URL` | `http://localhost:8123` | HTTP URL |
| `OBSALT_CLICKHOUSE_DATABASE` | `obsalt` | Database name |
| `OBSALT_REDIS_URL` | `redis://localhost:6379/0` | Lease accelerator |
| `OBSALT_S3_ENDPOINT` | `http://localhost:9010` | MinIO / S3 / GCS-compatible |
| `OBSALT_S3_BUCKET` | `obsalt` | Bucket |
| `OBSALT_S3_ACCESS_KEY` | `obsalt` | Access key |
| `OBSALT_S3_SECRET_KEY` | `obsalt-secret` | Secret key |
| `OBSALT_S3_REGION` | `us-east-1` | Region |

Postgres uses a DSN. ClickHouse, Redis, and S3 use URLs. That is
intentional.

## Secrets and bootstrap

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_MASTER_KEY` | `change-me-master-key-not-for-production` | Envelope encryption |
| `OBSALT_SESSION_SECRET` | `change-me-session` | Signed session cookies |
| `OBSALT_BOOTSTRAP_ORG_ID` | `local` | First org |
| `OBSALT_BOOTSTRAP_API_KEY` | `dev-key` | First hashed key |

## Ingest and OTLP

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_OTLP_GRPC_ENABLED` | `false` | Opt-in gRPC receiver |
| `OBSALT_OTLP_GRPC_PORT` | `4317` | Requires `obsalt[grpc]` |
| `OBSALT_EMIT_PII` | `false` | Allow `obsalt.pii.*` on exported spans |
| `OBSALT_COMPRESSED_BODY_LIMIT` | `1000000` | Incoming compressed bytes |
| `OBSALT_EXPANDED_BODY_LIMIT` | `8000000` | Incoming expanded bytes |
| `OBSALT_OUTBOX_BACKPRESSURE_LIMIT` | `10000` | OTLP 503 threshold |
| `OBSALT_MAX_CALL_DURATION_SECONDS` | `14400` | Trace finalize upper bound |
| `OBSALT_TRACE_GRACE_SECONDS` | `30` | After root end |
| `OBSALT_PLUGIN_DEADLINE_SECONDS` | `10` | Plugin join deadline |

## Retention and keys

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_RAW_RETENTION_DAYS` | `30` | Unredacted raw blobs |
| `OBSALT_TRANSCRIPT_RETENTION_DAYS` | `90` | Transcripts / recordings |
| `OBSALT_AGGREGATE_RETENTION_DAYS` | `400` | Rollup facts |
| `OBSALT_BACKUP_RETENTION_DAYS` | `30` | Managed backup expiry |
| `OBSALT_KEY_ROTATION_OVERLAP_SECONDS` | `86400` | Dual-key accept window |

## Analysis

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSALT_LLM_MONTHLY_BUDGET_USD` | `0` | Hard cap. `0` means no paid spend. |
| `OBSALT_BASELINE_SAMPLE_RATE` | `0` | Unbiased fleet sample. Triggers still run. |
| `OBSALT_JUDGE_BASE_URL` | unset | OpenAI-compatible judge |
| `OBSALT_JUDGE_API_KEY` | unset | Judge credential |
| `OBSALT_JUDGE_MODEL` | `gpt-4.1-mini` | Default judge model |
| `OBSALT_EMBEDDER_ONNX_PATH` | unset | Override local ONNX weights |
| `OBSALT_SLO_E2E_MS` | `2000` | SLO threshold for outbound `slo.breached` |

`OBSALT_LLM_*` is spend. `OBSALT_JUDGE_*` is the judge endpoint. They
are related and intentionally different prefixes: budget is a product
control, judge is a plugin capability.

## What's next

Flags on the command: [CLI](cli.md). Running this for real:
[Operate](../ops.md). Tenancy implications:
[Security](security.md).
