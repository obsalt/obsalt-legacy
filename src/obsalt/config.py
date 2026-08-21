from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from obsalt._version import __version__

_KNOWN_ROOT = {
    "api_keys",
    "vapi_secret",
    "retell_secret",
    "bland_secret",
    "otlp_endpoint",
    "require_auth",
    "host",
    "port",
    "environment",
    "service_name",
}

_NESTED = {
    ("server", "host"): "host",
    ("server", "port"): "port",
    ("auth", "api_keys"): "api_keys",
    ("auth", "require_auth"): "require_auth",
    ("export", "otlp_endpoint"): "otlp_endpoint",
    ("export", "environment"): "environment",
    ("export", "service_name"): "service_name",
    ("webhooks", "vapi_secret"): "vapi_secret",
    ("webhooks", "retell_secret"): "retell_secret",
    ("webhooks", "bland_secret"): "bland_secret",
}


def resolve_config_path() -> Path | None:
    """First existing path among OBSALT_CONFIG, ./obsalt.toml, ~/.config/obsalt/obsalt.toml."""
    env = os.environ.get("OBSALT_CONFIG", "").strip()
    candidates = [Path(env).expanduser()] if env else []
    candidates.extend((Path("obsalt.toml"), Path.home() / ".config" / "obsalt" / "obsalt.toml"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def flatten_toml(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "obsalt" and isinstance(value, dict):
            out.update(flatten_toml(value))
            continue
        if key in _KNOWN_ROOT and not isinstance(value, dict):
            out[key] = value
            continue
        if isinstance(value, dict):
            for inner_key, inner_val in value.items():
                mapped = _NESTED.get((key, inner_key))
                if mapped:
                    out[mapped] = inner_val
    return out


def load_toml_settings(path: Path | None = None) -> dict[str, Any]:
    import tomllib

    path = path or resolve_config_path()
    if path is None:
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        return {}
    return flatten_toml(data)


class TomlSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = load_toml_settings()

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in self._data.items()
            if name in self.settings_cls.model_fields
        }


class Settings(BaseSettings):
    """Runtime settings. Env vars win, then ``.env``, then ``obsalt.toml``, then defaults.

    ``OBSALT_API_KEYS`` format: ``org:secret,org2:secret2`` (secret → org).
    """

    model_config = SettingsConfigDict(
        env_prefix="OBSALT_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_keys: str = "demo:demo-secret"
    vapi_secret: str = ""
    retell_secret: str = ""
    bland_secret: str = ""
    otlp_endpoint: str = ""
    require_auth: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    environment: str = "dev"
    service_name: str = "obsalt"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlSettingsSource(settings_cls),
            file_secret_settings,
        )

    def parsed_api_keys(self) -> dict[str, str]:
        """Map secret → org_id."""
        mapping: dict[str, str] = {}
        for org, secret in self._api_key_pairs():
            mapping[secret] = org
        return mapping

    def _api_key_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for chunk in self.api_keys.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            org, secret = chunk.split(":", 1)
            org, secret = org.strip(), secret.strip()
            if org and secret:
                pairs.append((org, secret))
        return pairs

    def org_ids(self) -> list[str]:
        seen: list[str] = []
        for org, _secret in self._api_key_pairs():
            if org not in seen:
                seen.append(org)
        return seen

    def malformed_api_key_chunks(self) -> list[str]:
        bad: list[str] = []
        for chunk in self.api_keys.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                bad.append(chunk)
                continue
            org, secret = chunk.split(":", 1)
            if not org.strip() or not secret.strip():
                bad.append(chunk)
        return bad

    def duplicate_secret_orgs(self) -> list[str]:
        seen: dict[str, str] = {}
        dupes: list[str] = []
        for org, secret in self._api_key_pairs():
            if secret in seen and seen[secret] != org:
                dupes.append(f"{seen[secret]} and {org} share a secret")
            seen[secret] = org
        return dupes

    def uses_insecure_defaults(self) -> bool:
        keys = self.parsed_api_keys()
        return (not self.require_auth) or keys == {"demo-secret": "demo"} or "demo-secret" in keys

    def summary(self) -> dict[str, Any]:
        """Operator-safe view — never includes secrets."""
        path = resolve_config_path()
        return {
            "version": __version__,
            "config_file": str(path) if path else None,
            "host": self.host,
            "port": self.port,
            "environment": self.environment,
            "service_name": self.service_name,
            "otlp_endpoint": self.otlp_endpoint or None,
            "require_auth": self.require_auth,
            "orgs": self.org_ids(),
            "vapi_hmac": bool(self.vapi_secret),
            "retell_hmac": bool(self.retell_secret),
            "bland_hmac": bool(self.bland_secret),
            "store": "memory",
        }
