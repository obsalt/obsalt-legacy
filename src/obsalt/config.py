from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBSALT_", extra="ignore")

    api_keys: str = "demo:demo-secret"
    vapi_secret: str = ""
    retell_secret: str = ""
    bland_secret: str = ""
    otlp_endpoint: str = ""
    require_auth: bool = False

    def parsed_api_keys(self) -> dict[str, str]:
        """Map secret → org_id. Format: org:secret,org2:secret2"""
        mapping: dict[str, str] = {}
        for chunk in self.api_keys.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            org, secret = chunk.split(":", 1)
            mapping[secret.strip()] = org.strip()
        return mapping
