"""Process settings. Every field is `OBSALT_<NAME>` in the environment.

Postgres uses a libpq DSN. ClickHouse, Redis, and S3 use HTTP/URL endpoints.
That suffix difference is intentional, not drift.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Written by `obsalt init` and kept in the repo as `.env.example`.
# The unit test asserts every Settings field appears as OBSALT_<FIELD>.
ENV_EXAMPLE = """# obsalt environment. Copy to `.env` and replace every change-me / dev-key
# before the process is reachable from a network you do not trust.
# `obsalt init --write-env` does the copy when `.env` is missing.

# --- secrets ---------------------------------------------------------------
OBSALT_MASTER_KEY=change-me-master-key-not-for-production
OBSALT_SESSION_SECRET=change-me-session
OBSALT_BOOTSTRAP_API_KEY=dev-key
OBSALT_BOOTSTRAP_ORG_ID=local

# --- process ---------------------------------------------------------------
# OBSALT_HOST=0.0.0.0
# OBSALT_PORT=8080
# OBSALT_ENVIRONMENT=dev
# OBSALT_SERVICE_NAME=obsalt

# --- stores (docker compose defaults) --------------------------------------
OBSALT_POSTGRES_DSN=postgresql://obsalt:obsalt@localhost:5432/obsalt
OBSALT_CLICKHOUSE_URL=http://localhost:8123
OBSALT_CLICKHOUSE_DATABASE=obsalt
OBSALT_REDIS_URL=redis://localhost:6379/0
OBSALT_S3_ENDPOINT=http://localhost:9010
OBSALT_S3_BUCKET=obsalt
OBSALT_S3_ACCESS_KEY=obsalt
OBSALT_S3_SECRET_KEY=obsalt-secret
OBSALT_S3_REGION=us-east-1

# --- ingest / OTLP ---------------------------------------------------------
# OBSALT_OTLP_GRPC_ENABLED=false
# OBSALT_OTLP_GRPC_PORT=4317
# OBSALT_EMIT_PII=false
# OBSALT_COMPRESSED_BODY_LIMIT=1000000
# OBSALT_EXPANDED_BODY_LIMIT=8000000
# OBSALT_OUTBOX_BACKPRESSURE_LIMIT=10000
# OBSALT_MAX_CALL_DURATION_SECONDS=14400
# OBSALT_TRACE_GRACE_SECONDS=30
# OBSALT_PLUGIN_DEADLINE_SECONDS=10
# OBSALT_SLO_E2E_MS=2000

# --- retention and keys ----------------------------------------------------
# OBSALT_RAW_RETENTION_DAYS=30
# OBSALT_TRANSCRIPT_RETENTION_DAYS=90
# OBSALT_AGGREGATE_RETENTION_DAYS=400
# OBSALT_BACKUP_RETENTION_DAYS=30
# OBSALT_KEY_ROTATION_OVERLAP_SECONDS=86400

# --- analysis --------------------------------------------------------------
# OBSALT_LLM_MONTHLY_BUDGET_USD=0
# OBSALT_BASELINE_SAMPLE_RATE=0
# OBSALT_JUDGE_BASE_URL=
# OBSALT_JUDGE_API_KEY=
# OBSALT_JUDGE_MODEL=gpt-4.1-mini
# OBSALT_EMBEDDER_ONNX_PATH=
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBSALT_", env_file=(".env",), extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    environment: str = "dev"
    service_name: str = "obsalt"

    postgres_dsn: str = "postgresql://obsalt:obsalt@localhost:5432/obsalt"
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "obsalt"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9010"
    s3_bucket: str = "obsalt"
    s3_access_key: str = "obsalt"
    s3_secret_key: str = "obsalt-secret"
    s3_region: str = "us-east-1"

    master_key: str = "change-me-master-key-not-for-production"
    session_secret: str = "change-me-session"
    bootstrap_org_id: str = "local"
    bootstrap_api_key: str = "dev-key"

    otlp_grpc_enabled: bool = False
    otlp_grpc_port: int = 4317
    emit_pii: bool = False

    raw_retention_days: int = 30
    transcript_retention_days: int = 90
    aggregate_retention_days: int = 400

    max_call_duration_seconds: int = 4 * 60 * 60
    trace_grace_seconds: int = 30

    compressed_body_limit: int = 1_000_000
    expanded_body_limit: int = 8_000_000
    outbox_backpressure_limit: int = 10_000
    slo_e2e_ms: float = 2000.0
    key_rotation_overlap_seconds: int = 86_400
    plugin_deadline_seconds: float = 10.0
    backup_retention_days: int = 30

    llm_monthly_budget_usd: float = 0.0
    baseline_sample_rate: float = 0.0
    judge_base_url: str | None = None
    judge_api_key: str | None = None
    judge_model: str = "gpt-4.1-mini"
    embedder_onnx_path: str | None = None

    def insecure_defaults(self) -> bool:
        return (
            self.master_key.startswith("change-me")
            or self.session_secret.startswith("change-me")
            or self.bootstrap_api_key == "dev-key"
        )
