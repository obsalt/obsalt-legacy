from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBSALT_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    environment: str = "dev"
    public_url: str = "http://127.0.0.1:8080"

    postgres_dsn: str = "postgresql://obsalt:obsalt@127.0.0.1:5432/obsalt"
    clickhouse_url: str = "http://127.0.0.1:8123"
    clickhouse_db: str = "obsalt"
    redis_url: str = "redis://127.0.0.1:6379/0"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_bucket: str = "obsalt"
    s3_access_key: str = "obsalt"
    s3_secret_key: str = "obsalt-secret"
    master_key: str = ""

    session_secret: str = Field(default="change-me-session")
    bootstrap_owner_email: str = "owner@localhost"

    raw_retention_days: int = 30
    evidence_retention_days: int = 90
    aggregate_retention_days: int = 400

    max_call_duration_seconds: int = 4 * 3600
    trace_grace_seconds: int = 30
    grpc_otlp: bool = False

    llm_monthly_cap_usd: float = 0.0
    baseline_sample_rate: float = 0.0
    emit_pii: bool = False
    proposed_genai: bool = False

    demo: bool = False


def resolve_config_path() -> Path | None:
    env = os.environ.get("OBSALT_CONFIG", "").strip()
    candidates = [Path(env).expanduser()] if env else []
    candidates.extend((Path("obsalt.toml"), Path.home() / ".config" / "obsalt" / "obsalt.toml"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_toml() -> dict[str, Any]:
    import tomllib

    path = resolve_config_path()
    if path is None:
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}
