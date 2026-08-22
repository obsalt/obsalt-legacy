from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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
